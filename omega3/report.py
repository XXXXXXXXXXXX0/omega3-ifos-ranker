"""Reports and changelog. Everything here is formatting: no ranking decisions.

The per-product reasoning is assembled by code from the numbers (D-10).
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .load import placement_row
from .rank import (EXCLUDED, FLAVORED, FORM_UNVERIFIED, INCOMPLETE, MONOGLYCERIDE, OIL_UNAVAILABLE, PET, RANKED, RankResult,
                   agent_queue)

LIST_TITLES = {
    RANKED: "Ranked", FLAVORED: "Flavored (ranked separately)", FORM_UNVERIFIED: "Form unverified",
    MONOGLYCERIDE: "Monoglyceride", INCOMPLETE: "Incomplete data", OIL_UNAVAILABLE: "Oil weight unavailable",
    PET: "Pet products", EXCLUDED: "Excluded",
}
COLUMNS = [  # (header, key, format)
    ("Rank", "rank", "{}"), ("Oxidation load", "score", "{:.2f}"), ("TOTOX", "totox", "{:g}"), ("PV", "pv", "{:g}"),
    ("AV", "av", "{:g}"), ("Acid value", "acid_value", "{:g}"), ("EPA+DHA mg/capsule", "epa_dha_mg_cap", "{:.0f}"),
    ("mg/serving", "epa_dha_mg_serving", "{:.0f}"), ("mg/g oil", "epa_dha_mg_g", "{:.0f}"), ("EPA:DHA", "epa_dha_ratio", "{:.2f}"),
    ("Report date", "report_date", "{}"), ("Expiry", "expiry_date", "{}"),
]


def fmt(value, pattern: str = "{}") -> str:
    return "n/a" if value is None else pattern.format(value)


def reasoning(row: dict, neighbours: dict[int, list[dict]], cfg) -> str:
    """Plain-language account of why a product sits where it does, built from its numbers."""
    ox = cfg["filters"]["oxidation"]
    target = cfg["scoring"]["target_epa_dha_mg"]
    parts = [
        f"Oxidation load {row['score']:.2f} = TOTOX {row['totox']:g} x {row['oil_g_per_target']:.2f} g of oil needed for "
        f"{target} mg EPA+DHA ({row['epa_dha_mg_g']:.0f} mg per g of oil).",
        f"Lot {row['lot']}: PV {fmt(row['pv'], '{:g}')}, AV {fmt(row['av'], '{:g}')}, acid value {fmt(row['acid_value'], '{:g}')}; "
        f"all reported contaminants within IFOS limits.",
    ]
    parts.append(f"TOTOX is in the preferred tier (at or below {ox['totox_preferred_max']:g})." if row["preferred"]
                 else f"TOTOX passes the cutoff (below {ox['totox_max_exclusive']:g}) but is above the preferred tier of {ox['totox_preferred_max']:g}.")
    parts.append(f"Form {row['form']} per {row['form_source']}: \"{(row['form_evidence'] or '').strip()[:160]}\".")
    if row["oil_source"] == "label":
        parts.append(f"Oil weight {row['oil_mg_per_capsule']:g} mg per capsule from the label.")
    elif row["oil_source"] == "ifos_listing":
        parts.append(f"Oil weight {row['oil_mg_per_capsule']:g} mg per capsule as stated in the brand's IFOS listing text.")
    elif row["oil_source"] == "ifos_area_pct":
        parts.append(f"Oil weight {row['oil_mg_per_capsule']:g} mg per capsule is an estimate from the area-% figures in the IFOS report, not a label value.")
    else:
        parts.append("The IFOS report gives potency per gram of oil, so no capsule weight was needed.")
    tied = [r for r in neighbours.get(row["tie_group"], []) if r["product_num"] != row["product_num"]]
    if tied:
        margin = int(round(cfg["scoring"]["tie_margin"] * 100))
        others = ", ".join(f"#{r['rank']}" for r in tied)
        parts.append(f"Scores within {margin}% are treated as ties: tied with {others}, ordered by lower AV, then higher EPA+DHA per capsule.")
    if row["inconsistent"]:
        parts.append(f"Caution: another unexpired batch fails a cutoff (TOTOX across unexpired batches {row['totox_range'][0]:g} to {row['totox_range'][1]:g}).")
    return " ".join(parts)


def previous_run(conn, run_id: str) -> str | None:
    row = conn.execute("SELECT run_id FROM runs WHERE run_id < ? ORDER BY run_id DESC LIMIT 1", (run_id,)).fetchone()
    return row[0] if row else None


def snapshot_batches(conn, run_id: str) -> None:
    conn.execute("DELETE FROM run_batches WHERE run_id = ?", (run_id,))
    conn.execute("""INSERT INTO run_batches (run_id, product_num, lot, expiry_date)
                    SELECT ?, b.product_num, b.lot, b.expiry_date FROM batches b JOIN products p ON p.product_num = b.product_num
                    WHERE b.listed = 1 AND p.listed = 1""", (run_id,))
    conn.commit()


def build_changelog(conn, run_id: str, as_of: str) -> dict:
    """What changed since the previous run: new batches, expired or withdrawn
    batches, and every product whose list or rank moved."""
    prev = previous_run(conn, run_id)
    out = {"run_id": run_id, "previous_run": prev, "new_batches": [], "gone_batches": [], "ranking_changes": [], "first_run": prev is None}
    if prev is None:
        return out
    names = {r["product_num"]: f"{r['brand'] or r['brand_id']} {r['name']}" for r in conn.execute("SELECT product_num, brand, brand_id, name FROM products")}
    now = {(r[0], r[1]): r[2] for r in conn.execute("SELECT product_num, lot, expiry_date FROM run_batches WHERE run_id = ?", (run_id,))}
    was = {(r[0], r[1]): r[2] for r in conn.execute("SELECT product_num, lot, expiry_date FROM run_batches WHERE run_id = ?", (prev,))}
    for key in sorted(set(now) - set(was)):
        out["new_batches"].append({"product_num": key[0], "product": names.get(key[0], key[0]), "lot": key[1], "expiry_date": now[key]})
    for key in sorted(set(was) - set(now)):
        why = "expired" if was[key] and was[key] < as_of else "no longer listed"
        out["gone_batches"].append({"product_num": key[0], "product": names.get(key[0], key[0]), "lot": key[1], "expiry_date": was[key], "why": why})
    for key in sorted(set(now) & set(was)):                    # still listed but past expiry now
        if now[key] and now[key] < as_of <= (was[key] or as_of) and not (was[key] and was[key] < as_of):
            pass
    cur = {r["product_num"]: r for r in conn.execute("SELECT product_num, list_name, rank, score, lot FROM results WHERE run_id = ?", (run_id,))}
    old = {r["product_num"]: r for r in conn.execute("SELECT product_num, list_name, rank, score, lot FROM results WHERE run_id = ?", (prev,))}
    for num in sorted(set(cur) | set(old)):
        a, b = old.get(num), cur.get(num)
        before = f"{LIST_TITLES[a['list_name']]}" + (f" #{a['rank']}" if a and a["rank"] else "") if a else "not listed"
        after = f"{LIST_TITLES[b['list_name']]}" + (f" #{b['rank']}" if b and b["rank"] else "") if b else "no longer listed"
        if before != after or (a and b and a["lot"] != b["lot"]):
            out["ranking_changes"].append({"product_num": num, "product": names.get(num, num), "before": before, "after": after,
                                           "lot_before": a["lot"] if a else None, "lot_after": b["lot"] if b else None,
                                           "top": bool((a and a["rank"] and a["rank"] <= 10 and a["list_name"] == RANKED)
                                                       or (b and b["rank"] and b["rank"] <= 10 and b["list_name"] == RANKED))})
    out["ranking_changes"].sort(key=lambda c: (not c["top"], c["product"]))
    return out


def build_context(cfg, conn, result: RankResult, run_id: str, stats: dict) -> dict:
    rows = {name: [placement_row(p) for p in result.of(name)] for name in LIST_TITLES}
    top_n = cfg["scoring"]["top_n"]
    groups: dict[int, list[dict]] = {}
    for row in rows[RANKED]:
        groups.setdefault(row["tie_group"], []).append(row)
    top = rows[RANKED][:top_n]
    for row in top:
        row["reasoning"] = reasoning(row, groups, cfg)
    flavored_groups: dict[int, list[dict]] = {}
    for row in rows[FLAVORED]:
        flavored_groups.setdefault(row["tie_group"], []).append(row)
    queue, threshold = agent_queue(result, cfg)
    in_reach = [p for p in queue if threshold is None or p.optimistic_score <= threshold]
    checked = [p for p in in_reach if p.product.label is not None]
    attempted = conn.execute("SELECT COUNT(*), SUM(status='verified'), SUM(tool_calls), SUM(cost_usd) FROM label_facts").fetchone()
    models = [r[0] for r in conn.execute("SELECT DISTINCT model FROM label_facts")]
    return {
        "run_id": run_id, "as_of": result.as_of.isoformat(), "cfg": cfg, "top_n": top_n, "columns": COLUMNS, "fmt": fmt,
        "titles": LIST_TITLES, "rows": rows, "top": top, "counts": result.counts(), "exclusion_counts": result.exclusion_counts(),
        "inconsistent": [placement_row(p) for p in sorted(result.inconsistent, key=lambda p: (p.list_name, p.rank or 9999, p.product.name))],
        "total_products": len(result.placements), "stats": stats,
        "coverage": {"queue": len(queue), "in_reach": len(in_reach), "threshold": threshold, "ranked": len(rows[RANKED]),
                     "in_reach_checked": len(checked), "in_reach_unchecked": len(in_reach) - len(checked),
                     "top_complete": len(rows[RANKED]) >= top_n and not in_reach,
                     "in_reach_rows": [placement_row(p) for p in in_reach[:30]]},
        "agent": {"attempted": attempted[0] or 0, "verified": attempted[1] or 0, "tool_calls": attempted[2] or 0,
                  "cost_usd": attempted[3] or 0.0, "models": models, "in_session": models == ["in-session"] or not models},
        "extraction": dict(conn.execute("SELECT method || '_' || status, COUNT(*) FROM extractions GROUP BY 1").fetchall()),
    }


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["num"] = lambda value, pattern="{:g}": fmt(value, pattern)
    return env


def text_table(context: dict) -> str:
    """Top N as a plain-text table for the terminal."""
    headers = ["#", "Load", "TOTOX", "PV", "AV", "Acid", "mg/cap", "mg/g", "Brand / product"]
    lines = [[str(r["rank"]), fmt(r["score"], "{:.2f}"), fmt(r["totox"], "{:g}"), fmt(r["pv"], "{:g}"), fmt(r["av"], "{:g}"),
              fmt(r["acid_value"], "{:g}"), fmt(r["epa_dha_mg_cap"], "{:.0f}"), fmt(r["epa_dha_mg_g"], "{:.0f}"),
              f"{r['brand']} / {r['product']}"[:70]] for r in context["top"]]
    widths = [max(len(h), *(len(l[i]) for l in lines)) if lines else len(h) for i, h in enumerate(headers)]
    render = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    return "\n".join([render(headers), render(["-" * w for w in widths]), *map(render, lines)])


def write_reports(cfg, conn, result: RankResult, run_id: str, stats: dict) -> dict:
    """Write report.html, report.md, the dated changelog and the CSV exports."""
    context = build_context(cfg, conn, result, run_id, stats)
    changelog = build_changelog(conn, run_id, result.as_of.isoformat())
    context["changelog"] = changelog
    env = _env()
    out_dir = cfg.reports_dir / result.as_of.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.html").write_text(env.get_template("report.html.j2").render(**context), encoding="utf-8")
    (out_dir / "report.md").write_text(env.get_template("report.md.j2").render(**context), encoding="utf-8")
    log_dir = cfg.reports_dir / "changelogs"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_id = run_id.replace(":", "").replace("+0000", "Z").replace("+00:00", "Z")
    changelog_path = log_dir / f"{safe_id}.md"
    changelog_path.write_text(env.get_template("changelog.md.j2").render(**context), encoding="utf-8")
    latest = cfg.reports_dir / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for name in ("report.html", "report.md"):
        shutil.copyfile(out_dir / name, latest / name)
    shutil.copyfile(changelog_path, latest / "changelog.md")
    write_exports(cfg, conn, context)
    return {"report_dir": str(out_dir), "changelog": str(changelog_path), "new_batches": len(changelog["new_batches"]),
            "gone_batches": len(changelog["gone_batches"]), "ranking_changes": len(changelog["ranking_changes"]),
            "top_complete": context["coverage"]["top_complete"]}


def write_exports(cfg, conn, context: dict) -> None:
    """Diffable CSV copies of the main tables."""
    out = cfg.data_dir / "exports"
    out.mkdir(parents=True, exist_ok=True)

    def dump(name: str, sql: str) -> None:
        cursor = conn.execute(sql)
        with open(out / name, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([d[0] for d in cursor.description])
            writer.writerows(cursor)

    dump("batches.csv", """
        SELECT p.product_num, p.brand, p.name AS product, p.product_type, b.lot, b.expiry_date, r.report_date, r.sha256,
               e.template, e.method, e.status, e.basis_text, e.epa_orig, e.dha_orig, e.total_omega3_orig, e.epa_mg_cap,
               e.dha_mg_cap, e.total_omega3_mg_cap, e.epa_mg_g, e.dha_mg_g, e.conversion, e.pv, e.av, e.totox,
               e.totox_source, e.acid_value, e.report_form, b.report_filename
        FROM batches b JOIN products p ON p.product_num = b.product_num
        LEFT JOIN reports r ON r.filename = b.report_filename LEFT JOIN extractions e ON e.filename = b.report_filename
        WHERE b.listed = 1 ORDER BY p.product_num, b.lot""")
    dump("label_facts.csv", """
        SELECT product_num, status, form, form_source_url, form_evidence, oil_mg_per_capsule, oil_source_url, oil_evidence,
               serving_capsules, servings_per_bottle, flavored, antioxidants, packaging, model, tool_calls, cost_usd, verified_at, reasoning
        FROM label_facts ORDER BY product_num""")
    keys = ["list", "rank", "score", "product_num", "brand", "product", "lot", "totox", "pv", "av", "acid_value", "epa_dha_mg_cap",
            "epa_dha_mg_serving", "epa_dha_mg_g", "epa_dha_ratio", "oil_mg_per_capsule", "oil_source", "form", "form_source",
            "report_date", "expiry_date", "preferred", "inconsistent", "reason", "flags"]
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(keys)
        for name in LIST_TITLES:
            for row in context["rows"][name]:
                writer.writerow([json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], list) else row[k] for k in keys])


def keep_ranked_pdfs(cfg, conn, result: RankResult, session=None, log=print) -> dict:
    """Copy the raw PDF behind every ranked batch into the committed folder
    (storage.commit_raw_pdfs). A PDF missing from the local cache is fetched
    again and accepted only if its SHA-256 matches the manifest."""
    import hashlib
    mode = cfg["storage"]["commit_raw_pdfs"]
    kept_dir, cache_dir = cfg.data_dir / "raw" / "pdf_kept", cfg.data_dir / "raw" / "pdf"
    stats = {"pdfs_kept": 0, "pdfs_refetched": 0, "pdfs_unavailable": 0}
    if mode == "none":
        return stats
    kept_dir.mkdir(parents=True, exist_ok=True)
    wanted = {}
    for p in result.placements:
        if p.batch and p.batch.report_filename and (mode == "all" or p.list_name in (RANKED, FLAVORED)):
            row = conn.execute("SELECT sha256, url FROM reports WHERE filename = ? AND status = 'ok'", (p.batch.report_filename,)).fetchone()
            if row:
                wanted[row["sha256"]] = row["url"]
    for sha, url in wanted.items():
        target = kept_dir / f"{sha}.pdf"
        if not target.exists():
            source = cache_dir / f"{sha}.pdf"
            if source.exists():
                shutil.copyfile(source, target)
            elif session is not None:
                try:
                    content = session.get(url).content
                except Exception as exc:
                    log(f"  could not re-fetch {url}: {exc}")
                    content = b""
                if hashlib.sha256(content).hexdigest() == sha:
                    target.write_bytes(content)
                    stats["pdfs_refetched"] += 1
                else:
                    stats["pdfs_unavailable"] += 1
                    continue
            else:
                stats["pdfs_unavailable"] += 1
                continue
        stats["pdfs_kept"] += 1
    for stale in kept_dir.glob("*.pdf"):                       # batches that left the ranking
        if stale.stem not in wanted:
            stale.unlink()
    return stats
