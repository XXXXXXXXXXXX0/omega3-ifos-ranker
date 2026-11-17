"""Stage 3: template detection, table parsing, units, validation, LLM fallback."""
import datetime as dt
import json

import pytest

from omega3 import db
from omega3.extract import llm
from omega3.extract.derive import derive
from omega3.extract.parser import detect_template, parse_report
from omega3.extract.pdftext import PdfDoc, read_pdf
from omega3.extract.run import extract_all
from omega3.extract.terms import classify_form, find_form_terms, match_label
from omega3.extract.units import find_quantities, parse_basis, parse_report_expiry
from omega3.scrape.reports import store_report

# Values transcribed by hand from each fixture PDF.
# potency = (EPA, DHA, total omega-3) as printed; stability = (PV, AV, TOTOX, acid value)
EXPECTED = {
    "PLLF0001_per_cap": dict(template="T1", lot="F2604-0538", expiry="30-Apr-2028", basis=("capsule", 1),
                             potency=(423, 225, 750), per_cap=(423, 225, 750), stability=(5, 3.63, 13.63, 0.35),
                             extra={"pcbs": 0.666, "dioxins_furans": 0.223, "dioxin_like_pcbs": 0.0805, "mercury": 0.0125,
                                    "cadmium": 0.0017, "lead": 0.0067, "arsenic": 0.0067}),
    "ACAO0002_split_header_mmYYYY": dict(template="T1", lot="H25", expiry="11/2026", basis=("capsule", 1),
                                         potency=(105, 500, 706), per_cap=(105, 500, 706), stability=(2.23, 9.64, 14.10, 0.04),
                                         extra={"pcbs": 7.17, "dioxins_furans": 0.125, "dioxin_like_pcbs": 0.325, "arsenic": 0.0167}),
    "BEBI0001_per_2_softgels_rtg": dict(template="T1", lot="JFO202501", expiry="05-May-2027", basis=("capsule", 2),
                                        potency=(742, 256, 1033), per_cap=(371, 128, 516.5), stability=(2.8, 3.0, 8.6, 0.17),
                                        extra={"pcbs": 8.54, "mercury": 0.01}),
    "PIFT0001_per_5_softgels": dict(template="T1", lot="C522504001", expiry="31-Mar-2028", basis=("capsule", 5),
                                    potency=(2981, 1258, 4845), per_cap=(596.2, 251.6, 969), stability=(0.28, 10.53, 11.09, 0.29),
                                    extra={"pcbs": 2.37, "dioxin_like_pcbs": 0.0297}),
    "CERN0001_category_2024": dict(template="T1-2024", lot="13981", expiry="15/05/2027", basis=("capsule", 1),
                                   potency=(352, 206, 632), per_cap=(352, 206, 632), stability=(0.3, 5, 5.6, 0.4),
                                   extra={"pcbs": 1, "mercury": 0.005, "lead": 0.02, "arsenic": 0.04, "cadmium": 0.01}),
    "BLKC0001_ddmmyyyy": dict(template="T1", lot="324381", expiry="03/12/2026 (DD/MM/YY)", basis=("capsule", 1),
                              potency=(815, 547, 1510), per_cap=(815, 547, 1510), stability=(0.20, 5, 5.4, 0.37),
                              extra={"pcbs": 7.09, "dioxin_like_pcbs": 0.0312, "cadmium": 0.02}),
    "BARE0004_liquid_per_g": dict(template="T1", lot="AHL59675", expiry="28-May-2027", basis=("gram", 1),
                                  potency=(152.35, 489.37, 704.83), per_cap=(None, None, None), stability=(2.61, 7.64, 12.86, 0.16),
                                  extra={"pcbs": 0.495, "mercury": 0.0122}),
    "CRLL0082_liquid_per_ml": dict(template="T1", lot="0020226", expiry="02/2029", basis=("volume", 2.5),
                                   potency=(418, 266, 845), per_cap=(None, None, None), stability=(1.13, 11.4, 13.66, 0.10),
                                   extra={"dioxins_furans": 0.180, "dioxin_like_pcbs": 0.00629}),   # last one wraps over two lines
    "WHCC0007_chewy": dict(template="T1", lot="44006277", expiry="31-Aug-2028", basis=("other", 1),
                           potency=(340, 262, 660), per_cap=(None, None, None), stability=(0.19, 2, 2.38, 0.15),
                           extra={"pcbs": 0.2065295}),
    "BIOH0013_per_2_capsules_radio": dict(template="T1", lot="U606189", expiry="07-Jun-2029", basis=("capsule", 2),
                                          potency=(668, 446, 1215), per_cap=(334, 223, 607.5), stability=(3.0, 6.4, 12.4, 0.29),
                                          extra={"dioxins_furans": 0.375, "dioxin_like_pcbs": 0.0539}),  # both wrap
    "VETW0002_pet_per_cap": dict(template="T1", lot="20241120", expiry="19/11/2026 (DD/MM/YYYY)", basis=("capsule", 1),
                                 potency=(500, 262, 853), per_cap=(500, 262, 853), stability=(2.1, 9.2, 13.4, 0.1),
                                 extra={"pcbs": 18.7, "arsenic": 0.03}),
    "GNCC0011_per_2_softgels": dict(template="T1", lot="1069DJ2518", expiry="30-Nov-2028", basis=("capsule", 2),
                                    potency=(707, 562, 1346), per_cap=(353.5, 281, 673), stability=(0.18, 8.80, 9.16, 0.20),
                                    extra={"dioxins_furans": 0.0703, "dioxin_like_pcbs": 0.00748}),
}
ALL_ANALYTES = {"epa", "dha", "total_omega3", "pcbs", "dioxins_furans", "dioxin_like_pcbs", "acid_value", "pv", "av",
                "totox", "mercury", "cadmium", "lead", "arsenic"}


@pytest.fixture(scope="module")
def docs(fixtures):
    return {p.stem: read_pdf(p) for p in (fixtures / "pdf").glob("*.pdf")}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_values_match_the_pdf(docs, cfg, name):
    exp = EXPECTED[name]
    parsed = parse_report(docs[name])
    assert (parsed.template, parsed.status, parsed.error) == (exp["template"], "ok", None)
    assert parsed.lot == exp["lot"] and parsed.expiry_text == exp["expiry"]
    assert (parsed.basis.kind, parsed.basis.count) == exp["basis"]
    assert set(parsed.rows) == ALL_ANALYTES
    assert not parsed.unrecognized

    d = derive(parsed, cfg)
    assert d["status"] == "ok", d["error"]
    assert (d["epa_orig"], d["dha_orig"], d["total_omega3_orig"]) == exp["potency"]
    assert (d["epa_mg_cap"], d["dha_mg_cap"], d["total_omega3_mg_cap"]) == exp["per_cap"]
    assert (d["pv"], d["av"], d["totox"], d["acid_value"]) == exp["stability"]
    assert d["totox_source"] == "reported"
    for analyte, value in exp["extra"].items():
        assert parsed.rows[analyte].result_value == value, analyte
    # Every row carries its IFOS limit and a compliance verdict.
    for analyte in ALL_ANALYTES - {"epa", "dha", "total_omega3"}:
        row = parsed.rows[analyte]
        assert row.spec_op == "<=" and row.spec_value is not None and row.compliance == "yes", analyte
    # Independent check: each printed result appears verbatim in the page text.
    text = docs[name].text
    for row in parsed.rows.values():
        number = row.result_text.lstrip("< ").split()[0]
        assert number in text, (row.analyte, number)


def test_limits_units_and_censoring(docs):
    rows = parse_report(docs["PLLF0001_per_cap"]).rows
    assert (rows["pcbs"].spec_value, rows["pcbs"].spec_unit, rows["pcbs"].result_unit) == (45, "ppb", "ppb")
    assert rows["dioxins_furans"].spec_value == 1 and rows["dioxins_furans"].result_unit == "ppt WHO-TEQ"   # "(2005)" is not a value
    assert (rows["lead"].spec_value, rows["lead"].result_op, rows["lead"].result_value) == (0.05, "<", 0.0067)
    assert (rows["totox"].spec_value, rows["pv"].spec_value, rows["av"].spec_value, rows["acid_value"].spec_value) == (19.5, 5, 20, 3)
    assert rows["acid_value"].result_unit == "mg KOH/g" and rows["pv"].result_unit == "meq/kg"
    assert rows["epa"].spec_value == 400 and rows["epa"].result_op is None
    old = parse_report(docs["CERN0001_category_2024"]).rows
    assert old["arsenic"].label_raw == "Inorganic Arsenic" and old["totox"].label_raw == "Total Oxidation"
    assert old["lead"].spec_value == 0.1 and old["mercury"].result_op == "<"


def test_raw_material_certificate_has_no_results(docs, cfg):
    parsed = parse_report(docs["DYBT0001_raw_material_cert"])
    assert (parsed.template, parsed.status) == ("RM", "no_results") and not parsed.rows
    assert derive(parsed, cfg)["status"] == "no_results"


def test_comments_form_and_area_percent():
    tables = [[], [[["Potency", "Specification", "Batch Results", "IFOS Compliance"], [None, "mg / softgel", "mg / softgel", None],
                    ["EPA", "250", "255", "Yes"], ["DHA", "125", "139", "Yes"], ["Total Omega-3", "450", "450", "Yes"]],
                   [["Stability", "Specification", "Batch Results", "IFOS Compliance"], ["Peroxide", "<= 5 meq/kg", "1.95 meq/kg", "Yes"],
                    ["Anisidine", "<= 20", "2.46", "Yes"]],
                   [["Comments"], ["Radioactivity specification: < 50 bq/kg, Result: < 30 bq/kg.\n"
                                   "EPA area % specification: 250 mg/softgel (50%). Result: 255 mg/softgel (52.3%).\n"
                                   "Total Omega-3 area % specification: 450 mg/softgel (90%), Result: 446 mg/softgel (91.5%).\n"
                                   "EFA tested as triglyceride (TG) form."]]]]
    doc = PdfDoc(pages=["Brand Product\nBatch /Lot #: 2601007\nExpiry Date: 01/2029\nDirection: Take 1-4 softgels per day.",
                        "Specification Batch Results\nPotency IFOS Compliance\nStability"], tables=tables)
    parsed = parse_report(doc)
    assert parsed.status == "ok" and parsed.report_form == "TG"
    assert "tested as triglyceride" in parsed.report_form_evidence
    assert parsed.area_pct["epa"] == {"spec_mg": 250.0, "spec_pct": 50.0, "result_mg": 255.0, "result_pct": 52.3, "basis": "softgel"}
    assert parsed.area_pct["total_omega3"]["result_pct"] == 91.5
    assert len(parsed.comments) == 4 and parsed.directions == "Take 1-4 softgels per day."


def _doc(rows_potency, rows_stability, basis="mg / softgel"):
    tables = [[], [[["Potency", "Specification", "Batch Results", "IFOS Compliance"], [None, basis, basis, None], *rows_potency],
                   [["Stability", "Specification", "Batch Results", "IFOS Compliance"], *rows_stability]]]
    return PdfDoc(pages=["Batch /Lot #: L1\nExpiry Date: 01/2029", "Specification Batch Results\nPotency\nStability"], tables=tables)


GOOD_POTENCY = [["EPA", "400", "420", "Yes"], ["DHA", "200", "210", "Yes"], ["Total Omega-3", "700", "720", "Yes"]]
GOOD_STABILITY = [["Acid Value", "<= 3 mg KOH/g", "0.2 mg KOH/g", "Yes"], ["Peroxide", "<= 5 meq/kg", "1.0 meq/kg", "Yes"],
                  ["Anisidine", "<= 20", "4.0", "Yes"], ["TOTOX", "<= 19.5", "6.0", "Yes"]]


def test_totox_computed_when_absent_and_crosscheck_warning(cfg):
    d = derive(parse_report(_doc(GOOD_POTENCY, GOOD_STABILITY[:3])), cfg)
    assert (d["totox"], d["totox_source"]) == (6.0, "computed")
    mismatch = GOOD_STABILITY[:3] + [["TOTOX", "<= 19.5", "9.0", "Yes"]]
    d = derive(parse_report(_doc(GOOD_POTENCY, mismatch)), cfg)
    assert (d["totox"], d["totox_source"]) == (9.0, "reported")       # reported value is still the one used
    assert any("differs from 2*PV+AV" in w for w in d["warnings"])


def test_missing_and_non_numeric_cells(cfg):
    # Empty result cell: the row is partial, columns come from the full rows of the same table.
    stability = [GOOD_STABILITY[0], ["Peroxide", "<= 5 meq/kg", None, "Yes"], GOOD_STABILITY[2]]
    parsed = parse_report(_doc(GOOD_POTENCY, stability))
    assert parsed.status == "ok" and parsed.rows["pv"].result_value is None and parsed.rows["pv"].spec_value == 5
    d = derive(parsed, cfg)
    assert d["pv"] is None and d["totox"] is None and d["totox_source"] is None   # cannot compute without PV
    nd = [GOOD_STABILITY[0], ["Peroxide", "<= 5 meq/kg", "ND", "Yes"], GOOD_STABILITY[2]]
    parsed = parse_report(_doc(GOOD_POTENCY, nd))
    assert parsed.status == "ok" and parsed.rows["pv"].result_text == "ND" and parsed.rows["pv"].result_value is None
    censored = [GOOD_STABILITY[0], ["Peroxide", "<= 5 meq/kg", "< 0.1 meq/kg", "Yes"], GOOD_STABILITY[2]]
    d = derive(parse_report(_doc(GOOD_POTENCY, censored)), cfg)
    assert (d["pv"], d["pv_censored"], d["totox"], d["totox_censored"]) == (0.1, 1, 4.2, 1)


def test_missing_epa_is_null_not_zero(cfg):
    d = derive(parse_report(_doc(GOOD_POTENCY[1:], GOOD_STABILITY)), cfg)
    assert d["status"] == "ok" and d["epa_orig"] is None and d["epa_mg_cap"] is None and d["dha_mg_cap"] == 210


def test_unreadable_rows_fail_instead_of_guessing(cfg):
    garbled = [["EPA", "400", "4 2 0", "Yes"], *GOOD_POTENCY[1:]]
    parsed = parse_report(_doc(garbled, GOOD_STABILITY))
    assert parsed.status == "failed" and "EPA" in parsed.error
    no_unit = parse_report(_doc(GOOD_POTENCY, GOOD_STABILITY, basis="per serving"))
    assert no_unit.status == "failed" and "potency unit not found" in no_unit.error
    assert parse_report(PdfDoc(pages=["Consumer Report\nsome chart"], tables=[[]])).error == "unrecognized report layout"


def test_unrecognized_label_is_logged_not_dropped():
    potency = GOOD_POTENCY + [["DPA", "20", "25", "Yes"]]
    parsed = parse_report(_doc(potency, GOOD_STABILITY))
    assert parsed.status == "ok" and parsed.unrecognized == ["DPA | 20 | 25 | Yes"]


@pytest.mark.parametrize("potency,stability,message", [
    ([["EPA", "400", "4200", "Yes"], *GOOD_POTENCY[1:]], GOOD_STABILITY, "implausible EPA"),
    (GOOD_POTENCY, [GOOD_STABILITY[0], ["Peroxide", "<= 5 meq/kg", "500 meq/kg", "Yes"], *GOOD_STABILITY[2:]], "implausible PV"),
    (GOOD_POTENCY, [["Acid Value", "<= 3 mg KOH/g", "45 mg KOH/g", "No"], *GOOD_STABILITY[1:]], "implausible acid value"),
    ([["EPA", "400", "600", "Yes"], ["DHA", "200", "400", "Yes"], ["Total Omega-3", "700", "720", "Yes"]], GOOD_STABILITY, "exceeds total omega-3"),
])
def test_out_of_range_values_are_rejected(cfg, potency, stability, message):
    d = derive(parse_report(_doc(potency, stability)), cfg)
    assert d["status"] == "failed" and message in d["error"]


def test_crosscheck_warnings(cfg):
    stability = [["Acid Value", "<= 3 mg KOH/g", "4 mg KOH/g", "Yes"], *GOOD_STABILITY[1:]]
    d = derive(parse_report(_doc(GOOD_POTENCY, stability)), cfg, listing_lot="OTHER9", listing_expiry=dt.date(2027, 6, 30))
    joined = " | ".join(d["warnings"])
    assert "compliance says yes" in joined and "lot on report" in joined and "expiry on report 2029-01-31 differs" in joined
    d = derive(parse_report(_doc(GOOD_POTENCY, GOOD_STABILITY)), cfg, listing_lot="l-1", listing_expiry=dt.date(2029, 1, 31))
    assert d["warnings"] == []


# --- units -------------------------------------------------------------------
@pytest.mark.parametrize("text,kind,count", [
    ("mg / cap", "capsule", 1), ("mg per cap", "capsule", 1), ("mg/cap", "capsule", 1), ("mg / softgel", "capsule", 1),
    ("mg / capsule", "capsule", 1), ("mg per 2 softgels", "capsule", 2), ("mg / 5 softgels", "capsule", 5),
    ("mg / 2 capsules", "capsule", 2), ("mg per g", "gram", 1), ("mg/g", "gram", 1), ("mg per 1000 mg", "gram", 1),
    ("mg / 5 mL", "volume", 5), ("mg /2.5 mL", "volume", 2.5), ("mg / 1.5 tsp", "volume", 1.5), ("mg per 1.5 tsps", "volume", 1.5),
    ("mg / vial", "other", 1), ("mg / chewy", "other", 1), ("%", "percent", 1),
])
def test_parse_basis(text, kind, count):
    basis = parse_basis(text)
    assert (basis.kind, basis.count) == (kind, count)


def test_parse_basis_rejects_non_units():
    assert parse_basis("Specification") is None and parse_basis("mg / 0 softgels") is None


def test_unit_conversions(cfg):
    per_two = derive(parse_report(_doc([["EPA", "800", "840", "Yes"], ["DHA", "400", "420", "Yes"]], GOOD_STABILITY, "mg / 2 softgels")), cfg)
    assert (per_two["epa_mg_cap"], per_two["dha_mg_cap"]) == (420, 210) and "divided by 2" in per_two["conversion"]
    per_gram = derive(parse_report(_doc(GOOD_POTENCY, GOOD_STABILITY, "mg per 1000 mg")), cfg)
    assert (per_gram["epa_mg_g"], per_gram["epa_mg_cap"]) == (420, None) and "label oil grams" in per_gram["conversion"]
    percent = derive(parse_report(_doc([["EPA", "40", "42", "Yes"], ["DHA", "20", "21", "Yes"]], GOOD_STABILITY, "%")), cfg)
    assert percent["epa_mg_g"] == 420 and any("%" in w for w in percent["warnings"])
    per_ml = derive(parse_report(_doc(GOOD_POTENCY, GOOD_STABILITY, "mg / 5 mL")), cfg)
    assert per_ml["epa_mg_cap"] is None and per_ml["epa_mg_g"] is None and per_ml["epa_orig"] == 420
    assert "not converted" in per_ml["conversion"]


def test_find_quantities():
    q = find_quantities("<= 1 ppt WHO-TEQ (2005)")
    assert [(x.op, x.value, x.unit) for x in q] == [("<=", 1.0, "ppt WHO-TEQ")]
    assert [(x.op, x.value) for x in find_quantities("< 0.01 ppm")] == [("<", 0.01)]
    assert find_quantities("1,215")[0].value == 1215 and find_quantities("≤ 45 ppb")[0].op == "<="
    assert find_quantities("ND") == []


@pytest.mark.parametrize("text,hint,expected", [
    ("30-Apr-2028", None, dt.date(2028, 4, 30)), ("10/2027", None, dt.date(2027, 10, 31)), ("02/2028", None, dt.date(2028, 2, 29)),
    ("03/12/2026 (DD/MM/YY)", None, dt.date(2026, 12, 3)), ("19/11/2026 (DD/MM/YYYY)", None, dt.date(2026, 11, 19)),
    ("15/05/2027", None, dt.date(2027, 5, 15)), ("30/NOV/2026", None, dt.date(2026, 11, 30)), ("2028-04-30", None, dt.date(2028, 4, 30)),
    ("Apr 30, 2028", None, dt.date(2028, 4, 30)), ("May 2027", None, dt.date(2027, 5, 31)),
    ("03/04/2027", dt.date(2027, 4, 3), dt.date(2027, 4, 3)), ("03/04/2027", dt.date(2027, 3, 4), dt.date(2027, 3, 4)),
    ("03/04/2027", None, None), ("03/04/2027", dt.date(2030, 1, 1), None), ("soon", None, None), ("31/02/2027", None, None),
])
def test_parse_report_expiry(text, hint, expected):
    assert parse_report_expiry(text, hint) == expected


# --- terminology -------------------------------------------------------------
def test_row_labels_and_whole_word_matching():
    assert match_label("Peroxide <= 5")[:2] == ("pv", "Peroxide")
    assert match_label("PV")[0] == "pv" and match_label("p-Anisidine Value")[0] == "av" and match_label("AV 3")[0] == "av"
    assert match_label("Total Oxidation")[0] == "totox" and match_label("FFA")[0] == "acid_value"
    assert match_label("Dioxin-Like PCBs")[0] == "dioxin_like_pcbs" and match_label("PCBs")[0] == "pcbs"
    assert match_label("Total Omega 3")[0] == "total_omega3" and match_label("C22:6 n-3")[0] == "dha"
    assert match_label("Inorganic Arsenic")[0] == "arsenic"
    for not_a_label in ("AVERAGE 3", "PVC 2", "EPAX 500", "Leading 1", "FFAs"):
        assert match_label(not_a_label) is None, not_a_label


@pytest.mark.parametrize("text,form", [
    ("Joy Fish Oil Concentrate 85% omega-3 rTG with 720 mg EPA", "rTG"),
    ("Re-esterified Triglyceride form omega-3", "rTG"), ("reesterified triglycerides", "rTG"), ("r-TG fish oil", "rTG"),
    ("Supra Omega 3 TG 18 EPA/12 DHA", "TG"), ("natural triglyceride form", "TG"), ("Triacylglycerol", "TG"), ("nTG", "TG"),
    ("EFA tested as triglyceride (TG) form.", "TG"),
    ("High potency triglyceride form fish oil", "TG"), ("healthy natural triglyceride form", "TG"),
    ("Omega-3 Ethyl Ester concentrate", "EE"), ("EPA 90 EE", "EE"),
    ("OmegaKrill 5x is a combination of fish and krill oils", "PL"), ("phospholipid omega-3", "PL"),
    ("MaxSimil monoglyceride fish oil", "MAG"), ("MAG omega", "MAG"),
])
def test_classify_form(text, form):
    assert classify_form(text)[0] == form


@pytest.mark.parametrize("text", [
    "Omega 3 Fish Oil 1000 mg", "Deep sea fish oil for cats", "",                       # nothing stated: never assumed TG
    "STAGE 2 MEETING, PLEASE SEE TAGS", "BEEF TALLOW", "WETGEL", "PLUS omega",           # short terms only as whole words
    "Take two gelcaps daily to reduce serum triglycerides / triacylglycerols.",          # blood lipids, not the oil
    "supports healthy triglyceride levels",
])
def test_form_not_stated(text):
    form, reason = classify_form(text)
    assert form is None and reason == "no form stated", find_form_terms(text)


def test_conflicting_form_terms_stay_unverified():
    form, reason = classify_form("Available in TG and ethyl ester forms")
    assert form is None and "conflicting" in reason
    form, reason = classify_form("rTG fish oil with krill phospholipids")
    assert form is None and "conflicting" in reason


# --- LLM fallback ------------------------------------------------------------
PAYLOAD = {"sha256": "abc", "is_batch_report": True, "lot": "L9", "expiry_text": "01/2029", "directions": None,
           "basis_text": "mg / 2 softgels",
           "rows": [{"analyte": "epa", "label_raw": "EPA", "spec_text": "800", "result_text": "840", "compliance": "Yes"},
                    {"analyte": "dha", "label_raw": "DHA", "spec_text": "400", "result_text": "420", "compliance": "Yes"},
                    {"analyte": "pv", "label_raw": "Peroxide Value", "spec_text": "<= 5 meq/kg", "result_text": "1.0 meq/kg", "compliance": "Yes"},
                    {"analyte": "av", "label_raw": "p-Anisidine", "spec_text": "<= 20", "result_text": "4", "compliance": "Yes"},
                    {"analyte": "mercury", "label_raw": "Mercury", "spec_text": "<= 0.1 ppm", "result_text": "<0.01 ppm", "compliance": "Yes"}],
           "comments": ["EFA tested as ethyl ester (EE)."], "notes": None}


def test_llm_payload_goes_through_the_same_parsing_and_validation(cfg):
    parsed = llm.parsed_from_payload(PAYLOAD)
    assert parsed.status == "ok" and parsed.template == "llm" and parsed.report_form == "EE"
    d = derive(parsed, cfg)
    assert (d["epa_mg_cap"], d["dha_mg_cap"], d["totox"], d["totox_source"]) == (420, 210, 6.0, "computed")
    assert parsed.rows["mercury"].result_op == "<"
    bad = json.loads(json.dumps(PAYLOAD)); bad["rows"][0]["result_text"] = "9999"
    assert derive(llm.parsed_from_payload(bad), cfg)["status"] == "failed"            # range check still applies
    garbled = json.loads(json.dumps(PAYLOAD)); garbled["rows"][2]["result_text"] = "about one"
    assert llm.parsed_from_payload(garbled).status == "failed"                        # never guessed
    assert llm.parsed_from_payload({**PAYLOAD, "is_batch_report": False}).status == "no_results"


def _seed_report(cfg, conn, fixtures, name="PLLF0001_per_cap.pdf", filename="r.pdf"):
    now = db.now_iso()
    db.upsert(conn, "products", dict(product_num="P1", name="p", format_decision="include", first_seen=now, last_checked=now), keys=("product_num",))
    db.upsert(conn, "batches", dict(product_num="P1", lot="F2604-0538", expiry_date="2028-04-30", report_filename=filename,
                                    first_seen=now, last_seen=now), keys=("product_num", "lot"))
    return store_report(cfg, conn, filename, (fixtures / "pdf" / name).read_bytes(), {}, "http://u/" + filename)


def test_extract_all_writes_sqlite_and_is_idempotent(cfg, conn, fixtures):
    _seed_report(cfg, conn, fixtures)
    stats = extract_all(cfg, conn, log=lambda *_: None)
    assert stats["parser_ok"] == 1 and stats["llm_queue"] == 0
    row = conn.execute("SELECT * FROM extractions").fetchone()
    assert (row["method"], row["template"], row["epa_mg_cap"], row["pv"], row["totox"], row["totox_source"]) == ("parser", "T1", 423, 5, 13.63, "reported")
    assert json.loads(row["warnings"]) == []
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 14
    lead = conn.execute("SELECT * FROM measurements WHERE analyte='lead'").fetchone()
    assert (lead["spec_value"], lead["result_op"], lead["result_value"], lead["compliance"]) == (0.05, "<", 0.0067, "yes")
    assert extract_all(cfg, conn, log=lambda *_: None).get("parser_ok", 0) == 0      # nothing pending on re-run


def _break_text_layer(cfg, sha):
    """Make the parser fail on a stored report by removing its potency unit cells."""
    path = cfg.data_dir / "raw" / "text" / f"{sha}.json"
    doc = PdfDoc.from_json(path.read_text(encoding="utf-8"))
    doc.tables = [[[[None if c == "mg per cap" else c for c in row] for row in table] for table in page] for page in doc.tables]
    path.write_text(doc.to_json(), encoding="utf-8")


def test_parser_failure_is_queued_then_served_from_cache(cfg, conn, fixtures):
    sha = _seed_report(cfg, conn, fixtures)["sha256"]
    _break_text_layer(cfg, sha)
    stats = extract_all(cfg, conn, log=lambda *_: None)
    assert stats["parser_failed"] == 1 and stats["llm_queue"] == 1
    assert [q["sha256"] for q in llm.read_queue(cfg)] == [sha]
    assert conn.execute("SELECT status FROM extractions").fetchone()[0] == "failed"

    llm.save_cached(cfg, sha, {**PAYLOAD, "sha256": sha, "comments": []}, llm.IN_SESSION_MODEL)
    stats = extract_all(cfg, conn, log=lambda *_: None)
    assert stats["llm_ok"] == 1 and stats["llm_from_cache"] == 1 and stats["llm_queue"] == 0
    row = conn.execute("SELECT method, model, status, epa_mg_cap FROM extractions").fetchone()
    assert tuple(row) == ("llm", "in-session", "ok", 420)
    assert llm.read_queue(cfg) == []


class FakeAnthropic:
    """Stands in for anthropic.Anthropic(); records every request."""

    def __init__(self, reply=None, stop_reason="end_turn"):
        self.requests = []
        self.reply, self.stop_reason = reply, stop_reason
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        shas = [line.split('"')[1] for line in kwargs["messages"][0]["content"].split("\n") if line.startswith("<report sha256=")]
        body = self.reply if self.reply is not None else json.dumps({"reports": [{**PAYLOAD, "sha256": s, "comments": []} for s in shas]})
        block = type("Block", (), {"type": "text", "text": body})()
        usage = type("Usage", (), {"input_tokens": 1000, "output_tokens": 500})()
        return type("Resp", (), {"content": [block], "stop_reason": self.stop_reason, "usage": usage})()


def test_api_path_is_off_by_default_and_never_builds_a_client(cfg, conn, fixtures, monkeypatch):
    assert cfg["llm"]["api_enabled"] is False
    monkeypatch.setattr(llm, "make_client", lambda cfg: pytest.fail("API client constructed while llm.api_enabled is false"))
    sha = _seed_report(cfg, conn, fixtures)["sha256"]
    _break_text_layer(cfg, sha)
    assert extract_all(cfg, conn, log=lambda *_: None)["llm_queue"] == 1
    with pytest.raises(RuntimeError, match="api_enabled is false"):
        llm.extract_via_api(cfg, llm.read_queue(cfg), lambda s: None, client=FakeAnthropic())


def test_api_path_batches_caches_and_never_asks_twice(cfg, conn, fixtures, monkeypatch):
    cfg["llm"]["api_enabled"] = True
    fake = FakeAnthropic()
    monkeypatch.setattr(llm, "make_client", lambda cfg: fake)
    sha = _seed_report(cfg, conn, fixtures)["sha256"]
    _break_text_layer(cfg, sha)
    stats = extract_all(cfg, conn, log=lambda *_: None)
    assert stats["llm_api_calls"] == 1 and stats["llm_ok"] == 1 and len(fake.requests) == 1
    request = fake.requests[0]
    assert request["model"] == "claude-haiku-5-5"                                   # cheap model, not the session's
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "untrusted data" in request["system"] and f'<report sha256="{sha}">' in request["messages"][0]["content"]
    record = json.loads((cfg.data_dir / "cache" / "llm" / f"{sha}.json").read_text())
    assert record["model"] == "claude-haiku-5-5" and record["usage"]["cost_usd"] == pytest.approx(0.00035)
    extract_all(cfg, conn, log=lambda *_: None)
    assert len(fake.requests) == 1                                                  # cached: no second call


def test_api_refusal_or_bad_json_leaves_report_in_queue(cfg, monkeypatch):
    cfg["llm"]["api_enabled"] = True
    doc = lambda s: PdfDoc(pages=["x"])
    queue = [{"sha256": "s1", "filename": "f", "reason": "r"}]
    assert llm.extract_via_api(cfg, queue, doc, client=FakeAnthropic(stop_reason="refusal"), log=lambda *_: None) == 0
    assert llm.extract_via_api(cfg, queue, doc, client=FakeAnthropic(reply="not json"), log=lambda *_: None) == 0
    wrong_hash = json.dumps({"reports": [{**PAYLOAD, "sha256": "someone-else"}]})
    assert llm.extract_via_api(cfg, queue, doc, client=FakeAnthropic(reply=wrong_hash), log=lambda *_: None) == 0
    assert llm.load_cached(cfg, "s1") is None


def test_api_key_never_comes_from_claude_codes_variable(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-code-key")
    monkeypatch.delenv("OMEGA3_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OMEGA3_ANTHROPIC_API_KEY is not set"):
        llm.api_key(cfg)
    (tmp_path / ".env").write_text("OTHER=1\nOMEGA3_ANTHROPIC_API_KEY='from-dotenv'\n")
    assert llm.api_key(cfg) == "from-dotenv"
    monkeypatch.setenv("OMEGA3_ANTHROPIC_API_KEY", "from-env")
    assert llm.api_key(cfg) == "from-env"
    cfg["llm"]["api_key_env"] = "ANTHROPIC_API_KEY"
    with pytest.raises(RuntimeError, match="must not be ANTHROPIC_API_KEY"):
        llm.api_key(cfg)


def test_template_detection():
    assert detect_template("Raw Material Brand Names: X\nCertified SKUs:") == "RM"
    assert detect_template("Category 1: Specification Batch Results\nPotency\nStability") == "T1-2024"
    assert detect_template("Specification Batch Results\nPotency IFOS Compliance\nStability") == "T1"
    assert detect_template("IFOS Consumer Report\n5 star rating") == "unknown"


# --- cases first met in the live crawl -----------------------------------------------------------
def test_combined_epa_dha_row(cfg):
    for label in ("EPA + DHA", "EPA+DHA", "EPA +DHA", "EPA + DAH"):                  # last one is IFOS's own misspelling
        parsed = parse_report(_doc([[label, "1100", "1200", "Yes"]], GOOD_STABILITY, "mg / cap"))
        assert parsed.status == "ok" and parsed.unrecognized == [], label
        d = derive(parsed, cfg)
        assert (d["epa_dha_orig"], d["epa_dha_mg_cap"], d["epa_mg_cap"], d["dha_mg_cap"]) == (1200, 1200, None, None)
    both = GOOD_POTENCY + [["EPA + DHA", "600", "700", "Yes"]]
    d = derive(parse_report(_doc(both, GOOD_STABILITY)), cfg)
    assert any("combined EPA+DHA row 700 differs from EPA + DHA = 630" in w for w in d["warnings"])


def test_grams_per_100_grams_unit(cfg):
    assert (parse_basis("g / 100 g").kind, parse_basis("g / 100 g").count) == ("gram", 0.1)
    assert parse_basis("g / softgel") is None
    d = derive(parse_report(_doc([["EPA", "46.7", "51.6", "Yes"], ["DHA", "23.3", "27.5", "Yes"]], GOOD_STABILITY, "g / 100 g")), cfg)
    assert d["status"] == "ok" and (d["epa_mg_g"], d["dha_mg_g"]) == (516, 275)


def test_misprinted_number_is_not_read_as_a_value(cfg):
    with pytest.raises(ValueError, match="misprinted"):
        find_quantities("<00016 ppm")                                                # 0.0016 with the point lost, not 16
    assert find_quantities("0.0016 ppm")[0].value == 0.0016 and find_quantities("0 ppt")[0].value == 0
    metals = [["Heavy Metals", "Specification", "Batch Results", "IFOS Compliance"], ["Cadmium", "<= 0.1 ppm", "<00016 ppm", "Yes"],
              ["Lead", "<= 0.05 ppm", "<0.0066 ppm", "Yes"]]
    doc = _doc(GOOD_POTENCY, GOOD_STABILITY)
    doc.tables[1].append(metals)
    parsed = parse_report(doc)
    cadmium = parsed.rows["cadmium"]
    assert parsed.status == "ok" and cadmium.result_value is None and cadmium.result_text == "<00016 ppm" and cadmium.compliance == "yes"
    assert any("Cadmium" in w and "misprinted" in w for w in parsed.warnings) and parsed.rows["lead"].result_value == 0.0066


def test_product_without_epa_or_dha_parses_but_has_no_potency(cfg):
    parsed = parse_report(_doc([["Omega-7 Palmitoleic Acid", "500", "570", "Yes"]], GOOD_STABILITY, "mg / 2 capsules"))
    assert parsed.status == "ok" and any("no EPA or DHA row" in w for w in parsed.warnings)
    d = derive(parsed, cfg)
    assert d["status"] == "ok" and d["epa_mg_cap"] is None and d["epa_dha_mg_cap"] is None and d["totox"] == 6.0
    assert parse_report(_doc([], GOOD_STABILITY)).error == "no potency rows found"
