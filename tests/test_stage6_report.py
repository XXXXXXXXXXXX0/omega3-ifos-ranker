"""Stage 6: end-to-end run on real fixture PDFs, report content, changelog, idempotence."""
import datetime as dt
import json

import pytest

from omega3 import db
from omega3.agent.session import cache_path
from omega3.pipeline import rank_and_report, run_all

# Listing-page markup in the site's own shape, for products whose page is not a stored fixture.
PAGE = """<html><body><a href="/certified-products/brand?id={brand}">b</a>
<div><h2>{name}</h2><h3>Product Summary</h3><p>{summary}</p>
<p><strong>Product Type:</strong> {ptype}</p><p><strong>Recommended Daily Allowance:</strong> {rda}</p></div>
<select id="ReportIfos"><option value="">--Select--</option>{options}</select></body></html>"""

PRODUCTS = {  # product -> (name, summary, type, rda, [(lot, expiry text, fixture pdf)])
    "BEBI0001": ("Joy Fish Oil Concentrate 85% omega-3 rTG", "", "Softgels", "Take 2 softgels daily", [("JFO202501", "May 05, 2027", "BEBI0001_per_2_softgels_rtg")]),
    "CERN0001": ("Clinical Omega 3X", "Triglyceride form fish oil.", "Softgels", "Take one gelcap daily", [("13981", "May 15, 2027", "CERN0001_category_2024")]),
    "BLKC0001": ("Blackmores Omega Ultra", "Concentrated fish oil.", "Softgels", "Take 1 softgel", [("324381", "Dec 03, 2026", "BLKC0001_ddmmyyyy")]),
    "PIFT0001": ("Pink Fit Omega 3 rTG", "", "Softgels", "Take 5 softgels daily.", [("C522504001", "Mar 31, 2028", "PIFT0001_per_5_softgels")]),
    "PLLF0001": ("+Life Omega 3 Complete TG", "", "Softgels", "5 softgels per day.", [("F2604-0538", "Apr 30, 2028", "PLLF0001_per_cap")]),
    "VETW0002": ("vetwish anchovy pet fish oil for cat", "", "Capsules", "1 capsule per day", [("20241120", "Nov 19, 2026", "VETW0002_pet_per_cap")]),
    "BARE0004": ("Action Heroes Daily Liquid", "", "Liquids", "1 ml", [("AHL59675", "May 27, 2027", "BARE0004_liquid_per_g")]),
    "DYBT0001": ("Dynes Biotechnology", "", "Capsules", "", [("Various SKUs", "Nov 30, 2026", "DYBT0001_raw_material_cert")]),
}
LABELS = {  # what the verification agent would have stored
    "BEBI0001": dict(status="verified", form=None, oil_mg_per_capsule=600.0, oil_evidence="Fish oil concentrate 1,200 mg", oil_source_url="https://brand.example/joy"),
    "CERN0001": dict(status="verified", form="TG", form_evidence="triglyceride form", form_source_url="https://cn.example/x", oil_mg_per_capsule=1000.0,
                     oil_evidence="Fish oil 1000 mg", oil_source_url="https://cn.example/x", antioxidants=["mixed tocopherols"]),
    "PIFT0001": dict(status="verified", form=None, oil_mg_per_capsule=1000.0, oil_evidence="1000 mg", oil_source_url="https://pf.example/x", flavored=True, flavor_evidence="lemon flavor"),
}


class Site:
    """A fake Nutrasource built from the fixture PDFs. Counts every request."""

    def __init__(self, fixtures, fake_response_cls, products=None):
        self.fixtures, self.resp = fixtures, fake_response_cls
        self.products = dict(products or PRODUCTS)
        self.calls, self.request_count = [], 0

    def get(self, url, params=None, **kw):
        self.calls.append(url)
        self.request_count += 1
        params = params or {}
        if "GetFilteredProducts" in url:
            return self.resp(json_data={"totalCount": len(self.products), "list": [{"ProductNum": n, "ProductName": v[0]} for n, v in self.products.items()]})
        if "GetFilteredBrands" in url:
            return self.resp(json_data={"totalCount": 2, "list": [{"BrandId": "BEBI", "Name": "Better Bioscience"}, {"BrandId": "CERN", "Name": "Certified Naturals"}]})
        if "certified-products/product" in url:
            name, summary, ptype, rda, lots = self.products[params["id"]]
            options = "".join(f'<option value="{pdf}.pdf">Lot #: {lot} ({exp})</option>' for lot, exp, pdf in lots)
            return self.resp(content=PAGE.format(brand=params["id"][:4], name=name, summary=summary, ptype=ptype, rda=rda, options=options).encode())
        name = url.rsplit("/", 1)[1]
        path = self.fixtures / "pdf" / name
        if path.exists():
            return self.resp(content=path.read_bytes(), headers={"Content-Type": "application/pdf"})
        r = self.resp(status=404)
        r.raise_for_status()


def write_labels(cfg, labels=LABELS):
    base = dict(form=None, form_source_url=None, form_evidence=None, oil_mg_per_capsule=None, oil_source_url=None, oil_evidence=None,
                serving_capsules=None, servings_per_bottle=None, flavored=None, flavor_evidence=None, antioxidants=[], packaging=[],
                reasoning="", sources_checked=[], page_hashes={}, model="in-session", tool_calls=2, cost_usd=0.0, verified_at="2026-11-17T00:00:00+00:00")
    for num, facts in labels.items():
        cache_path(cfg, num).write_text(json.dumps({"product_num": num, **base, **facts}))


@pytest.fixture
def ran(cfg, conn, fixtures, fake_response_cls):
    # These scenarios were built on the oil-weight score; the per-capsule default
    # has its own end-to-end test below (D-35).
    cfg["scoring"]["basis"] = "oil_weight"
    cfg["as_of_date"] = "2026-11-17"
    write_labels(cfg)
    site = Site(fixtures, fake_response_cls)
    stats = run_all(cfg, conn, site, log=lambda *_: None)
    return site, stats


def results(conn, run_id):
    return {r["product_num"]: (r["list_name"], r["rank"]) for r in conn.execute("SELECT * FROM results WHERE run_id = ?", (run_id,))}


def test_end_to_end_run_places_every_product(cfg, conn, ran):
    site, stats = ran
    assert stats["crawl"]["products_listed"] == 8 and stats["fetch"]["reports_ok"] == 7     # the liquid's report is never downloaded
    assert not any(url.endswith("BARE0004_liquid_per_g.pdf") for url in site.calls)
    assert stats["extract"]["parser_ok"] == 6 and stats["extract"]["parser_no_results"] == 1 and stats["extract"]["llm_queue"] == 0
    placed = results(conn, stats["run_id"])
    assert placed == {
        "BEBI0001": ("ranked", 1),            # score 10.34, tied with CERN (10.04): lower AV (3.0 vs 5) goes first
        "CERN0001": ("ranked", 2),
        "PIFT0001": ("excluded", None),       # AV 10.53 and TOTOX 11.09
        "BLKC0001": ("form_unverified", None),
        "PLLF0001": ("excluded", None),       # TOTOX 13.63, PV 5
        "VETW0002": ("pet", None),
        "BARE0004": ("excluded", None),
        "DYBT0001": ("excluded", None),
    }
    reasons = dict(conn.execute("SELECT product_num, reason FROM results WHERE run_id = ?", (stats["run_id"],)).fetchall())
    assert reasons["PLLF0001"].startswith("oxidation cutoff: TOTOX 13.63") and "PV 5 is above 3" in reasons["PLLF0001"]
    assert reasons["BARE0004"] == "not a capsule: product type Liquids"
    assert reasons["DYBT0001"] == "raw-material certificate, no batch results"
    assert reasons["BLKC0001"].startswith("form unverified, not attempted")
    assert stats["lists"]["ranked"] == 2 and stats["exclusions"]["oxidation cutoff"] == 2


def test_report_has_every_column_list_and_reasoning(cfg, conn, ran):
    _, stats = ran
    md = (cfg.reports_dir / "2026-11-17" / "report.md").read_text(encoding="utf-8")
    for column in ("Rank", "Oxidation load", "TOTOX", "PV", "AV", "Acid value", "EPA+DHA mg/capsule", "mg/serving", "mg/g oil",
                   "EPA:DHA", "Report date", "Expiry", "Flags"):
        assert f"| {column} |" in md or f" {column} |" in md, column
    for heading in ("## Top 10", "### Why each product is here", "## Flavored (ranked separately)", "## Form unverified", "## Monoglyceride",
                    "## Incomplete data", "## Inconsistent batches", "## Oil weight unavailable", "## Pet products", "## Exclusion counts by reason"):
        assert heading in md, heading
    row = next(line for line in md.splitlines() if line.startswith("| 1 | Better Bioscience"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    # rank, brand, product, lot, load, TOTOX, PV, AV, acid, mg/cap, mg/serving, mg/g, EPA:DHA, form, report date, expiry
    assert cells[:16] == ["1", "Better Bioscience", "Joy Fish Oil Concentrate 85% omega-3 rTG", "JFO202501", "10.34", "8.6", "2.8", "3", "0.17",
                          "499", "998", "832", "2.90", "rTG", "2025-09-08", "2027-05-05"]
    assert "Oxidation load 10.34 = TOTOX 8.6 x 1.20 g of oil needed for 1000 mg EPA+DHA (832 mg per g of oil)." in md
    assert "tied with #2, ordered by lower AV" in md and "above the preferred tier of 8" in md
    assert "Oil weight 600 mg per capsule from the label." in md and 'Form rTG per IFOS listing' in md
    assert "5.6 ★" in md                                                       # CERN: preferred tier
    assert "**Top 10 status: incomplete.** Only 2 product(s)" in md
    assert "Agent API cost: $0 (in-session mode" in md and "IFOS's own TOTOX limit is 19.5" in md
    assert "| oxidation cutoff | 2 |" in md and "| **Total** | **8** |" in md
    assert "vetwish anchovy pet fish oil for cat" in md.split("## Pet products")[1]

    html = (cfg.reports_dir / "2026-11-17" / "report.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "Joy Fish Oil Concentrate 85% omega-3 rTG" in html and "10.34" in html
    assert "<script" not in html and "<details>" in html
    excluded = html.split("Excluded products")[1]
    assert "+Life Omega 3 Complete TG" in excluded and "oxidation cutoff: TOTOX 13.63" in excluded
    assert "+Life Omega 3 Complete TG" in md.split("Excluded products")[1]
    assert (cfg.reports_dir / "latest" / "report.html").read_text(encoding="utf-8") == html


def test_exports_and_kept_pdfs(cfg, conn, ran, pdf_manifest):
    import csv
    rows = list(csv.DictReader(open(cfg.data_dir / "exports" / "results.csv", encoding="utf-8")))
    assert len(rows) == 8 and rows[0]["product_num"] == "BEBI0001" and rows[0]["rank"] == "1" and rows[0]["score"] == "10.341"
    batches = list(csv.DictReader(open(cfg.data_dir / "exports" / "batches.csv", encoding="utf-8")))
    joy = next(b for b in batches if b["product_num"] == "BEBI0001")
    assert (joy["epa_orig"], joy["epa_mg_cap"], joy["conversion"]) == ("742.0", "371.0", "divided by 2 (mg / 2 softgels to mg per capsule)")
    labels = list(csv.DictReader(open(cfg.data_dir / "exports" / "label_facts.csv", encoding="utf-8")))
    assert {l["product_num"] for l in labels} == set(LABELS)
    kept = {p.stem for p in (cfg.data_dir / "raw" / "pdf_kept").glob("*.pdf")}       # raw PDFs of ranked batches only (D-5)
    assert kept == {pdf_manifest["BEBI0001_per_2_softgels_rtg.pdf"]["sha256"], pdf_manifest["CERN0001_category_2024.pdf"]["sha256"]}


def test_first_changelog_is_a_baseline(cfg, conn, ran):
    _, stats = ran
    text = open(stats["changelog"], encoding="utf-8").read()
    assert "First run: this is the baseline" in text and "Totals: 2 ranked" in text and "## New batches" not in text


def test_second_run_makes_no_network_calls_and_changes_nothing(cfg, conn, ran, fixtures, fake_response_cls):
    _, first = ran
    again = Site(fixtures, fake_response_cls)
    second = run_all(cfg, conn, again, log=lambda *_: None)
    assert again.calls == [] and second["http_requests"] == 0
    assert second["crawl"] == {"crawl_skipped_fresh": 1, "products_listed": 8} and second["fetch"] == {"reports_pending": 0}
    assert results(conn, second["run_id"]) == results(conn, first["run_id"])
    assert (second["new_batches"], second["gone_batches"], second["ranking_changes"]) == (0, 0, 0)
    text = open(second["changelog"], encoding="utf-8").read()
    assert f"Compared with run `{first['run_id']}`" in text and text.count("None.") == 3
    scores = lambda run: conn.execute("SELECT product_num, score FROM results WHERE run_id = ? ORDER BY 1", (run,)).fetchall()  # noqa: E731
    assert [tuple(r) for r in scores(first["run_id"])] == [tuple(r) for r in scores(second["run_id"])]


def test_changelog_reports_new_expired_and_moved(cfg, conn, ran, fixtures, fake_response_cls):
    _, first = ran
    # Three months later: a new lot for CERN, BLKC's lot has expired off the listing, VETW is gone, Joy's label changed.
    cfg["as_of_date"] = "2027-02-17"
    db.set_meta(conn, "crawled_at", "2020-01-01T00:00:00+00:00")           # the refresh window uses the real clock
    conn.execute("UPDATE products SET last_checked = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    later = dict(PRODUCTS)
    later["CERN0001"] = (*PRODUCTS["CERN0001"][:4], [("13981", "May 15, 2027", "CERN0001_category_2024"), ("14000", "Jan 31, 2029", "BLKC0001_ddmmyyyy")])
    later["BLKC0001"] = (*PRODUCTS["BLKC0001"][:4], [])
    del later["VETW0002"]
    # The new CERN lot carries 1510 mg omega-3 per softgel, so its label weight is larger too.
    write_labels(cfg, {**LABELS, "BEBI0001": {**LABELS["BEBI0001"], "oil_mg_per_capsule": 900.0},
                       "CERN0001": {**LABELS["CERN0001"], "oil_mg_per_capsule": 1700.0}})
    second = run_all(cfg, conn, Site(fixtures, fake_response_cls, later), log=lambda *_: None)
    text = open(second["changelog"], encoding="utf-8").read()
    assert second["new_batches"] == 1 and "Clinical Omega 3X (CERN0001), lot 14000, expires 2029-01-31" in text
    assert second["gone_batches"] == 2
    assert "Blackmores Omega Ultra (BLKC0001), lot 324381, expiry 2026-12-03: expired" in text
    assert "vetwish anchovy pet fish oil for cat (VETW0002), lot 20241120, expiry 2026-11-19: expired" in text
    placed = results(conn, second["run_id"])
    assert placed["CERN0001"] == ("ranked", 1) and placed["BEBI0001"] == ("ranked", 2) and "VETW0002" not in placed
    assert "**Better Bioscience Joy Fish Oil Concentrate 85% omega-3 rTG** (BEBI0001): Ranked #1 -> Ranked #2" in text
    assert "(CERN0001): Ranked #2 -> Ranked #1 (lot 13981 -> 14000)" in text        # newer report date wins
    assert "(BLKC0001): Form unverified -> Excluded" in text and "(VETW0002): Pet products -> no longer listed" in text
    assert len(list((cfg.reports_dir / "changelogs").glob("*.md"))) == 2            # every run keeps its own changelog


def test_rank_command_works_without_network(cfg, conn, ran):
    out = rank_and_report(cfg, conn)
    assert out["lists"]["ranked"] == 2 and out["label_facts_restored"] == 3


def test_text_table(cfg, conn, ran):
    from omega3.load import load_products
    from omega3.rank import rank_products
    from omega3.report import build_context, text_table
    table = text_table(build_context(cfg, conn, rank_products(load_products(conn), cfg), "x", {}))
    lines = table.splitlines()
    assert lines[0].split()[:4] == ["#", "Load", "TOTOX", "PV"] and lines[2].startswith("1  10.34  8.6")
    assert "Better Bioscience / Joy Fish Oil" in lines[2] and len(lines) == 4


def test_report_renders_inconsistent_batches(cfg, conn, fixtures, fake_response_cls):
    # Blackmores with an older, failing lot (ACAO0002 report from 2025: TOTOX 14.1) and a verified label.
    cfg["as_of_date"] = "2026-11-17"
    products = dict(PRODUCTS)
    products["BLKC0001"] = ("Blackmores Omega Ultra rTG", "", "Softgels", "Take 1 softgel",
                            [("324381", "Dec 03, 2026", "BLKC0001_ddmmyyyy"), ("OLD1", "Dec 31, 2027", "ACAO0002_split_header_mmYYYY")])
    write_labels(cfg, {**LABELS, "BLKC0001": dict(status="verified", form=None, oil_mg_per_capsule=1800.0, oil_evidence="1800 mg", oil_source_url="https://b.example")})
    stats = run_all(cfg, conn, Site(fixtures, fake_response_cls, products), log=lambda *_: None)
    md = (cfg.reports_dir / "2026-11-17" / "report.md").read_text(encoding="utf-8")
    section = md.split("## Inconsistent batches")[1].split("##")[0]
    assert "Blackmores Omega Ultra rTG" in section and "5.4 to 14.1" in section and "inconsistent batches: 1 of 1 other" in section
    assert "inconsistent batches" in (cfg.reports_dir / "2026-11-17" / "report.html").read_text(encoding="utf-8")


def test_end_to_end_per_capsule_default(cfg, conn, fixtures, fake_response_cls):
    assert cfg["scoring"]["basis"] == "per_capsule"
    cfg["as_of_date"] = "2026-11-17"
    write_labels(cfg)
    stats = run_all(cfg, conn, Site(fixtures, fake_response_cls), log=lambda *_: None)
    placed = results(conn, stats["run_id"])
    # CERN: TOTOX 5.6, 558 mg EPA+DHA per capsule -> 10.04. BEBI: TOTOX 8.6, 499 mg -> 17.23.
    # 17.23 is more than 10% above 10.04, so they are not tied and CERN goes first.
    assert placed["CERN0001"] == ("ranked", 1) and placed["BEBI0001"] == ("ranked", 2)
    scores = dict(conn.execute("SELECT product_num, score FROM results WHERE run_id = ?", (stats["run_id"],)).fetchall())
    assert scores["CERN0001"] == pytest.approx(5.6 * 1000 / 558, abs=1e-3) and scores["BEBI0001"] == pytest.approx(8.6 * 1000 / 499, abs=1e-3)
    md = (cfg.reports_dir / "2026-11-17" / "report.md").read_text(encoding="utf-8")
    assert "Oxidation load = TOTOX x the number of capsules needed for 1000 mg EPA+DHA" in md
    assert "Oxidation load 17.23 = TOTOX 8.6 x 2.00 capsules needed for 1000 mg EPA+DHA (499 mg per capsule, measured by IFOS)." in md
    assert "(shown for reference; not used in the score)" in md
