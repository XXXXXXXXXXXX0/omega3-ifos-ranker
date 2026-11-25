"""In-session helper: list same-site links already collected for a product, filtered by a pattern."""
import json
import re
import sys

from ..config import load_config
from .session import trace_path

product, pattern = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "."
trace = json.loads(trace_path(load_config(), product).read_text(encoding="utf-8"))
seen = set()
for c in trace["calls"]:
    if c["tool"] == "fetch_page" and not c.get("is_error"):
        for link in json.loads(c["output"].split("\n", 1)[1])["links"]:
            if link["url"] not in seen and re.search(pattern, f"{link['url']} {link['text']}", re.I):
                seen.add(link["url"])
                print(link["url"], "|", link["text"][:60])
