"""Stage 2: download report PDFs once, hash them, keep the text layer.

Raw cache layout (content-addressed):
    data/raw/pdf/<sha256>.pdf    raw file, gitignored (DECISIONS D-5)
    data/raw/text/<sha256>.json  extracted text layer and tables, committed
The `reports` table is the manifest: filename -> URL, hash, validators, dates.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from collections import Counter
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from .. import db
from ..extract.pdftext import read_pdf


def report_url(cfg, filename: str) -> str:
    return cfg["scrape"]["files_url"] + quote(filename)


def pending_reports(cfg, conn) -> list[str]:
    """Report files that still need fetching: listed, unexpired lots of products
    that passed (or have not yet failed) the capsule filter, not yet in the
    manifest with status ok."""
    rows = conn.execute(
        """
        SELECT DISTINCT b.report_filename
        FROM batches b
        JOIN products p ON p.product_num = b.product_num
        LEFT JOIN reports r ON r.filename = b.report_filename
        WHERE b.listed = 1 AND p.listed = 1
          AND p.format_decision IN ('include', 'pending')
          AND b.report_filename IS NOT NULL AND b.report_filename <> ''
          AND (b.expiry_date IS NULL OR b.expiry_date >= ?)
          AND (r.filename IS NULL OR r.status <> 'ok')
        ORDER BY b.report_filename
        """,
        (cfg.today.isoformat(),),
    ).fetchall()
    return [r[0] for r in rows]


def _http_date(value: str | None) -> dt.date | None:
    try:
        return parsedate_to_datetime(value).date() if value else None
    except (TypeError, ValueError):
        return None


def store_report(cfg, conn, filename: str, content: bytes, headers: dict, url: str) -> dict:
    """Hash, cache and index one downloaded report. Returns the manifest row."""
    raw_dir = cfg.data_dir / "raw"
    (raw_dir / "pdf").mkdir(parents=True, exist_ok=True)
    (raw_dir / "text").mkdir(parents=True, exist_ok=True)
    now = db.now_iso()
    sha = hashlib.sha256(content).hexdigest()
    row = {
        "filename": filename, "url": url, "status": "ok", "error": None, "sha256": sha, "bytes": len(content),
        "etag": headers.get("ETag"), "last_modified": headers.get("Last-Modified"),
        "pdf_created": None, "pdf_producer": None, "pages": None,
        "report_date": None, "report_date_source": None, "first_seen": now, "last_checked": now,
    }
    if not content.startswith(b"%PDF"):
        row.update(status="error", error="not a PDF (missing %PDF header)")
    else:
        pdf_path = raw_dir / "pdf" / f"{sha}.pdf"
        pdf_path.write_bytes(content)
        try:
            parsed = read_pdf(pdf_path)
        except Exception as exc:  # malformed PDF: record, do not crash the crawl
            row.update(status="error", error=f"PDF parse failed: {type(exc).__name__}: {exc}"[:300])
        else:
            (raw_dir / "text" / f"{sha}.json").write_text(parsed.to_json(), encoding="utf-8")
            modified = _http_date(headers.get("Last-Modified"))
            # Report date stands in for the test date IFOS does not publish (P-1).
            if parsed.created:
                row.update(report_date=parsed.created.isoformat(), report_date_source="pdf_created")
            elif modified:
                row.update(report_date=modified.isoformat(), report_date_source="http_last_modified")
            row.update(pdf_created=parsed.created.isoformat() if parsed.created else None,
                       pdf_producer=parsed.producer, pages=len(parsed.pages))
    db.upsert(conn, "reports", row, keys=("filename",))
    return row


def fetch_reports(cfg, conn, session, limit: int | None = None, log=print) -> dict:
    """Download every pending report. Already-fetched filenames cost nothing."""
    todo = pending_reports(cfg, conn)
    stats = Counter(reports_pending=len(todo))
    for i, filename in enumerate(todo[:limit] if limit else todo, 1):
        url = report_url(cfg, filename)
        try:
            resp = session.get(url)
            row = store_report(cfg, conn, filename, resp.content, dict(resp.headers), url)
        except Exception as exc:
            now = db.now_iso()
            row = {"filename": filename, "url": url, "status": "error", "error": f"{type(exc).__name__}: {exc}"[:300],
                   "first_seen": now, "last_checked": now}
            db.upsert(conn, "reports", row, keys=("filename",))
        stats[f"reports_{row['status']}"] += 1
        stats["bytes_downloaded"] += row.get("bytes") or 0
        if row["status"] != "ok":
            log(f"  report error: {filename}: {row['error']}")
        if i % 50 == 0:
            conn.commit()
            log(f"  {i}/{len(todo)} reports")
    conn.commit()
    return dict(stats)
