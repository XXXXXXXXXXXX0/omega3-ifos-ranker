"""Stage 2: report download, hashing, manifest, text layer, skip logic."""
import datetime as dt
import hashlib

import pytest

from omega3 import db
from omega3.extract.pdftext import PdfDoc, parse_pdf_date, read_pdf
from omega3.scrape.reports import fetch_reports, pending_reports, report_url, store_report


def _seed(conn, product_num="PLLF0001", decision="include", lots=(("L1", "2099-01-01", "a.pdf"),), listed=1):
    now = db.now_iso()
    db.upsert(conn, "products", dict(product_num=product_num, name="p", format_decision=decision, listed=listed,
                                     first_seen=now, last_checked=now), keys=("product_num",))
    for lot, expiry, filename in lots:
        db.upsert(conn, "batches", dict(product_num=product_num, lot=lot, expiry_date=expiry, report_filename=filename,
                                        listed=1, first_seen=now, last_seen=now), keys=("product_num", "lot"))
    conn.commit()


def test_fixture_hashes_match_manifest(fixtures, pdf_manifest):
    assert len(pdf_manifest) == 13
    for name, item in pdf_manifest.items():
        data = (fixtures / "pdf" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"], name
        assert len(data) == item["bytes"]


def test_read_pdf_text_tables_and_metadata(fixtures):
    doc = read_pdf(fixtures / "pdf" / "PLLF0001_per_cap.pdf")
    assert len(doc.pages) == 2
    assert "Batch /Lot #: F2604-0538" in doc.pages[0]
    assert "TOTOX <= 19.5 13.63 Yes" in doc.pages[1]
    assert doc.created == dt.date(2026, 5, 4)
    assert "Word" in doc.producer
    cells = [c for table in doc.tables[1] for row in table for c in row if c]
    assert "13.63" in cells and "mg per cap" in cells
    # The committed JSON round-trips losslessly.
    again = PdfDoc.from_json(doc.to_json())
    assert again.pages == doc.pages and again.tables == doc.tables and again.created == doc.created


def test_parse_pdf_date():
    assert parse_pdf_date("D:20241108153756-05'00'") == dt.date(2024, 11, 8)
    assert parse_pdf_date("D:20241399") is None
    assert parse_pdf_date(None) is None


def test_report_url_is_percent_encoded(cfg):
    url = report_url(cfg, "Plus LIFE (Nutramis) F2604-0538 #1 Report.pdf")
    assert url == "https://andi.nutrasource.ca/files/Plus%20LIFE%20%28Nutramis%29%20F2604-0538%20%231%20Report.pdf"


def test_store_report_writes_manifest_text_and_pdf(cfg, conn, fixtures, pdf_manifest):
    data = (fixtures / "pdf" / "ACAO0002_split_header_mmYYYY.pdf").read_bytes()
    row = store_report(cfg, conn, "x.pdf", data, {"Last-Modified": "Wed, 10 Sep 2025 20:13:33 GMT", "ETag": '"abc"'}, "http://u/x.pdf")
    sha = pdf_manifest["ACAO0002_split_header_mmYYYY.pdf"]["sha256"]
    assert row["status"] == "ok" and row["sha256"] == sha
    assert (row["report_date"], row["report_date_source"]) == ("2025-09-10", "pdf_created")
    assert (cfg.data_dir / "raw" / "pdf" / f"{sha}.pdf").read_bytes() == data
    doc = PdfDoc.from_json((cfg.data_dir / "raw" / "text" / f"{sha}.json").read_text(encoding="utf-8"))
    assert "Batch /Lot #: H25" in doc.pages[0]
    stored = conn.execute("SELECT * FROM reports WHERE filename='x.pdf'").fetchone()
    assert stored["etag"] == '"abc"' and stored["pages"] == 2 and stored["bytes"] == len(data)


def test_store_report_rejects_non_pdf(cfg, conn):
    row = store_report(cfg, conn, "bad.pdf", b"<html>not found</html>", {}, "http://u/bad.pdf")
    assert row["status"] == "error" and "not a PDF" in row["error"]
    assert not list((cfg.data_dir / "raw" / "pdf").glob("*.pdf"))


def test_pending_skips_excluded_expired_and_delisted(cfg, conn):
    cfg["as_of_date"] = "2026-11-17"
    _seed(conn, "INCL0001", "include", [("A", "2027-01-01", "keep.pdf"), ("B", "2026-11-16", "expired.pdf"),
                                        ("C", "2026-11-17", "expires-today.pdf"), ("D", None, "no-expiry.pdf")])
    _seed(conn, "LIQD0001", "exclude", [("A", "2027-01-01", "liquid.pdf")])
    _seed(conn, "CHEW0001", "pending", [("A", "2027-01-01", "chew.pdf")])
    _seed(conn, "GONE0001", "include", [("A", "2027-01-01", "gone.pdf")], listed=0)
    assert pending_reports(cfg, conn) == ["chew.pdf", "expires-today.pdf", "keep.pdf", "no-expiry.pdf"]


def test_fetch_downloads_once_then_nothing(cfg, conn, fixtures, fake_session_cls, fake_response_cls):
    pdf = (fixtures / "pdf" / "PLLF0001_per_cap.pdf").read_bytes()
    _seed(conn, lots=[("L1", "2099-01-01", "a b.pdf"), ("L2", "2099-01-01", "missing.pdf")])

    def handler(url, params):
        if url.endswith("a%20b.pdf"):
            return fake_response_cls(content=pdf, headers={"Last-Modified": "Mon, 04 May 2026 13:00:00 GMT"})
        return fake_response_cls(status=404)

    first = fake_session_cls(handler)
    stats = fetch_reports(cfg, conn, first, log=lambda *_: None)
    assert stats["reports_ok"] == 1 and stats["reports_error"] == 1 and stats["bytes_downloaded"] == len(pdf)
    assert conn.execute("SELECT status FROM reports WHERE filename='missing.pdf'").fetchone()[0] == "error"

    # Second run, same day: nothing is requested. The good file is cached and the
    # failed one is not retried inside the refresh window.
    second = fake_session_cls(handler)
    assert fetch_reports(cfg, conn, second, log=lambda *_: None) == {"reports_pending": 0} and second.calls == []
    # Once the failure is older than the window, only that file is retried.
    conn.execute("UPDATE reports SET last_checked='2020-01-01T00:00:00+00:00' WHERE filename='missing.pdf'")
    third = fake_session_cls(handler)
    stats3 = fetch_reports(cfg, conn, third, log=lambda *_: None)
    assert [url.rsplit("/", 1)[1] for url, _ in third.calls] == ["missing.pdf"] and stats3.get("reports_ok", 0) == 0


def test_revised_report_is_a_new_filename_and_gets_fetched(cfg, conn, fixtures, fake_session_cls, fake_response_cls):
    pdf = (fixtures / "pdf" / "PLLF0001_per_cap.pdf").read_bytes()
    handler = lambda url, params: fake_response_cls(content=pdf)
    _seed(conn, lots=[("L1", "2099-01-01", "Report.pdf")])
    fetch_reports(cfg, conn, fake_session_cls(handler), log=lambda *_: None)
    _seed(conn, lots=[("L1", "2099-01-01", "Report Rev1.pdf")])   # listing now points at a revision
    session = fake_session_cls(handler)
    fetch_reports(cfg, conn, session, log=lambda *_: None)
    assert len(session.calls) == 1 and session.calls[0][0].endswith("Report%20Rev1.pdf")
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 2   # both revisions stay in the manifest
    assert conn.execute("SELECT report_filename FROM batches").fetchone()[0] == "Report Rev1.pdf"
