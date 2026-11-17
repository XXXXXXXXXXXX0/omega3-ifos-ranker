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


COMMANDS = {"crawl": cmd_crawl, "fetch": cmd_fetch}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omega3", description="Omega-3 IFOS ranking pipeline")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--limit", type=int, default=None, help="process at most N items (debugging)")
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
