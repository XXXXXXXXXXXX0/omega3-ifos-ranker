"""In-session helper: run several budgeted fetches and print compact snippets.

Usage: python -m omega3.agent.batch PRODUCT=URL [PRODUCT=URL ...]
Each fetch is one ordinary tool call on that product's Session (same budget,
same trace). Output is short on purpose; the full text stays in the trace.
"""
from __future__ import annotations

import json
import re
import sys

from .. import db
from ..config import load_config
from ..extract.terms import find_form_terms
from ..http import PoliteSession
from . import tools
from .explore import OIL_RE
from .session import Session, trace_path

PANEL_RE = re.compile(r"(supplement facts|nutrition facts|información nutricional|informação nutricional|ingredients?:|ingredientes|serving size|tamaño de la porción|porción|each (?:softgel|capsule) contains|cada cápsula|per (?:softgel|capsule)|por cápsula)", re.I)


def snippets(text: str, regex: re.Pattern, width: int, limit: int) -> list[str]:
    out, last = [], -10**9
    for m in regex.finditer(text):
        if m.start() - last < width:
            continue
        last = m.start()
        out.append(text[max(0, m.start() - width // 2): m.end() + width].replace("\n", " "))
        if len(out) >= limit:
            break
    return out


def run(pairs: list[tuple[str, str]], width: int = 170) -> None:
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    http = PoliteSession(cfg)
    for product, url in pairs:
        resume = trace_path(cfg, product).exists() and not json.loads(trace_path(cfg, product).read_text())["finished_at"]
        session = Session(cfg, conn, product, model="in-session", mode="in-session", http=http, resume=resume)
        try:
            session.call("fetch_page", {"url": url})
        except tools.ToolError as exc:
            print(f"### {product} {url}\n  ERROR {exc} | calls left {session.calls_left}")
            continue
        page = session.trace["calls"][-1]["page"]
        text = page["full_text"]
        terms = find_form_terms(text)
        print(f"### {product} [{session.calls_used}/{cfg['agent']['max_tool_calls']}] {page['url']} | {page.get('title', '')[:70]} | {len(text)} chars")
        print(f"  form terms: {dict((k, v[:4]) for k, v in terms.items())}")
        for label, regex in (("FORM", None), ("OIL", OIL_RE), ("PANEL", PANEL_RE)):
            if regex is None:
                phrases = [p for v in terms.values() for p in v][:3]
                found = []
                for phrase in phrases:
                    found += snippets(text, re.compile(re.escape(phrase)), width, 1)
            else:
                found = snippets(text, regex, width, 3)
            for s in found[:3]:
                print(f"  {label}: {s}")


if __name__ == "__main__":
    run([tuple(arg.split("=", 1)) for arg in sys.argv[1:]])
