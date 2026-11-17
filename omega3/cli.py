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


def _agent_http(cfg):
    session = PoliteSession(cfg)
    return session


def cmd_agent_queue(cfg, conn, args) -> dict:
    """List products waiting for label verification, best possible score first."""
    from .agent.runner import candidates, needs_verification
    queue, threshold = candidates(cfg, conn)
    todo = [p for p in queue if needs_verification(cfg, p.product.product_num)]
    in_reach = [p for p in todo if threshold is None or p.optimistic_score <= threshold]
    for p in (todo[: args.limit] if args.limit else todo):
        mark = " " if threshold is None or p.optimistic_score <= threshold else "x"
        need = "form+oil" if p.form is None and p.oil_mg_per_capsule is None and p.epa_dha_mg_g is None else "form" if p.form is None else "oil"
        print(f"{mark} {p.product.product_num} best={p.optimistic_score:7.3f} need={need:8s} {p.product.brand} | {p.product.name} | {p.product.website}")
    return {"queue": len(queue), "to_verify": len(todo), "threshold": threshold, "can_still_reach_top_n": len(in_reach)}


def cmd_agent_start(cfg, conn, args) -> dict:
    """In-session: open a trace for a product and print its briefing."""
    from .agent.session import Session
    session = Session(cfg, conn, args.product, model="in-session", mode="in-session", http=_agent_http(cfg))
    print(session.trace["briefing"])
    return {"product": args.product, "calls_left": session.calls_left}


def cmd_agent_tool(cfg, conn, args) -> dict:
    """In-session: run one tool for a product under the same budget as the API agent."""
    import json as _json
    from .agent import tools
    from .agent.session import Session
    session = Session(cfg, conn, args.product, model="in-session", mode="in-session", http=_agent_http(cfg), resume=True)
    payload = _json.loads(args.args or "{}")
    try:
        if args.tool == "web_search":
            print(session.log_search(payload["query"], payload.get("results", [])))
        else:
            print(session.call(args.tool, payload))
    except tools.ToolError as exc:
        print(str(exc))
    return {"product": args.product, "calls_used": session.calls_used, "calls_left": session.calls_left}


def cmd_agent_record(cfg, conn, args) -> dict:
    """In-session: validate and store decisions (a JSON object keyed by product number)."""
    import json as _json
    from .agent.session import Session
    out = {}
    for product, decision in _json.loads(open(args.file, encoding="utf-8").read()).items():
        session = Session(cfg, conn, product, model="in-session", mode="in-session", resume=True)
        outcome = session.finish(decision, "decision recorded")
        out[product] = {"status": outcome["facts"]["status"], "form": outcome["facts"]["form"],
                        "oil_mg": outcome["facts"]["oil_mg_per_capsule"], "notes": outcome["notes"]}
    return out


def cmd_agent_run(cfg, conn, args) -> dict:
    """API path: work the verification queue with the Claude API (needs llm.api_enabled)."""
    from .agent.runner import run_api_queue
    return run_api_queue(cfg, conn, _agent_http(cfg), limit=args.limit)


def cmd_rank(cfg, conn, args) -> dict:
    """Rank what is already in the database and write the report (no network)."""
    from .pipeline import rank_and_report
    return rank_and_report(cfg, conn)


def cmd_run(cfg, conn, args) -> dict:
    """Everything, incrementally: crawl, fetch, extract, verify (API only), rank, report."""
    from .pipeline import run_all
    return run_all(cfg, conn, PoliteSession(cfg), offline=args.offline)


def cmd_top(cfg, conn, args) -> dict:
    """Print the current top N as a text table."""
    from .load import load_products
    from .rank import rank_products
    from .report import build_context, text_table
    result = rank_products(load_products(conn), cfg)
    print(text_table(build_context(cfg, conn, result, "preview", {})))
    return result.counts()


COMMANDS = {"crawl": cmd_crawl, "fetch": cmd_fetch, "extract": cmd_extract, "rank": cmd_rank, "run": cmd_run, "top": cmd_top,
            "llm-queue": cmd_llm_queue, "llm-record": cmd_llm_record,
            "agent-queue": cmd_agent_queue, "agent-start": cmd_agent_start, "agent-tool": cmd_agent_tool,
            "agent-record": cmd_agent_record, "agent-run": cmd_agent_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omega3", description="Omega-3 IFOS ranking pipeline")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--limit", type=int, default=None, help="process at most N items (debugging)")
    parser.add_argument("--file", default=None, help="input file for commands that take one")
    parser.add_argument("--offline", action="store_true", help="run: skip every network stage")
    parser.add_argument("--product", default=None, help="product number (agent commands)")
    parser.add_argument("--tool", default=None, help="tool name for agent-tool")
    parser.add_argument("--args", default=None, help="JSON arguments for agent-tool")
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
