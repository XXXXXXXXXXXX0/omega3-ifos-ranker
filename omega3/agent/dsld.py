"""In-session helper: show the NIH label database hits and fish-oil facts already fetched for a product."""
from __future__ import annotations

import json
import re
import sys

from ..config import load_config
from .session import trace_path


def fields(text: str) -> dict[str, str]:
    out = {}
    for part in text.split(" | "):
        key, _, value = part.partition(": ")
        out[key] = value
    return out


def show(product_num: str) -> None:
    trace = json.loads(trace_path(load_config(), product_num).read_text(encoding="utf-8"))
    for c in trace["calls"]:
        page = c.get("page") or {}
        if "api.ods.od.nih.gov" not in page.get("url", ""):
            continue
        f = fields(page["full_text"])
        if "search-filter" in page["url"]:
            for i in range(10):
                if f"hits[{i}]._id" not in f:
                    break
                print(f"  hit {f[f'hits[{i}]._id']}: {f.get(f'hits[{i}]._source.brandName')} | {f.get(f'hits[{i}]._source.fullName')} | {f.get(f'hits[{i}]._source.entryDate', '')}")
        else:
            print(f"  label {page['url'].rsplit('/', 1)[1]}: {f.get('brandName')} | {f.get('fullName')}")
            for key, value in f.items():
                if re.search(r"(ingredientRows\[\d+\](\.nestedRows\[\d+\])?\.(name|quantity\[0\]\.(quantity|unit)))$|otheringredients\.ingredients\[\d+\]\.(name|forms\[\d+\]\.name)$|statements\[\d+\]\.notes$|servingSizes\[0\]\.(minQuantity|unit)$|netContents\[0\]\.display$", key):
                    print(f"    {key} = {value[:150]}")


if __name__ == "__main__":
    for product in sys.argv[1:]:
        print(f"## {product}")
        show(product)
