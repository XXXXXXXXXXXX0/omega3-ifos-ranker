"""In-session helper: the routine opening moves of a label check, and a short
digest of what came back.

Nothing here decides anything. `explore` spends at most two ordinary tool calls
through the product's Session (the brand site from the IFOS listing, then its
best same-site link), so the calls are budgeted and traced like any other.
`digest` then prints only the sentences that bear on the questions, to keep
the reading short; the full page text stays in the trace.
"""
from __future__ import annotations

import json
import re

from ..extract.terms import find_form_terms
from . import tools
from .session import Session

OIL_RE = re.compile(r"(fish\s+oil|marine\s+oil|oil\s+concentrate|omega[\s-]?3\s+(?:fish\s+)?oil|anchovy\s+oil|concentrate|softgel\s+contains|capsule\s+contains|"
                    r"serving\s+size|servings?\s+per|supplement\s+facts|amount\s+per)[^.|]{0,90}?\d[\d,.]*\s*(?:mg|g)\b|\d[\d,.]*\s*(?:mg|g)\s+(?:of\s+)?(?:fish|marine)\s+oil", re.I)
EXTRA_RE = re.compile(r"(tocopherol|vitamin\s+e\b|rosemary|astaxanthin|antioxidant|flavou?r|lemon|orange|blister|nitrogen|amber|opaque|other\s+ingredients)", re.I)


def _windows(text: str, spans: list[tuple[int, int]], pad: int, limit: int) -> list[str]:
    out, last_end = [], -1
    for start, end in sorted(spans):
        if start <= last_end:
            continue
        a, b = max(0, start - pad), min(len(text), end + pad)
        out.append(text[a:b].strip())
        last_end = b
        if sum(map(len, out)) > limit:
            break
    return out


def digest(page: dict, limit: int = 1400) -> dict:
    """The parts of a fetched page that mention oil form, oil weight or label extras."""
    text = page["full_text"]
    form_spans = []
    for phrases in find_form_terms(text).values():
        for phrase in phrases:
            form_spans += [m.span() for m in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", text)][:4]
    return {
        "url": page["url"], "title": page.get("title", ""), "chars": len(text),
        "form": _windows(text, form_spans, 110, limit // 2),
        "oil": _windows(text, [m.span() for m in OIL_RE.finditer(text)], 60, limit // 2),
        "extras": _windows(text, [m.span() for m in EXTRA_RE.finditer(text)], 50, limit // 4)[:4],
    }


def normalize_site(website: str | None) -> str | None:
    site = (website or "").strip()
    if not site or " " in site:
        return None
    return site if re.match(r"^https?://", site, re.I) else "https://" + site.lstrip("/")


def explore(cfg, conn, http, product_num: str, follow: bool = True) -> dict:
    """Open a session and make the standard first one or two fetches."""
    session = Session(cfg, conn, product_num, model="in-session", mode="in-session", http=http)
    row = conn.execute("SELECT name, brand, website FROM products WHERE product_num = ?", (product_num,)).fetchone()
    out = {"product": product_num, "brand": row["brand"], "name": row["name"], "website": row["website"], "pages": [], "errors": []}
    url = normalize_site(row["website"])
    if url is None:
        out["errors"].append("no brand website in the IFOS listing")
        return out
    links = []
    try:
        session.call("fetch_page", {"url": url})
        page = session.trace["calls"][-1]["page"]
        out["pages"].append(digest(page))
        links = json.loads(session.trace["calls"][-1]["output"].split("\n", 1)[1])["links"]
    except tools.ToolError as exc:
        out["errors"].append(f"{url}: {exc}")
    # Follow the best same-site link only when it actually names the product.
    words = session.product_words
    best = next((l for l in links if sum(w in f"{l['url']} {l['text']}".lower() for w in words) >= max(1, min(2, len(words)))), None)
    if follow and best:
        try:
            session.call("fetch_page", {"url": best["url"]})
            out["pages"].append(digest(session.trace["calls"][-1]["page"]))
        except tools.ToolError as exc:
            out["errors"].append(f"{best['url']}: {exc}")
    out["links"] = [l["url"] for l in links[:8]]
    out["calls_left"] = session.calls_left
    return out
