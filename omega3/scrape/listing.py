"""Enumerate IFOS products and parse each product page.

Source: the public JSON endpoint behind certifications.nutrasource.ca's own
product search, plus one HTML page per product. See DECISIONS F-1, F-2.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

LOT_RE = re.compile(r"^\s*Lot\s*#:\s*(?P<lot>.*?)\s*\((?P<expiry>[^()]*)\)\s*$", re.S)
BRAND_LINK_RE = re.compile(r"/certified-products/brand\?id=([A-Za-z0-9]+)")


@dataclass
class Lot:
    lot: str
    expiry_text: str
    expiry_date: dt.date | None
    report_filename: str


@dataclass
class ProductPage:
    product_num: str
    name: str
    brand_id: str | None = None
    summary: str = ""
    product_type: str | None = None
    rda: str | None = None
    website: str | None = None
    lots: list[Lot] = field(default_factory=list)
    page_sha256: str = ""


def fetch_paged(session, url: str, certification: str, page_size: int) -> list[dict]:
    """Page through GetFilteredProducts / GetFilteredBrands until totalCount is reached."""
    items: list[dict] = []
    page = 1
    while True:
        resp = session.get(url, params={"pageNumber": page, "pageSize": page_size, "forCertification": certification, "byName": ""})
        data = resp.json()
        batch = data.get("list") or []
        items.extend(batch)
        if not batch or len(items) >= int(data.get("totalCount") or 0):
            return items
        page += 1


def parse_expiry(text: str) -> dt.date | None:
    """Listing expiry is always 'Mon DD, YYYY'. Anything else is kept as text only."""
    try:
        return dt.datetime.strptime(text.strip(), "%b %d, %Y").date()
    except ValueError:
        return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


FIELD_RE = re.compile(r"<p>\s*<strong>\s*(Product Type|Recommended Daily Allowance|Website)\s*:\s*</strong>(.*?)</p>", re.S | re.I)
PANEL_RE = re.compile(r"<h2[^>]*>(?P<name>.*?)</h2>\s*<h3[^>]*>\s*Product Summary\s*</h3>(?P<body>.*?)</div>", re.S | re.I)


def _text(fragment: str) -> str:
    return _clean(BeautifulSoup(fragment, "html.parser").get_text(" "))


def parse_product_page(product_num: str, html: str | bytes) -> ProductPage:
    raw = html if isinstance(html, bytes) else html.encode("utf-8")
    source = raw.decode("utf-8", "replace")
    soup = BeautifulSoup(raw, "html.parser")
    page = ProductPage(product_num=product_num, name="")

    # The summary is brand-supplied HTML and is sometimes malformed (unclosed
    # <p>), which makes a tree parser swallow the fields that follow it. The
    # labelled fields are therefore cut out of the raw markup by pattern.
    panel = PANEL_RE.search(source)
    if panel:
        page.name = _text(panel.group("name"))
        body = panel.group("body")
        first_field = FIELD_RE.search(body)
        page.summary = _text(body[: first_field.start()] if first_field else body)
        for key, value in FIELD_RE.findall(body):
            value = _text(value) or None
            key = key.lower()
            if key == "product type":
                page.product_type = value
            elif key == "recommended daily allowance":
                page.rda = value
            elif key == "website":
                page.website = value

    match = BRAND_LINK_RE.search(source)
    page.brand_id = match.group(1) if match else None

    select = soup.find("select", id="ReportIfos")
    for option in select.find_all("option") if select else []:
        filename = (option.get("value") or "").strip()
        if not filename:
            continue
        text = _clean(option.get_text())
        m = LOT_RE.match(text)
        lot, expiry_text = (m.group("lot"), m.group("expiry")) if m else (text, "")
        page.lots.append(Lot(lot=lot, expiry_text=expiry_text, expiry_date=parse_expiry(expiry_text), report_filename=filename))
    page.page_sha256 = content_hash(page)
    return page


def content_hash(page: ProductPage) -> str:
    """Hash of the parsed content, not the raw HTML, so site chrome changes
    (scripts, tracking tags) do not look like a product change."""
    canon = json.dumps(
        [page.name, page.brand_id, page.summary, page.product_type, page.rda, page.website,
         [[l.lot, l.expiry_text, l.report_filename] for l in page.lots]],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _has_word(text: str, words: list[str]) -> str | None:
    for word in words:
        if re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", text, re.I):
            return word
    return None


def decide_format(product_type: str | None, name: str, summary: str, cfg, rda: str | None = None, report_unit: str | None = None) -> tuple[str, str, bool]:
    """Capsules-only filter, applied before any PDF is downloaded.

    Returns (decision, reason, chewable) where decision is 'include',
    'exclude' or 'pending'. Chewables are kept (and flagged) only when the
    listing text or the report unit says softgel/capsule (DECISIONS D-2);
    'pending' means the listing is silent and the report unit must be read.
    """
    fmt = cfg["formats"]
    ptype = (product_type or "").strip()
    if ptype in fmt["included"]:
        return "include", f"product type {ptype}", False
    if ptype in fmt["excluded"]:
        return "exclude", f"not a capsule: product type {ptype}", False
    if ptype in fmt["conditional"]:
        # Directions ("chew two softgels") can confirm a softgel but must not
        # trigger the gummy words, so they are only used for the softgel test.
        text = " ".join(filter(None, [name, summary, report_unit]))
        gummy = _has_word(text, fmt["gummy_words"])
        softgel = _has_word(" ".join(filter(None, [text, rda])), fmt["softgel_words"])
        if softgel and not gummy:
            return "include", f"chewable softgel ('{softgel}' in listing/report)", True
        if gummy:
            return "exclude", f"chewable, not a softgel ('{gummy}' in listing/report)", True
        if report_unit is None:
            # Nothing in the listing settles it; the report's unit line might
            # ("mg / softgel"), so download it and decide again afterwards.
            return "pending", "chewable, format to be read from the report unit", True
        return "exclude", "chewable, not stated to be a softgel", True
    return "exclude", f"not a capsule: product type {ptype or 'missing'}", False
