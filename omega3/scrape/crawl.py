"""Stage 1 orchestration: listing -> product pages -> products/batches tables."""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter

from .. import db
from .listing import decide_format, fetch_paged, parse_product_page


def _trim(item: dict) -> dict:
    """Drop null/false fields: lossless for this API, and keeps git diffs small."""
    return {k: v for k, v in item.items() if v not in (None, False, 0, "")}


def _fresh(last_checked: str | None, hours: float) -> bool:
    if not last_checked or hours <= 0:
        return False
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_checked)
    return age < dt.timedelta(hours=hours)


def crawl_listing(cfg, conn, session, log=print) -> dict:
    """Enumerate every IFOS product, refresh its page, upsert products and lots.

    Product pages checked within `scrape.page_refresh_hours` are read from the
    raw cache instead of the network, so an immediate re-run costs only the
    listing requests.
    """
    sc = cfg["scrape"]
    raw_dir = cfg.data_dir / "raw"
    pages_dir = raw_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "listing").mkdir(parents=True, exist_ok=True)

    products = fetch_paged(session, sc["listing_url"], sc["certification"], sc["page_size"])
    brands = fetch_paged(session, sc["brands_url"], sc["certification"], sc["page_size"])
    products.sort(key=lambda p: p["ProductNum"])
    brands.sort(key=lambda b: b["BrandId"])
    for name, items in (("products", products), ("brands", brands)):
        with open(raw_dir / "listing" / f"{name}.json", "w", encoding="utf-8") as fh:
            json.dump([_trim(i) for i in items], fh, ensure_ascii=False, indent=0, sort_keys=True)
    brand_names = {b["BrandId"]: b.get("Name") for b in brands}

    stats = Counter(products_listed=len(products))
    now = db.now_iso()
    known = {r["product_num"]: r for r in conn.execute("SELECT product_num, last_checked, page_sha256 FROM products")}
    seen_products: set[str] = set()

    for i, item in enumerate(products, 1):
        num = item["ProductNum"]
        seen_products.add(num)
        page_file = pages_dir / f"{num}.html"
        prior = known.get(num)
        if prior and page_file.exists() and _fresh(prior["last_checked"], sc["page_refresh_hours"]):
            html = page_file.read_bytes()
            stats["pages_from_cache"] += 1
            checked = prior["last_checked"]
        else:
            try:
                html = session.get(sc["product_url"], params={"id": num}).content
            except Exception as exc:  # keep going; the product stays as last known
                log(f"  page fetch failed for {num}: {exc}")
                stats["page_errors"] += 1
                continue
            page_file.write_bytes(html)
            stats["pages_fetched"] += 1
            checked = now

        page = parse_product_page(num, html)
        if not page.name:
            page.name = item.get("ProductName") or num
        decision, reason, chewable = decide_format(page.product_type, page.name, page.summary, cfg, rda=page.rda)
        stats[f"format_{decision}"] += 1
        if prior and prior["page_sha256"] != page.page_sha256:
            stats["pages_changed"] += 1
        db.upsert(conn, "products", {
            "product_num": num, "brand_id": page.brand_id, "brand": brand_names.get(page.brand_id or num[:4]),
            "name": page.name, "summary": page.summary, "product_type": page.product_type, "rda": page.rda,
            "website": page.website, "page_sha256": page.page_sha256, "listed": 1,
            "format_decision": decision, "format_reason": reason, "chewable": int(chewable),
            "first_seen": now, "last_checked": checked,
        }, keys=("product_num",))

        listed_lots = set()
        for lot in page.lots:
            if lot.lot in listed_lots:
                # The site sometimes lists one lot twice (seen with two expiry
                # dates for the same report). Keep the first, count the rest.
                stats["lots_duplicate_on_page"] += 1
                continue
            listed_lots.add(lot.lot)
            db.upsert(conn, "batches", {
                "product_num": num, "lot": lot.lot,
                "expiry_date": lot.expiry_date.isoformat() if lot.expiry_date else None,
                "expiry_text": lot.expiry_text, "report_filename": lot.report_filename,
                "listed": 1, "first_seen": now, "last_seen": now,
            }, keys=("product_num", "lot"))
            stats["lots_listed"] += 1
        # Lots that were listed before and are gone now (expired or withdrawn).
        for row in conn.execute("SELECT lot FROM batches WHERE product_num=? AND listed=1", (num,)).fetchall():
            if row["lot"] not in listed_lots:
                conn.execute("UPDATE batches SET listed=0 WHERE product_num=? AND lot=?", (num, row["lot"]))
                stats["lots_delisted"] += 1
        if i % 100 == 0:
            conn.commit()
            log(f"  {i}/{len(products)} products")

    for num in set(known) - seen_products:
        conn.execute("UPDATE products SET listed=0 WHERE product_num=?", (num,))
        conn.execute("UPDATE batches SET listed=0 WHERE product_num=?", (num,))
        stats["products_delisted"] += 1
    conn.commit()
    return dict(stats)
