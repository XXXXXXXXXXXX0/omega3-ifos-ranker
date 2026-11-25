"""The whole pipeline as one call: crawl -> fetch -> extract -> verify -> rank -> report.

Every stage is incremental, so a re-run only does new work. Inside the refresh
window a re-run makes no network requests at all.
"""
from __future__ import annotations

from . import db
from .agent.session import restore_cache
from .extract.run import extract_all
from .load import load_products, save_results
from .rank import rank_products
from .report import keep_ranked_pdfs, snapshot_batches, write_reports


def new_run_id(conn) -> str:
    """UTC timestamp, unique and strictly increasing even for runs in the same second."""
    import datetime as dt
    stamp = dt.datetime.now(dt.timezone.utc)
    last = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    run_id = stamp.isoformat(timespec="milliseconds")
    while last and run_id <= last:
        stamp += dt.timedelta(milliseconds=1)
        run_id = stamp.isoformat(timespec="milliseconds")
    return run_id


def rank_and_report(cfg, conn, session=None, stats: dict | None = None, log=print) -> dict:
    """Rank what is in the database and write the report, changelog and exports."""
    stats = dict(stats or {})
    stats["label_facts_restored"] = restore_cache(cfg, conn)
    result = rank_products(load_products(conn), cfg)
    run_id = new_run_id(conn)
    stats["lists"] = result.counts()
    stats["exclusions"] = result.exclusion_counts()
    save_results(conn, run_id, result, stats)
    snapshot_batches(conn, run_id)
    stats.update(write_reports(cfg, conn, result, run_id, stats))
    stats.update(keep_ranked_pdfs(cfg, conn, result, session, log=log))
    conn.execute("UPDATE runs SET stats = ? WHERE run_id = ?", (__import__("json").dumps(stats, default=str), run_id))
    conn.commit()
    stats["run_id"] = run_id
    return stats


def run_all(cfg, conn, session, offline: bool = False, log=print) -> dict:
    from .scrape.crawl import crawl_listing
    from .scrape.reports import fetch_reports
    stats: dict = {}
    if not offline:
        log("crawl: IFOS listing and product pages")
        stats["crawl"] = crawl_listing(cfg, conn, session, log=log)
        log("fetch: new report PDFs")
        stats["fetch"] = fetch_reports(cfg, conn, session, log=log)
    log("extract: parse reports")
    stats["extract"] = extract_all(cfg, conn, log=log)
    if cfg["llm"]["api_enabled"] and not offline:
        from .agent.runner import run_api_queue
        log("verify: label agent (API)")
        stats["agent"] = run_api_queue(cfg, conn, session, log=log)
    log("rank and report")
    out = rank_and_report(cfg, conn, session=None if offline else session, stats=stats, log=log)
    out["http_requests"] = getattr(session, "request_count", 0)
    return out
