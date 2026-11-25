"""In-session helper: search the text already fetched for a product (no tool call, no network)."""
from __future__ import annotations

import json
import re
import sys

from ..config import load_config
from .session import trace_path


def peek(product_num: str, pattern: str, width: int = 160, limit: int = 12, call: int | None = None) -> list[str]:
    trace = json.loads(trace_path(load_config(), product_num).read_text(encoding="utf-8"))
    out = []
    for c in trace["calls"]:
        page = c.get("page")
        if not page or (call is not None and c["n"] != call):
            continue
        text = page["full_text"]
        for m in re.finditer(pattern, text, re.I):
            out.append(f"[call {c['n']}] ...{text[max(0, m.start() - width):m.end() + width]}...")
            if len(out) >= limit:
                return out
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    call = int(args[3]) if len(args) > 3 else None
    for line in peek(args[0], args[1], int(args[2]) if len(args) > 2 else 160, call=call):
        print(line)
