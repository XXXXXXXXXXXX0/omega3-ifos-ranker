"""Stage 4: filters, scoring, ties, lists. The locked criteria are tested here."""
import datetime as dt

import pytest

from omega3 import db
from omega3.load import load_products, placement_row, save_results
from omega3.rank import (EXCLUDED, FLAVORED, FORM_UNVERIFIED, INCOMPLETE, MONOGLYCERIDE, OIL_UNAVAILABLE, PET, RANKED,
                         Batch, Contaminant, LabelFacts, Product, agent_queue, assign_ranks, place, rank_products)

TODAY = dt.date(2026, 11, 17)


def contaminant(analyte="mercury", value=0.01, limit=0.1, compliance="yes"):
    return Contaminant(analyte, analyte.title(), f"{value} ppm", value, f"<= {limit} ppm", limit, "<=", compliance)


ALL_CLEAN = [contaminant(a) for a in ("pcbs", "dioxins_furans", "dioxin_like_pcbs", "mercury", "lead", "cadmium", "arsenic")]


def batch(**kw):
    base = dict(lot="L1", expiry_date=dt.date(2028, 1, 31), report_filename="r.pdf", report_date=dt.date(2026, 6, 1),
                status="ok", method="parser", basis_text="mg / softgel", basis_kind="capsule", basis_count=1,
                epa_mg_cap=400, dha_mg_cap=200, total_omega3_mg_cap=700, pv=1.0, av=4.0, totox=6.0, totox_source="reported",
                acid_value=0.2, contaminants=list(ALL_CLEAN))
    base.update(kw)
    return Batch(**base)


def product(num="P1", name="rTG Omega-3 unflavored", batches=None, label="default", **kw):
    if label == "default":
        label = LabelFacts(status="verified", oil_mg_per_capsule=1000, oil_evidence="Fish oil concentrate 1000 mg")
    return Product(product_num=num, name=name, brand="Brand", product_type=kw.pop("product_type", "Softgels"),
                   batches=[batch()] if batches is None else batches, label=label, **kw)


def placed(cfg, **kw):
    return place(product(**kw), cfg, TODAY)


# --- oxidation cutoffs (LOCKED) -------------------------------------------------
@pytest.mark.parametrize("field,value,ok", [
    ("totox", 9.99, True), ("totox", 10.0, False), ("totox", 10.01, False),
    ("pv", 3.0, True), ("pv", 3.01, False), ("av", 8.0, True), ("av", 8.01, False),
])
def test_oxidation_boundaries(cfg, field, value, ok):
    values = dict(pv=1.0, av=4.0, totox=6.0)
    values[field] = value
    p = placed(cfg, batches=[batch(**values)])
    assert (p.list_name == RANKED) is ok, p.reason
    if not ok:
        assert p.list_name == EXCLUDED and p.reason.startswith("oxidation cutoff") and f"{value:g}" in p.reason


def test_no_lower_bound_and_preferred_tier(cfg):
    assert placed(cfg, batches=[batch(pv=0.0, av=0.0, totox=0.0)]).list_name == RANKED
    assert placed(cfg, batches=[batch(totox=8.0)]).preferred is True
    assert placed(cfg, batches=[batch(totox=8.01)]).preferred is False
    assert placed(cfg, batches=[batch(totox=8.01)]).list_name == RANKED     # preferred tier is a mark, not a cutoff


@pytest.mark.parametrize("value,ok", [(3.0, True), (3.01, False), (0.0, True)])
def test_acid_value_boundary(cfg, value, ok):
    p = placed(cfg, batches=[batch(acid_value=value)])
    assert (p.list_name == RANKED) is ok
    if not ok:
        assert p.reason.startswith("acid value cutoff")


def test_censored_values_are_judged_at_the_bound(cfg):
    p = placed(cfg, batches=[batch(pv=3.0, censored=["pv"])])
    assert p.list_name == RANKED and any("evaluated at x" in f for f in p.flags)
    assert placed(cfg, batches=[batch(pv=3.5, censored=["pv"])]).list_name == EXCLUDED


# --- missing data ---------------------------------------------------------------
@pytest.mark.parametrize("missing", [dict(pv=None), dict(av=None), dict(pv=None, av=None, totox=None)])
def test_missing_pv_or_av_is_incomplete_data(cfg, missing):
    p = placed(cfg, batches=[batch(**missing)])
    assert p.list_name == INCOMPLETE and "oxidation cannot be assessed" in p.reason


def test_missing_acid_value_or_contaminants_is_flagged_not_excluded(cfg):
    p = placed(cfg, batches=[batch(acid_value=None, contaminants=ALL_CLEAN[:3])])
    assert p.list_name == RANKED
    assert "acid value missing" in p.flags
    assert "contaminants not reported: mercury, lead, cadmium, arsenic" in p.flags


def test_computed_totox_is_marked(cfg):
    p = placed(cfg, batches=[batch(totox=6.0, totox_source="computed")])
    assert p.list_name == RANKED and "TOTOX computed as 2*PV+AV" in p.flags


# --- contaminants -----------------------------------------------------------------
def test_contaminant_over_ifos_limit_excludes(cfg):
    over = [contaminant("lead", value=0.06, limit=0.05, compliance="no")]
    p = placed(cfg, batches=[batch(contaminants=over)])
    assert p.list_name == EXCLUDED and p.reason.startswith("contaminant over IFOS limit") and "Lead" in p.reason
    # Number over the stated limit excludes even if the compliance cell says yes.
    assert placed(cfg, batches=[batch(contaminants=[contaminant("lead", 0.06, 0.05, "yes")])]).list_name == EXCLUDED
    # At the limit passes; the limit used is the report's own, not a constant.
    assert placed(cfg, batches=[batch(contaminants=[contaminant("lead", 0.05, 0.05, "yes")])]).list_name == RANKED
    assert placed(cfg, batches=[batch(contaminants=[contaminant("lead", 0.06, 0.1, "yes")])]).list_name == RANKED


# --- format -------------------------------------------------------------------------
@pytest.mark.parametrize("ptype,name,unit,list_name", [
    ("Capsules", "rTG oil", "mg / cap", RANKED), ("Softgels", "rTG oil", "mg / softgel", RANKED),
    ("Liquids", "rTG oil", "mg / 5 mL", EXCLUDED), ("Gummies", "rTG gummies", "mg / gummy", EXCLUDED),
    ("Chewables", "rTG Kids", "mg / softgel", RANKED), ("Chewables", "rTG Kids", "mg / chewy", EXCLUDED),
    ("Chewables", "rTG Gummy Chews", "mg / softgel", EXCLUDED),
])
def test_capsules_only(cfg, ptype, name, unit, list_name):
    kind = "capsule" if "softgel" in unit or "cap" in unit else "other"
    p = placed(cfg, name=name, product_type=ptype, batches=[batch(basis_text=unit, basis_kind=kind)])
    assert p.list_name == list_name, p.reason
    if ptype == "Chewables" and list_name == RANKED:
        assert "chewable softgel" in p.flags


# --- form (LOCKED: TG and rTG only) ---------------------------------------------------
@pytest.mark.parametrize("name,list_name,form", [
    ("Omega-3 rTG 1000", RANKED, "rTG"), ("Natural Triglyceride Fish Oil", RANKED, "TG"),
    ("Omega-3 Ethyl Ester 1000", EXCLUDED, "EE"), ("Antarctic Krill Oil", EXCLUDED, "PL"),
    ("Phospholipid Omega", EXCLUDED, "PL"), ("MaxSimil Monoglyceride Omega", MONOGLYCERIDE, "MAG"),
    ("Omega 3 Fish Oil 1000 mg", FORM_UNVERIFIED, None), ("TG and EE Omega blend", FORM_UNVERIFIED, None),
])
def test_form_filter(cfg, name, list_name, form):
    p = placed(cfg, name=name)
    assert (p.list_name, p.form) == (list_name, form), p.reason


def test_generic_fish_oil_is_never_assumed_tg(cfg):
    p = placed(cfg, name="Pure Fish Oil", label=None)
    assert p.list_name == FORM_UNVERIFIED and "not attempted" in p.reason and p.form is None
    tried = LabelFacts(status="unverified", reasoning="brand page says only 'natural fish oil'")
    p = placed(cfg, name="Pure Fish Oil", label=tried)
    assert p.list_name == FORM_UNVERIFIED and "natural fish oil" in p.reason and "not attempted" not in p.reason


def test_form_sources_and_conflicts(cfg):
    from_label = LabelFacts(status="verified", form="rTG", form_source_url="https://brand.example/p", form_evidence="rTG form", oil_mg_per_capsule=1000)
    p = placed(cfg, name="Pure Fish Oil", label=from_label)
    assert (p.list_name, p.form, p.form_source) == (RANKED, "rTG", "label") and "brand.example" in p.form_evidence
    p = placed(cfg, name="Pure Fish Oil", batches=[batch(report_form="TG", report_form_evidence="EFA tested as triglyceride (TG) form.")])
    assert (p.list_name, p.form, p.form_source) == (RANKED, "TG", "IFOS report")
    ee_label = LabelFacts(status="verified", form="EE", oil_mg_per_capsule=1000)
    p = placed(cfg, name="Omega-3 rTG 1000", label=ee_label)                    # listing says rTG, label says EE
    assert p.list_name == FORM_UNVERIFIED and "sources disagree" in p.reason
    unverified_label = LabelFacts(status="unverified", form="TG", oil_mg_per_capsule=1000)   # an unverified guess does not count
    assert placed(cfg, name="Pure Fish Oil", label=unverified_label).list_name == FORM_UNVERIFIED


# --- batches (LOCKED) ------------------------------------------------------------------
def test_expired_batches_are_dropped(cfg):
    old = batch(lot="OLD", expiry_date=dt.date(2026, 11, 16), report_date=dt.date(2026, 10, 1), totox=2.0)
    new = batch(lot="NEW", expiry_date=dt.date(2026, 11, 17), report_date=dt.date(2025, 1, 1), totox=9.0)
    p = placed(cfg, batches=[old, new])
    assert p.batch.lot == "NEW" and p.expired_dropped == 1 and p.unexpired_batches == 1    # expiring today still counts
    p = placed(cfg, batches=[old])
    assert p.list_name == EXCLUDED and p.reason == "no unexpired batch"


def test_ranks_on_most_recent_report_date(cfg):
    older = batch(lot="A", report_date=dt.date(2026, 1, 1), expiry_date=dt.date(2029, 1, 1), totox=2.0, av=1.0)
    newer = batch(lot="B", report_date=dt.date(2026, 9, 1), expiry_date=dt.date(2028, 1, 1), totox=9.0, av=7.0)
    p = placed(cfg, batches=[older, newer])
    assert p.batch.lot == "B" and p.score == pytest.approx(9.0 * 1000 / 600)
    cfg["batches"]["recency_key"] = "expiry_date"
    assert placed(cfg, batches=[older, newer]).batch.lot == "A"
    cfg["batches"]["recency_key"] = "report_date"
    undated = [batch(lot="A", report_date=None, expiry_date=dt.date(2028, 1, 1)), batch(lot="B", report_date=None, expiry_date=dt.date(2029, 1, 1))]
    p = placed(cfg, batches=undated)
    assert p.batch.lot == "B" and "no report date; batch chosen by expiry" in p.flags


def test_inconsistent_batches_flag_and_totox_range(cfg):
    newest = batch(lot="NEW", report_date=dt.date(2026, 9, 1), totox=5.0)
    bad = batch(lot="BAD", report_date=dt.date(2026, 3, 1), totox=14.0, av=9.0)
    fine = batch(lot="OK", report_date=dt.date(2026, 2, 1), totox=7.0)
    expired_bad = batch(lot="GONE", expiry_date=dt.date(2026, 1, 1), totox=30.0, av=20.0)
    p = placed(cfg, batches=[fine, bad, newest, expired_bad])
    assert p.list_name == RANKED and p.batch.lot == "NEW" and p.inconsistent is True
    assert p.totox_range == (5.0, 14.0)                                  # expired batch is not in the range
    assert any(f.startswith("inconsistent batches: 1 of 2 other") and "TOTOX range 5 to 14" in f for f in p.flags)
    assert placed(cfg, batches=[fine, newest]).inconsistent is False
    acid_bad = batch(lot="ACID", report_date=dt.date(2026, 3, 1), acid_value=4.0)
    assert placed(cfg, batches=[newest, acid_bad]).inconsistent is True
    metal_bad = batch(lot="PB", report_date=dt.date(2026, 3, 1), contaminants=[contaminant("lead", 0.2, 0.05, "no")])
    assert placed(cfg, batches=[newest, metal_bad]).inconsistent is True


def test_report_problems(cfg):
    assert placed(cfg, batches=[batch(status="no_results")]).reason == "raw-material certificate, no batch results"
    p = placed(cfg, batches=[batch(status="failed")])
    assert (p.list_name, p.reason) == (INCOMPLETE, "report could not be extracted")
    assert placed(cfg, batches=[batch(status=None)]).reason == "report not available"
    # A failed report does not hide a good one.
    assert placed(cfg, batches=[batch(lot="X", status="failed"), batch(lot="Y")]).batch.lot == "Y"


# --- scoring (LOCKED) -------------------------------------------------------------------
def test_oxidation_load_formula(cfg):
    p = placed(cfg)                       # TOTOX 6, 600 mg EPA+DHA in a 1000 mg capsule
    assert p.epa_dha_mg_g == 600 and p.oil_g_per_target == pytest.approx(1.6667, abs=1e-4)
    assert p.score == pytest.approx(6.0 * 1000 / 600, abs=1e-3) == pytest.approx(10.0)
    concentrated = place(product(label=LabelFacts(status="verified", oil_mg_per_capsule=700)), cfg, TODAY)
    assert concentrated.score == pytest.approx(6.0 * 700 / 600, abs=1e-3)      # same TOTOX, less oil needed, lower load
    cfg["scoring"]["target_epa_dha_mg"] = 2000
    assert placed(cfg).score == pytest.approx(20.0)


def test_per_gram_report_needs_no_capsule_weight(cfg):
    per_gram = batch(basis_text="mg per g", basis_kind="gram", epa_mg_cap=None, dha_mg_cap=None, total_omega3_mg_cap=None,
                     epa_mg_g=500, dha_mg_g=250, total_omega3_mg_g=800)
    p = placed(cfg, batches=[per_gram], label=None)
    assert p.list_name == RANKED and p.score == pytest.approx(6.0 * 1000 / 750) and p.epa_dha_mg_cap is None
    with_oil = placed(cfg, batches=[per_gram], label=LabelFacts(status="verified", oil_mg_per_capsule=1200))
    assert with_oil.epa_dha_mg_cap == 900 and with_oil.score == p.score            # mg/g x label oil grams


def test_missing_oil_weight_is_excluded_not_guessed(cfg):
    p = placed(cfg, label=None)
    assert p.list_name == OIL_UNAVAILABLE and p.score is None and "not found on the label" in p.reason
    assert p.optimistic_score == pytest.approx(6.0 * 700 / 600)                 # bound: capsule is all omega-3
    impossible = placed(cfg, label=LabelFacts(status="verified", oil_mg_per_capsule=500))    # 700 mg omega-3 in 500 mg oil
    assert impossible.list_name == OIL_UNAVAILABLE and "less than the omega-3" in impossible.reason
    absurd = placed(cfg, label=LabelFacts(status="verified", oil_mg_per_capsule=9000))
    assert absurd.list_name == OIL_UNAVAILABLE and "outside the plausible range" in absurd.reason


def test_oil_weight_estimated_from_ifos_area_percent(cfg):
    area = {"epa": {"result_pct": 40.0}, "dha": {"result_pct": 20.0}}
    p = placed(cfg, label=None, batches=[batch(area_pct=area)])
    assert p.list_name == RANKED and p.oil_source == "ifos_area_pct" and p.oil_mg_per_capsule == 1000
    assert "oil weight estimated from IFOS area %" in p.flags
    labelled = placed(cfg, batches=[batch(area_pct=area)], label=LabelFacts(status="verified", oil_mg_per_capsule=1100))
    assert labelled.oil_source == "label" and labelled.oil_mg_per_capsule == 1100       # label wins
    cfg["oil_weight"]["sources"] = ["label"]
    assert placed(cfg, label=None, batches=[batch(area_pct=area)]).list_name == OIL_UNAVAILABLE


def test_missing_epa_or_dha_is_null_and_labelled(cfg):
    p = placed(cfg, batches=[batch(epa_mg_cap=None, total_omega3_mg_cap=300)])
    assert p.list_name == RANKED and p.potency_label == "DHA only" and "DHA only" in p.flags
    assert p.epa_dha_mg_cap == 200 and p.epa_dha_ratio is None and p.score == pytest.approx(6.0 * 1000 / 200)
    p = placed(cfg, batches=[batch(dha_mg_cap=None)])
    assert p.potency_label == "EPA only" and p.epa_dha_mg_cap == 400
    p = placed(cfg, batches=[batch(epa_mg_cap=None, dha_mg_cap=None)])
    assert p.list_name == INCOMPLETE and p.reason == "no EPA or DHA result"


def test_display_values(cfg):
    p = placed(cfg, rda="Take 2 softgels daily with food.")
    assert (p.epa_dha_mg_cap, p.serving_capsules, p.epa_dha_mg_serving, p.epa_dha_ratio) == (600, 2, 1200, 2.0)
    p = placed(cfg, batches=[batch(basis_text="mg / 3 softgels", basis_count=3)])
    assert p.serving_capsules == 3
    labelled = LabelFacts(status="verified", oil_mg_per_capsule=1000, serving_capsules=4, antioxidants=["mixed tocopherols"], packaging=["amber bottle"])
    p = placed(cfg, label=labelled)
    assert p.serving_capsules == 4 and "antioxidants: mixed tocopherols" in p.flags and "packaging: amber bottle" in p.flags


def test_antioxidants_and_packaging_never_change_the_score(cfg):
    plain = placed(cfg)
    decorated = placed(cfg, label=LabelFacts(status="verified", oil_mg_per_capsule=1000, antioxidants=["rosemary"], packaging=["blister"]))
    assert plain.score == decorated.score and plain.list_name == decorated.list_name == RANKED


# --- ties (LOCKED) ------------------------------------------------------------------------
def _scored(cfg, num, totox, av, epa=400):
    return product(num=num, batches=[batch(totox=totox, av=av, epa_mg_cap=epa)])


def test_tie_groups_break_on_av_then_epa_dha(cfg):
    # oil 1000 mg, EPA+DHA 600 -> score = TOTOX / 0.6: scores 10.0, 10.9, 11.5
    result = rank_products([_scored(cfg, "A", 6.0, 5.0), _scored(cfg, "B", 6.54, 2.0), _scored(cfg, "C", 6.9, 1.0)], cfg, TODAY)
    ranked = result.of(RANKED)
    assert [round(p.score, 1) for p in sorted(ranked, key=lambda p: p.score)] == [10.0, 10.9, 11.5]
    # 10.9 is within 10% of 10.0: tie, lower AV wins. 11.5 is not within 10% of the anchor 10.0: next group.
    assert [(p.product.product_num, p.rank, p.tie_group) for p in ranked] == [("B", 1, 1), ("A", 2, 1), ("C", 3, 2)]


def test_tie_break_falls_to_epa_dha_when_av_is_equal(cfg):
    # same AV; "RICH" has more EPA+DHA per capsule and a slightly worse score, still inside the tie
    lean = product(num="LEAN", batches=[batch(totox=6.0, av=3.0, epa_mg_cap=400, dha_mg_cap=200)],
                   label=LabelFacts(status="verified", oil_mg_per_capsule=1000))
    rich = product(num="RICH", batches=[batch(totox=6.3, av=3.0, epa_mg_cap=500, dha_mg_cap=250, total_omega3_mg_cap=800)],
                   label=LabelFacts(status="verified", oil_mg_per_capsule=1250))
    ranked = rank_products([lean, rich], cfg, TODAY).of(RANKED)
    assert [p.product.product_num for p in ranked] == ["RICH", "LEAN"] and ranked[0].score > ranked[1].score


def test_tie_margin_comes_from_config(cfg):
    products = [_scored(cfg, "A", 6.0, 5.0), _scored(cfg, "B", 6.54, 2.0)]
    assert [p.product.product_num for p in rank_products(products, cfg, TODAY).of(RANKED)] == ["B", "A"]
    cfg["scoring"]["tie_margin"] = 0.05
    assert [p.product.product_num for p in rank_products(products, cfg, TODAY).of(RANKED)] == ["A", "B"]


def test_ranking_is_deterministic_for_identical_scores(cfg):
    products = [_scored(cfg, n, 6.0, 4.0) for n in ("Z", "M", "A")]
    assert [p.product.product_num for p in rank_products(products, cfg, TODAY).of(RANKED)] == ["A", "M", "Z"]
    assert [p.product.product_num for p in rank_products(products[::-1], cfg, TODAY).of(RANKED)] == ["A", "M", "Z"]


# --- separate lists -----------------------------------------------------------------------
def test_flavored_products_are_ranked_separately(cfg):
    lemon = product(num="LEMON", name="rTG Omega-3 Lemon", batches=[batch(totox=2.0)])
    plain = product(num="PLAIN", batches=[batch(totox=6.0)])
    unknown = product(num="UNKNOWN", name="rTG Omega-3")
    labelled = product(num="LABEL", name="rTG Omega-3", label=LabelFacts(status="verified", oil_mg_per_capsule=1000, flavored=True, flavor_evidence="natural lemon flavor"))
    result = rank_products([lemon, plain, unknown, labelled], cfg, TODAY)
    assert {p.product.product_num for p in result.of(FLAVORED)} == {"LEMON", "LABEL"}
    assert [p.rank for p in result.of(FLAVORED)] == [1, 2] and result.of(FLAVORED)[0].product.product_num == "LEMON"
    assert {p.product.product_num for p in result.of(RANKED)} == {"PLAIN", "UNKNOWN"}       # a better flavored score does not displace them
    assert "flavor not stated" in next(p for p in result.placements if p.product.product_num == "UNKNOWN").flags
    assert any(f.startswith("flavored") and "anisidine" in f for f in result.of(FLAVORED)[0].flags)


def test_pet_products_have_their_own_list(cfg):
    p = placed(cfg, name="rTG anchovy pet fish oil for cat")
    assert p.list_name == PET and "'pet'" in p.reason
    assert placed(cfg, name="rTG Omega", rda="Dogs under 10 kg: 1 capsule").list_name == PET
    assert placed(cfg, name="rTG Catalyst Petite Omega").list_name == RANKED          # whole words only
    cfg["filters"]["exclude_pet_products"] = False
    assert placed(cfg, name="rTG anchovy pet fish oil for cat").list_name == RANKED


def test_one_list_per_product_and_all_reasons_kept(cfg):
    p = placed(cfg, name="Fish Oil", label=None, batches=[batch(totox=12.0, av=9.0)])
    assert p.list_name == EXCLUDED and p.reason.startswith("oxidation cutoff")       # a hard failure outranks 'unverified'
    assert any(r.startswith("form unverified") for r in p.all_reasons) and len(p.all_reasons) >= 2
    liquid = placed(cfg, name="Krill Liquid", product_type="Liquids", batches=[batch(totox=30.0)])
    assert liquid.reason.startswith("not a capsule")                                  # format is reported first


def test_exclusion_counts_by_reason(cfg):
    products = [product(num="A"), product(num="B", name="Omega EE Ethyl Ester"), product(num="C", name="Omega Ethyl Esters"),
                product(num="D", batches=[batch(totox=11.0)]), product(num="E", batches=[batch(totox=15.0)]),
                product(num="F", product_type="Liquids"), product(num="G", name="Fish Oil", label=None)]
    result = rank_products(products, cfg, TODAY)
    assert result.counts()[RANKED] == 1 and sum(result.counts().values()) == 7
    assert result.exclusion_counts() == {"ethyl ester form": 2, "oxidation cutoff": 2, "not a capsule": 1, "form unverified, not attempted": 1}


# --- agent queue -----------------------------------------------------------------------------
def test_agent_queue_is_ordered_by_best_possible_score_with_a_stopping_threshold(cfg):
    cfg["scoring"]["top_n"] = 2
    ranked = [_scored(cfg, "R1", 3.0, 2.0), _scored(cfg, "R2", 6.0, 2.0)]                    # scores 5.0 and 10.0
    near = product(num="NEAR", name="Fish Oil", label=None, batches=[batch(totox=4.0)])     # bound 4*700/600 = 4.67
    far = product(num="FAR", name="Fish Oil", label=None, batches=[batch(totox=9.9)])       # bound 11.55
    failing = product(num="FAIL", name="Fish Oil", label=None, batches=[batch(totox=12.0)])  # never verified: fails a cutoff
    need_oil = product(num="OIL", name="rTG Fish Oil", label=None, batches=[batch(totox=5.0)])
    result = rank_products(ranked + [near, far, failing, need_oil], cfg, TODAY)
    queue, threshold = agent_queue(result, cfg)
    assert [p.product.product_num for p in queue] == ["NEAR", "OIL", "FAR"]
    assert threshold == pytest.approx(10.0 * 1.10)
    assert [p.product.product_num for p in queue if p.optimistic_score <= threshold] == ["NEAR", "OIL"]   # FAR cannot reach the top 2
    assert agent_queue(rank_products(ranked[:1] + [near], cfg, TODAY), cfg)[1] is None      # fewer than N ranked: no cut-off yet


# --- thresholds come from config --------------------------------------------------------------
def test_every_cutoff_is_read_from_config(cfg):
    b = [batch(totox=9.0, pv=2.5, av=7.0, acid_value=2.0)]
    assert placed(cfg, batches=b).list_name == RANKED
    for path, value in ((("oxidation", "totox_max_exclusive"), 9.0), (("oxidation", "pv_max"), 2.0), (("oxidation", "av_max"), 6.0)):
        original = cfg["filters"][path[0]][path[1]]
        cfg["filters"][path[0]][path[1]] = value
        assert placed(cfg, batches=b).list_name == EXCLUDED, path
        cfg["filters"][path[0]][path[1]] = original
    cfg["filters"]["acid_value_max"] = 1.5
    assert placed(cfg, batches=b).list_name == EXCLUDED
    cfg["filters"]["acid_value_max"] = 3
    cfg["filters"]["oxidation"]["totox_preferred_max"] = 9.5
    assert placed(cfg, batches=b).preferred is True


def test_locked_defaults_in_config_yaml(cfg):
    ox = cfg["filters"]["oxidation"]
    assert (ox["totox_max_exclusive"], ox["pv_max"], ox["av_max"], ox["totox_preferred_max"]) == (10, 3, 8, 8)
    assert cfg["filters"]["acid_value_max"] == 3 and cfg["scoring"]["tie_margin"] == 0.10
    assert cfg["scoring"]["target_epa_dha_mg"] == 1000 and cfg["formats"]["included"] == ["Capsules", "Softgels"]
    assert cfg["agent"]["max_tool_calls"] == 8 and cfg["llm"]["api_enabled"] is False


# --- SQLite round trip ---------------------------------------------------------------------------
def test_load_from_sqlite_and_save_results(cfg, conn, fixtures):
    from omega3.extract.run import extract_all
    from omega3.scrape.reports import store_report
    now = db.now_iso()
    db.upsert(conn, "products", dict(product_num="BEBI0001", brand="Better Bioscience", name="Joy Fish Oil Concentrate 85% omega-3 rTG",
                                     product_type="Softgels", rda="Take 2 softgels daily", format_decision="include",
                                     first_seen=now, last_checked=now), keys=("product_num",))
    db.upsert(conn, "batches", dict(product_num="BEBI0001", lot="JFO202501", expiry_date="2027-05-05", report_filename="joy.pdf",
                                    first_seen=now, last_seen=now), keys=("product_num", "lot"))
    store_report(cfg, conn, "joy.pdf", (fixtures / "pdf" / "BEBI0001_per_2_softgels_rtg.pdf").read_bytes(), {}, "http://u/joy.pdf")
    extract_all(cfg, conn, log=lambda *_: None)
    db.upsert(conn, "label_facts", dict(product_num="BEBI0001", status="verified", oil_mg_per_capsule=600, verified_at=now,
                                        antioxidants=["vitamin E"], packaging=[]), keys=("product_num",), keep=())
    conn.commit()

    [loaded] = load_products(conn)
    b = loaded.batches[0]
    assert (b.epa_mg_cap, b.dha_mg_cap, b.pv, b.av, b.totox, b.report_date) == (371, 128, 2.8, 3.0, 8.6, dt.date(2025, 9, 8))
    assert len(b.contaminants) == 7 and all(c.passes() for c in b.contaminants)
    result = rank_products([loaded], cfg, TODAY)
    [p] = result.of(RANKED)
    assert (p.form, p.form_source, p.preferred) == ("rTG", "IFOS listing", False)      # TOTOX 8.6: passes, not preferred
    assert p.score == pytest.approx(8.6 * 600 / 499, abs=1e-2) and p.epa_dha_mg_serving == 998

    save_results(conn, "run-1", result, {"ranked": 1})
    row = conn.execute("SELECT list_name, rank, score, lot FROM results").fetchone()
    assert tuple(row) == ("ranked", 1, p.score, "JFO202501")
    assert placement_row(p)["flags"] == p.flags
