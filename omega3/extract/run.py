"""Stage 3 orchestration: text layer -> parser -> (LLM fallback) -> SQLite."""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter

from .. import db
from . import llm
from .derive import derive
from .parser import PARSER_VERSION, Parsed, parse_report
from .pdftext import PdfDoc

MEASUREMENT_COLS = ("category", "analyte", "label_raw", "spec_text", "spec_op", "spec_value", "spec_unit",
                    "result_text", "result_op", "result_value", "result_unit", "compliance")


def load_doc(cfg, sha: str) -> PdfDoc:
    return PdfDoc.from_json((cfg.data_dir / "raw" / "text" / f"{sha}.json").read_text(encoding="utf-8"))


def pending_extractions(conn) -> list:
    """Reports that are downloaded but not yet extracted for their current hash."""
    return conn.execute(
        """
        SELECT r.filename, r.sha256 FROM reports r
        LEFT JOIN extractions e ON e.filename = r.filename AND e.sha256 = r.sha256
        WHERE r.status = 'ok' AND (e.filename IS NULL OR e.status = 'failed' OR e.parser_version IS NOT ?)
        ORDER BY r.filename
        """, (PARSER_VERSION,)
    ).fetchall()


def _listing_context(conn, filename: str) -> tuple[str | None, dt.date | None]:
    row = conn.execute("SELECT lot, expiry_date FROM batches WHERE report_filename=? ORDER BY listed DESC LIMIT 1", (filename,)).fetchone()
    if not row:
        return None, None
    return row["lot"], dt.date.fromisoformat(row["expiry_date"]) if row["expiry_date"] else None


def store(conn, filename: str, sha: str, parsed: Parsed, derived: dict, method: str, model: str | None) -> None:
    row = {"filename": filename, "sha256": sha, "method": method, "model": model, "parser_version": PARSER_VERSION,
           "extracted_at": db.now_iso(), **derived}
    conn.execute("DELETE FROM extractions WHERE filename=?", (filename,))
    conn.execute("DELETE FROM measurements WHERE filename=?", (filename,))
    conn.execute("DELETE FROM unrecognized_labels WHERE filename=?", (filename,))
    db.upsert(conn, "extractions", row, keys=("filename",), keep=())
    if derived["status"] == "ok":
        for r in parsed.rows.values():
            db.upsert(conn, "measurements", {"filename": filename, **{c: getattr(r, c) for c in MEASUREMENT_COLS}},
                      keys=("filename", "analyte"), keep=())
    for line in dict.fromkeys(parsed.unrecognized):
        conn.execute("INSERT OR IGNORE INTO unrecognized_labels (filename, line) VALUES (?, ?)", (filename, line))


def extract_all(cfg, conn, log=print) -> dict:
    """Parse every pending report. Parser failures go to the LLM fallback:
    a cached result if one exists, otherwise the API (when enabled) or the
    in-session queue (data/cache/llm/queue.json)."""
    stats = Counter()
    queue: list[dict] = []
    todo = pending_extractions(conn)
    for i, (filename, sha) in enumerate(todo, 1):
        lot, expiry = _listing_context(conn, filename)
        doc = load_doc(cfg, sha)
        parsed = parse_report(doc)
        derived = derive(parsed, cfg, lot, expiry)
        method, model = "parser", None
        if derived["status"] == "failed":
            parser_error = derived["error"]
            cached = llm.load_cached(cfg, sha)
            if cached is not None:
                parsed, model = cached
                derived = derive(parsed, cfg, lot, expiry)
                method = "llm"
                stats["llm_from_cache"] += 1
                if derived["status"] == "failed":
                    derived["error"] = f"parser: {parser_error} | llm: {derived['error']}"
            else:
                queue.append({"sha256": sha, "filename": filename, "reason": parser_error})
        store(conn, filename, sha, parsed, derived, method, model)
        stats[f"{method}_{derived['status']}"] += 1
        stats[f"template_{parsed.template}"] += 1
        if i % 500 == 0:
            conn.commit()
            log(f"  {i}/{len(todo)} reports extracted")
    conn.commit()

    if queue and cfg["llm"]["api_enabled"]:
        done = llm.extract_via_api(cfg, queue, lambda sha: load_doc(cfg, sha), log=log)
        stats["llm_api_calls"] = done
        if done:                                   # pick the fresh results up from the cache
            again = extract_all(cfg, conn, log=log)
            for key in ("llm_ok", "llm_failed", "llm_from_cache"):
                stats[key] += again.get(key, 0)
            return dict(stats)
    llm.write_queue(cfg, queue)
    stats["llm_queue"] = len(queue)
    stats["unrecognized_label_lines"] = conn.execute("SELECT COUNT(*) FROM unrecognized_labels").fetchone()[0]
    return dict(stats)
