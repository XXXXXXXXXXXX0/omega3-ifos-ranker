"""Command line entry point: python -m omega3 <command>."""
from __future__ import annotations

import argparse
import json

from . import db
from .config import load_config
from .http import PoliteSession


def cmd_crawl(cfg, conn, args) -> dict:
    from .scrape.crawl import crawl_listing
    session = PoliteSession(cfg)
    stats = crawl_listing(cfg, conn, session)
    stats["http_requests"] = session.request_count
    return stats


def cmd_fetch(cfg, conn, args) -> dict:
    from .scrape.reports import fetch_reports
    session = PoliteSession(cfg)
    stats = fetch_reports(cfg, conn, session, limit=args.limit)
    stats["http_requests"] = session.request_count
    return stats


def cmd_extract(cfg, conn, args) -> dict:
    from .extract.run import extract_all
    return extract_all(cfg, conn)


def cmd_llm_queue(cfg, conn, args) -> dict:
    """Print the reports waiting for the LLM fallback (in-session mode)."""
    from .extract import llm
    from .extract.run import load_doc
    queue = llm.read_queue(cfg)
    for item in queue[: args.limit] if args.limit else queue:
        print(f"===== sha256={item['sha256']}\n      file={item['filename']}\n      parser said: {item['reason']}")
        print(load_doc(cfg, item["sha256"]).text)
    return {"queued": len(queue)}


def cmd_llm_record(cfg, conn, args) -> dict:
    """Save in-session transcriptions (a JSON list of payloads) to the LLM cache."""
    import json as _json
    from .extract import llm
    payloads = _json.loads(open(args.file, encoding="utf-8").read())
    queued = {item["sha256"] for item in llm.read_queue(cfg)}
    saved = 0
    for payload in payloads:
        if payload["sha256"] not in queued:
            raise SystemExit(f"{payload['sha256']} is not in the LLM queue")
        llm.save_cached(cfg, payload["sha256"], payload, llm.IN_SESSION_MODEL)
        saved += 1
    return {"saved": saved}


COMMANDS = {"crawl": cmd_crawl, "fetch": cmd_fetch, "extract": cmd_extract,
            "llm-queue": cmd_llm_queue, "llm-record": cmd_llm_record}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omega3", description="Omega-3 IFOS ranking pipeline")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--limit", type=int, default=None, help="process at most N items (debugging)")
    parser.add_argument("--file", default=None, help="input file for commands that take one")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    try:
        stats = COMMANDS[args.command](cfg, conn, args)
    finally:
        conn.commit()
        conn.close()
    print(json.dumps(stats, indent=1, default=str))
    return 0
