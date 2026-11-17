"""Stage 1: listing enumeration, product-page parsing, format filter, schema."""
import datetime as dt
import json

import pytest

from omega3 import db
from omega3.http import PoliteSession
from omega3.scrape.crawl import crawl_listing
from omega3.scrape.listing import decide_format, fetch_paged, parse_expiry, parse_product_page

# Expected values transcribed by hand from the fixture HTML.
PAGES = {
    "PLLF0001": dict(name="+Life Omega 3 Complete", brand_id="PLLF", product_type="Softgels", n_lots=3,
                     first=("F2604-0538", dt.date(2028, 4, 30), "Plus LIFE (Nutramis) F2604-0538 17322026 Report Rev 01.pdf")),
    "ACAO0002": dict(name="PURIS DHA PLUS", brand_id="ACAO", product_type="Capsules", n_lots=1,
                     first=("H25", dt.date(2026, 11, 30), "Vitafor H25 31362025 Report Rev1.pdf")),
    "ALTA0001": dict(name="rTG Fish Oil", brand_id="ALTA", product_type="Liquids", n_lots=0, first=None),
    "BARE0004": dict(name="Action Heroes, Pure Omega-3, Daily Liquid", brand_id="BARE", product_type="Liquids", n_lots=3,
                     first=("AHL63895", dt.date(2028, 2, 29), "Bare Biology Ltd. AHL63895 12852026 Report.pdf")),
    "GNCC0011": dict(name="GNC 95% High Purity Fish Oil Mini", brand_id="GNCC", product_type="Softgels", n_lots=26,
                     first=("1069DJ2519", dt.date(2028, 12, 31), "GNC Hong Kong Limited 1069DJ2519 352026 Report.pdf")),
    "WHCC0007": dict(name="UnoCardio CHEWY", brand_id="WHCC", product_type="Chewables", n_lots=60,
                     first=("44006277", dt.date(2028, 8, 31), "WHC Labs BV 44006277 49712026 Product Approval Report.pdf")),
    "DYBT0001": dict(name="Dynes Biotechnology", brand_id="DYBT", product_type="Liquids", n_lots=1,
                     first=("Various SKUs", dt.date(2026, 11, 30), "IFOS Raw Materials Certificate - Dynes- 2026.pdf")),
}


@pytest.mark.parametrize("num", sorted(PAGES))
def test_parse_product_page(fixtures, num):
    expected = PAGES[num]
    page = parse_product_page(num, (fixtures / "pages" / f"{num}.html").read_bytes())
    assert page.name == expected["name"]
    assert page.brand_id == expected["brand_id"]
    assert page.product_type == expected["product_type"]
    assert len(page.lots) == expected["n_lots"]
    if expected["first"]:
        lot = page.lots[0]
        assert (lot.lot, lot.expiry_date, lot.report_filename) == expected["first"]
    assert len(page.page_sha256) == 64


def test_page_fields(fixtures):
    page = parse_product_page("PLLF0001", (fixtures / "pages" / "PLLF0001.html").read_bytes())
    assert page.rda == "5 softgels per day."
    assert page.summary.startswith("+Life Omega 3 Complete is a clean and highly concentrated omega-3 fish oil product.")
    assert "400 mg of EPA" in page.summary
    page = parse_product_page("ACAO0002", (fixtures / "pages" / "ACAO0002.html").read_bytes())
    assert page.website == "http://acaomagistral.org.br"


def test_lot_with_slash_and_all_lots_have_expiry(fixtures):
    page = parse_product_page("PLLF0001", (fixtures / "pages" / "PLLF0001.html").read_bytes())
    assert [l.lot for l in page.lots] == ["F2604-0538", "F2602/0392", "2513007"]
    for num in PAGES:
        for lot in parse_product_page(num, (fixtures / "pages" / f"{num}.html").read_bytes()).lots:
            assert lot.expiry_date is not None, (num, lot)


def test_content_hash_ignores_site_chrome(fixtures):
    html = (fixtures / "pages" / "PLLF0001.html").read_bytes()
    changed_chrome = html.replace(b"GTM-MTJZSM3", b"GTM-OTHER")
    changed_lot = html.replace(b"F2604-0538", b"F2604-0539")
    base = parse_product_page("PLLF0001", html).page_sha256
    assert parse_product_page("PLLF0001", changed_chrome).page_sha256 == base
    assert parse_product_page("PLLF0001", changed_lot).page_sha256 != base


def test_parse_expiry():
    assert parse_expiry("Apr 30, 2028") == dt.date(2028, 4, 30)
    assert parse_expiry("sometime") is None


@pytest.mark.parametrize("ptype,name,rda,decision,chewable", [
    ("Capsules", "Fish Oil", None, "include", False),
    ("Softgels", "Fish Oil", None, "include", False),
    ("Liquids", "rTG Fish Oil", None, "exclude", False),
    ("Gummies", "Omega Gummies", None, "exclude", False),
    ("Food", "Omega Bar", None, "exclude", False),
    (None, "Mystery", None, "exclude", False),
    ("Chewables", "UnoCardio CHEWY", "Children take one (1) chewy a day.", "exclude", True),
    ("Chewables", "Zero Sugar Fish Oil Gummy Chews CHH", None, "exclude", True),
    ("Chewables", "Kids Chewable Softgels", None, "include", True),
    ("Chewables", "Kids Chewable DHA", None, "pending", True),
    ("Chewables", "Kids Chewable DHA", "Chew 2 softgels daily", "include", True),
])
def test_decide_format(cfg, ptype, name, rda, decision, chewable):
    got_decision, reason, got_chewable = decide_format(ptype, name, "", cfg, rda=rda)
    assert (got_decision, got_chewable) == (decision, chewable), reason
    assert reason


def test_fetch_paged_stops_at_total(fake_session_cls, fake_response_cls):
    def handler(url, params):
        page = params["pageNumber"]
        items = [{"ProductNum": f"P{page}{i}"} for i in range(2 if page < 3 else 1)]
        return fake_response_cls(json_data={"totalCount": 5, "list": items})
    session = fake_session_cls(handler)
    items = fetch_paged(session, "http://x/list", "IFOS", 2)
    assert len(items) == 5
    assert [p["pageNumber"] for _, p in session.calls] == [1, 2, 3]
    assert all(p["forCertification"] == "IFOS" for _, p in session.calls)


def _site(fixtures, fake_response_cls, nums, page_overrides=None):
    """A fake Nutrasource: listing + brands + the fixture product pages."""
    page_overrides = page_overrides or {}

    def handler(url, params):
        if "GetFilteredProducts" in url:
            return fake_response_cls(json_data={"totalCount": len(nums), "list": [{"ProductNum": n, "ProductName": n} for n in nums]})
        if "GetFilteredBrands" in url:
            return fake_response_cls(json_data={"totalCount": 2, "list": [{"BrandId": "PLLF", "Name": "+LIFE"}, {"BrandId": "GNCC", "Name": "GNC"}]})
        num = params["id"]
        return fake_response_cls(content=page_overrides.get(num) or (fixtures / "pages" / f"{num}.html").read_bytes())
    return handler


def test_crawl_populates_db_and_second_run_fetches_no_pages(cfg, conn, fixtures, fake_session_cls, fake_response_cls):
    nums = ["PLLF0001", "GNCC0011", "BARE0004", "WHCC0007"]
    session = fake_session_cls(_site(fixtures, fake_response_cls, nums))
    stats = crawl_listing(cfg, conn, session, log=lambda *_: None)
    assert stats["products_listed"] == 4 and stats["pages_fetched"] == 4
    assert stats["format_include"] == 2 and stats["format_exclude"] == 2
    # WHCC0007 lists lot 44004978 twice; the duplicate is counted, not stored.
    assert stats["lots_listed"] == 3 + 26 + 3 + 59 and stats["lots_duplicate_on_page"] == 1

    row = conn.execute("SELECT * FROM products WHERE product_num='PLLF0001'").fetchone()
    assert (row["brand"], row["product_type"], row["format_decision"]) == ("+LIFE", "Softgels", "include")
    assert conn.execute("SELECT COUNT(*) FROM batches WHERE product_num='GNCC0011'").fetchone()[0] == 26
    assert (cfg.data_dir / "raw" / "pages" / "PLLF0001.html").exists()
    listing = json.loads((cfg.data_dir / "raw" / "listing" / "products.json").read_text())
    assert [p["ProductNum"] for p in listing] == sorted(nums)

    # Immediate re-run: pages are inside the refresh window -> no page requests.
    session2 = fake_session_cls(_site(fixtures, fake_response_cls, nums))
    stats2 = crawl_listing(cfg, conn, session2, log=lambda *_: None)
    assert stats2.get("pages_fetched", 0) == 0 and stats2["pages_from_cache"] == 4
    assert all("certified-products/product" not in url for url, _ in session2.calls)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 91  # idempotent


def test_crawl_detects_delisted_lot_and_product(cfg, conn, fixtures, fake_session_cls, fake_response_cls):
    cfg["scrape"]["page_refresh_hours"] = 0  # force re-fetch
    nums = ["PLLF0001", "GNCC0011"]
    crawl_listing(cfg, conn, fake_session_cls(_site(fixtures, fake_response_cls, nums)), log=lambda *_: None)
    first_seen = conn.execute("SELECT first_seen FROM products WHERE product_num='PLLF0001'").fetchone()[0]

    html = (fixtures / "pages" / "PLLF0001.html").read_bytes()
    start = html.index(b'<option value="Vitamin Store  Nutramis')
    end = html.index(b"</option>", start) + len(b"</option>")
    without_lot = html[:start] + html[end:]
    session = fake_session_cls(_site(fixtures, fake_response_cls, ["PLLF0001"], {"PLLF0001": without_lot}))
    stats = crawl_listing(cfg, conn, session, log=lambda *_: None)
    assert stats["lots_delisted"] == 1 and stats["products_delisted"] == 1 and stats["pages_changed"] == 1
    assert conn.execute("SELECT listed FROM batches WHERE product_num='PLLF0001' AND lot='2513007'").fetchone()[0] == 0
    assert conn.execute("SELECT listed FROM products WHERE product_num='GNCC0011'").fetchone()[0] == 0
    # first_seen survives the update
    assert conn.execute("SELECT first_seen FROM products WHERE product_num='PLLF0001'").fetchone()[0] == first_seen


def test_schema_indexes_and_upsert_idempotent(conn):
    indexes = {r[1] for r in conn.execute("SELECT * FROM sqlite_master WHERE type='index'")}
    assert {"idx_batches_lot", "idx_batches_report", "idx_products_brand", "idx_reports_sha"} <= indexes
    row = dict(product_num="X1", name="n", first_seen="2026-01-01", last_checked="2026-01-01")
    db.upsert(conn, "products", row, keys=("product_num",))
    db.upsert(conn, "products", {**row, "name": "n2", "first_seen": "2027-01-01"}, keys=("product_num",))
    got = conn.execute("SELECT name, first_seen FROM products").fetchall()
    assert [(r[0], r[1]) for r in got] == [("n2", "2026-01-01")]


def test_polite_session_rate_limits_and_retries(cfg):
    class Resp:
        def __init__(self, code): self.status_code = code
        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(str(self.status_code))

    class Inner:
        headers = {}
        def __init__(self): self.codes = [503, 200, 200]
        def get(self, url, params=None, timeout=None): return Resp(self.codes.pop(0))

    now = [0.0]
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds); now[0] += seconds
    session = PoliteSession(cfg, session=Inner(), sleep=sleep, clock=lambda: now[0])
    assert session.get("https://h/a").status_code == 200   # 503 then retry
    session.get("https://h/b")
    assert session.request_count == 3
    assert sleeps[0] == cfg["http"]["backoff_seconds"]      # backoff after the 503
    assert cfg["http"]["min_interval_seconds"] in sleeps    # waited before the next request to the same host
    assert Inner.headers["User-Agent"].startswith("omega3-ifos-ranker")


def test_pending_chewable_resolved_by_report_unit(cfg):
    assert decide_format("Chewables", "Kids Chewable DHA", "", cfg, report_unit="mg / softgel")[0] == "include"
    assert decide_format("Chewables", "Kids Chewable DHA", "", cfg, report_unit="mg / chewy")[0] == "exclude"
    assert decide_format("Chewables", "Kids Chewable DHA", "", cfg, report_unit="mg / 5 mL")[0] == "exclude"


MALFORMED = b"""<div><h2>Omega Plus</h2><h3>Product Summary</h3>
<p>High concentration.<br/><p><b>Product instructions:</b>Take 3 capsules daily.</p>
<p><strong>Product Type:</strong> Capsules</p>
<p><strong>Recommended Daily Allowance:</strong> Take one serving (3 capsules) daily.</p>
<p><strong>Website:</strong> <a href="https://x.example/">https://x.example/</a></p></div>
<a href="/certified-products/brand?id=DLSP">b</a>
<select id="ReportIfos"><option value="">--Select--</option>
<option value="RTCP certificate_ Lot A1.pdf">Lot #: A1 (Jul 31, 2028)</option>
<option value="Brand A1 4662025 Report.pdf">Lot #: A1 (Jul 31, 2028)</option>
<option value="RTCP certificate_ Lot B2.pdf">Lot #: B2 (Jul 31, 2028)</option></select>"""


def test_malformed_brand_summary_does_not_swallow_the_fields():
    page = parse_product_page("DLSP0002", MALFORMED)
    assert (page.name, page.product_type, page.website) == ("Omega Plus", "Capsules", "https://x.example/")
    assert page.rda == "Take one serving (3 capsules) daily."
    assert page.summary == "High concentration. Product instructions: Take 3 capsules daily."


def test_duplicate_lot_prefers_the_ifos_report_over_an_rtcp_certificate(cfg, conn, fake_session_cls, fake_response_cls):
    def handler(url, params):
        if "GetFilteredProducts" in url:
            return fake_response_cls(json_data={"totalCount": 1, "list": [{"ProductNum": "DLSP0002", "ProductName": "x"}]})
        if "GetFilteredBrands" in url:
            return fake_response_cls(json_data={"totalCount": 0, "list": []})
        return fake_response_cls(content=MALFORMED)
    stats = crawl_listing(cfg, conn, fake_session_cls(handler), log=lambda *_: None)
    assert stats["lots_listed"] == 2 and stats["lots_duplicate_on_page"] == 1
    files = dict(conn.execute("SELECT lot, report_filename FROM batches").fetchall())
    assert files == {"A1": "Brand A1 4662025 Report.pdf", "B2": "RTCP certificate_ Lot B2.pdf"}
