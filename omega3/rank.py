"""Filters, scoring and ranking. Pure code: no I/O, no network, no LLM.

Everything here is a function of the records passed in and config.yaml. The
locked criteria live in this file; every threshold comes from config.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field

from .extract.terms import CONTAMINANTS, TG_FAMILY, classify_form
from .scrape.listing import decide_format

# List names. A product lands in exactly one.
RANKED, FLAVORED = "ranked", "flavored"
FORM_UNVERIFIED, MONOGLYCERIDE, INCOMPLETE, OIL_UNAVAILABLE, PET, EXCLUDED = (
    "form_unverified", "monoglyceride", "incomplete_data", "oil_weight_unavailable", "pet", "excluded")
LIST_ORDER = [RANKED, FLAVORED, FORM_UNVERIFIED, MONOGLYCERIDE, INCOMPLETE, OIL_UNAVAILABLE, PET, EXCLUDED]


@dataclass
class Contaminant:
    analyte: str
    label: str
    result_text: str | None
    result_value: float | None
    limit_text: str | None
    limit_value: float | None
    limit_op: str | None
    compliance: str | None

    def passes(self) -> bool | None:
        """True/False against IFOS's stated limit; None when it cannot be judged."""
        numeric = None
        if self.result_value is not None and self.limit_value is not None:
            numeric = self.result_value < self.limit_value if self.limit_op == "<" else self.result_value <= self.limit_value
        if self.compliance == "no" or numeric is False:
            return False
        if self.compliance == "yes" or numeric is True:
            return True
        return None


@dataclass
class Batch:
    lot: str
    expiry_date: dt.date | None
    report_filename: str | None = None
    report_date: dt.date | None = None
    status: str | None = None              # extraction status: ok | no_results | failed | None (not extracted)
    method: str | None = None
    basis_text: str | None = None
    basis_kind: str | None = None
    basis_count: float | None = None
    epa_mg_cap: float | None = None
    dha_mg_cap: float | None = None
    total_omega3_mg_cap: float | None = None
    epa_mg_g: float | None = None
    dha_mg_g: float | None = None
    total_omega3_mg_g: float | None = None
    pv: float | None = None
    av: float | None = None
    totox: float | None = None
    totox_source: str | None = None
    acid_value: float | None = None
    censored: list[str] = field(default_factory=list)
    contaminants: list[Contaminant] = field(default_factory=list)
    report_form: str | None = None
    report_form_evidence: str | None = None
    area_pct: dict = field(default_factory=dict)
    directions: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class LabelFacts:
    status: str = "unverified"             # verified | unverified
    form: str | None = None
    form_source_url: str | None = None
    form_evidence: str | None = None
    oil_mg_per_capsule: float | None = None
    oil_source_url: str | None = None
    oil_evidence: str | None = None
    serving_capsules: float | None = None
    servings_per_bottle: float | None = None
    flavored: bool | None = None
    flavor_evidence: str | None = None
    antioxidants: list[str] = field(default_factory=list)
    packaging: list[str] = field(default_factory=list)
    reasoning: str | None = None


@dataclass
class Product:
    product_num: str
    name: str
    brand: str | None = None
    summary: str = ""
    product_type: str | None = None
    rda: str | None = None
    website: str | None = None
    batches: list[Batch] = field(default_factory=list)
    label: LabelFacts | None = None


@dataclass
class Placement:
    product: Product
    list_name: str
    reason: str = ""
    batch: Batch | None = None
    rank: int | None = None
    tie_group: int | None = None
    score: float | None = None
    optimistic_score: float | None = None    # best possible score if oil weight were minimal
    flags: list[str] = field(default_factory=list)
    all_reasons: list[str] = field(default_factory=list)
    form: str | None = None
    form_source: str | None = None
    form_evidence: str | None = None
    oil_mg_per_capsule: float | None = None
    oil_source: str | None = None
    epa_dha_mg_cap: float | None = None
    epa_dha_mg_g: float | None = None
    epa_dha_mg_serving: float | None = None
    serving_capsules: float | None = None
    epa_dha_ratio: float | None = None
    potency_label: str = ""                  # "", "EPA only", "DHA only"
    oil_g_per_target: float | None = None
    preferred: bool = False
    flavored: bool | None = None
    inconsistent: bool = False
    totox_range: tuple[float, float] | None = None
    unexpired_batches: int = 0
    expired_dropped: int = 0


@dataclass
class RankResult:
    as_of: dt.date
    placements: list[Placement]

    def of(self, list_name: str) -> list[Placement]:
        items = [p for p in self.placements if p.list_name == list_name]
        if list_name in (RANKED, FLAVORED):
            return sorted(items, key=lambda p: p.rank)
        return sorted(items, key=lambda p: (p.reason, p.product.brand or "", p.product.name))

    @property
    def inconsistent(self) -> list[Placement]:
        return [p for p in self.placements if p.inconsistent]

    def counts(self) -> dict[str, int]:
        return {name: len(self.of(name)) for name in LIST_ORDER}

    def exclusion_counts(self) -> dict[str, int]:
        """Products outside the two ranked lists, counted by reason."""
        return dict(Counter(_reason_key(p) for p in self.placements if p.list_name not in (RANKED, FLAVORED)).most_common())


def _reason_key(p: Placement) -> str:
    """Collapse per-product detail ('TOTOX 12.4 >= 10') into a countable reason."""
    return re.sub(r"\s*[:(].*$", "", p.reason).strip() or p.list_name


def _word(text: str, words: list[str]) -> str | None:
    for word in words:
        if re.search(rf"(?<![A-Za-z]){re.escape(word).replace(chr(92) + ' ', '[ -]?')}(?![A-Za-z])", text, re.I):
            return word
    return None


# --- single-batch checks --------------------------------------------------------
def oxidation_failures(batch: Batch, cfg) -> list[str]:
    """LOCKED cutoffs: TOTOX < 10, PV <= 3, AV <= 8. Censored '<x' values are judged at x."""
    ox = cfg["filters"]["oxidation"]
    out = []
    if batch.totox is not None and not batch.totox < ox["totox_max_exclusive"]:
        out.append(f"TOTOX {batch.totox:g} is not below {ox['totox_max_exclusive']:g}")
    if batch.pv is not None and not batch.pv <= ox["pv_max"]:
        out.append(f"PV {batch.pv:g} is above {ox['pv_max']:g}")
    if batch.av is not None and not batch.av <= ox["av_max"]:
        out.append(f"AV {batch.av:g} is above {ox['av_max']:g}")
    return out


def acid_failure(batch: Batch, cfg) -> str | None:
    limit = cfg["filters"]["acid_value_max"]
    if batch.acid_value is not None and not batch.acid_value <= limit:
        return f"acid value {batch.acid_value:g} is above {limit:g}"
    return None


def contaminant_failures(batch: Batch) -> list[str]:
    return [f"{c.label} {c.result_text} against limit {c.limit_text}" for c in batch.contaminants if c.passes() is False]


def cutoff_failures(batch: Batch, cfg) -> list[str]:
    acid = acid_failure(batch, cfg)
    return oxidation_failures(batch, cfg) + ([acid] if acid else []) + contaminant_failures(batch)


# --- product-level helpers --------------------------------------------------------
def recency_key(batch: Batch, cfg) -> tuple:
    """Newest first. Report date stands in for the unpublished test date (P-1)."""
    floor = dt.date.min
    report, expiry = batch.report_date or floor, batch.expiry_date or floor
    primary = (report, expiry) if cfg["batches"]["recency_key"] == "report_date" else (expiry, report)
    return (*primary, batch.lot)


def detect_flavored(product: Product, cfg) -> tuple[bool | None, str | None]:
    """(flavored, evidence). The label wins; otherwise listing keywords; else unknown."""
    if product.label and product.label.flavored is not None:
        return product.label.flavored, product.label.flavor_evidence or "label"
    text = f"{product.name} {product.summary}"
    word = _word(text, cfg["filters"]["unflavored_words"])
    if word:
        return False, f"'{word}' in IFOS listing"
    word = _word(text, cfg["filters"]["flavor_words"])
    if word:
        return True, f"'{word}' in IFOS listing"
    return None, None


def detect_pet(product: Product, cfg) -> str | None:
    return _word(f"{product.name} {product.rda or ''}", cfg["filters"]["pet_words"])


def resolve_form(product: Product, batches: list[Batch]) -> tuple[str | None, str | None, str]:
    """(form, source, evidence). Sources: verified label facts, the IFOS report's
    own comment, the IFOS listing text. A form is accepted only when every
    source that states one agrees on the family; disagreement stays unverified."""
    found: list[tuple[str, str, str]] = []
    if product.label and product.label.status == "verified" and product.label.form:
        found.append((product.label.form, "label", f"{product.label.form_evidence or ''} [{product.label.form_source_url or ''}]".strip()))
    for batch in batches:
        if batch.report_form:
            found.append((batch.report_form, "IFOS report", f"{batch.report_form_evidence} [lot {batch.lot}]"))
            break
    listing_form, listing_evidence = classify_form(f"{product.name}. {product.summary}")
    if listing_form:
        found.append((listing_form, "IFOS listing", listing_evidence))
    elif listing_evidence.startswith("conflicting"):
        return None, None, f"IFOS listing: {listing_evidence}"
    if not found:
        reason = product.label.reasoning if product.label and product.label.reasoning else "no form stated"
        return None, None, reason
    families = {"TG" if form in TG_FAMILY else form for form, _, _ in found}
    if len(families) > 1:
        return None, None, "sources disagree: " + "; ".join(f"{src} says {form}" for form, src, _ in found)
    form, source, evidence = found[0]
    if families == {"TG"} and any(f == "rTG" for f, _, _ in found):
        form = "rTG"                                   # the more specific reading
    return form, source, evidence


def serving_capsules(product: Product, batch: Batch) -> float | None:
    if product.label and product.label.serving_capsules:
        return product.label.serving_capsules
    if batch.basis_kind == "capsule" and batch.basis_count and batch.basis_count > 1:
        return batch.basis_count
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    text = batch.directions or product.rda or ""
    m = re.search(r"\b(\d+|one|two|three|four|five|six)\s*(?:\(\d+\)\s*)?(?:mini\s+|small\s+)?(?:softgels?|soft\s*gels?|capsules?|caps?|gelcaps?)\b", text, re.I)
    if not m:
        return None
    token = m.group(1).lower()
    return float(words.get(token, token)) if token in words or token.isdigit() else None


def resolve_concentration(product: Product, batch: Batch, cfg) -> dict:
    """EPA+DHA per capsule and per gram of oil, and the oil weight that links them.

    Per-gram reports give concentration directly. Per-capsule reports need the
    capsule's oil weight: from the label, else estimated from the area-% figures
    some reports print. No source means no concentration and no score: the oil
    weight is never guessed (LOCKED).
    """
    present = [v for v in (batch.epa_mg_cap, batch.dha_mg_cap) if v is not None]
    present_g = [v for v in (batch.epa_mg_g, batch.dha_mg_g) if v is not None]
    out = {"mg_cap": sum(present) if present else None, "mg_g": sum(present_g) if present_g else None,
           "oil_mg": None, "oil_source": None, "flags": [], "problem": None}
    label_oil = product.label.oil_mg_per_capsule if product.label else None
    bounds = cfg["validation"]["oil_mg_per_capsule"]
    sources = cfg["oil_weight"]["sources"]

    if out["mg_g"] is not None:                         # report already per gram of oil
        if label_oil and "label" in sources:
            out.update(oil_mg=label_oil, oil_source="label", mg_cap=round(out["mg_g"] * label_oil / 1000, 1))
        return out
    if out["mg_cap"] is None:
        return out

    candidates: list[tuple[float, str]] = []
    if label_oil and "label" in sources:
        candidates.append((label_oil, "label"))
    if "ifos_area_pct" in sources:
        pct = [batch.area_pct.get(k, {}).get("result_pct") for k, v in (("epa", batch.epa_mg_cap), ("dha", batch.dha_mg_cap)) if v is not None]
        if pct and all(p for p in pct) and 0 < sum(pct) <= 100:
            candidates.append((round(out["mg_cap"] / (sum(pct) / 100), 1), "ifos_area_pct"))
    floor = (batch.total_omega3_mg_cap or out["mg_cap"]) * cfg["oil_weight"]["min_ratio_to_total_omega3"]
    for oil_mg, source in candidates:
        if not bounds[0] <= oil_mg <= bounds[1]:
            out["problem"] = f"oil weight {oil_mg:g} mg from {source} is outside the plausible range"
        elif oil_mg < floor:
            out["problem"] = f"oil weight {oil_mg:g} mg from {source} is less than the omega-3 it is reported to contain"
        else:
            out.update(oil_mg=oil_mg, oil_source=source, mg_g=round(out["mg_cap"] / oil_mg * 1000, 1), problem=None)
            if source == "ifos_area_pct":
                out["flags"].append("oil weight estimated from IFOS area %")
            break
    return out


def potency_label(batch: Batch) -> str:
    epa = batch.epa_mg_cap if batch.epa_mg_cap is not None else batch.epa_mg_g
    dha = batch.dha_mg_cap if batch.dha_mg_cap is not None else batch.dha_mg_g
    if epa is not None and dha is None:
        return "EPA only"
    if dha is not None and epa is None:
        return "DHA only"
    return ""


# --- placement of one product -------------------------------------------------------
def place(product: Product, cfg, today: dt.date) -> Placement:
    p = Placement(product=product, list_name=EXCLUDED)
    reasons: list[tuple[str, str]] = []          # (list, reason), most decisive first

    unit = next((b.basis_text for b in product.batches if b.basis_text), None)
    decision, format_reason, chewable = decide_format(product.product_type, product.name, product.summary, cfg,
                                                      rda=product.rda, report_unit=unit or "")
    if decision != "include":
        reasons.append((EXCLUDED, format_reason))
    elif chewable:
        p.flags.append("chewable softgel")
    pet_word = detect_pet(product, cfg)
    if pet_word and cfg["filters"]["exclude_pet_products"]:
        reasons.append((PET, f"pet product: '{pet_word}' in name or directions"))

    # Batches: drop expired, then rank on the most recently tested one (LOCKED).
    current = [b for b in product.batches if b.expiry_date is None or b.expiry_date >= today]
    p.expired_dropped = len(product.batches) - len(current)
    p.unexpired_batches = len(current)
    usable = sorted((b for b in current if b.status == "ok"), key=lambda b: recency_key(b, cfg), reverse=True)
    if not current:
        reasons.append((EXCLUDED, "no unexpired batch"))
    elif not usable:
        if any(b.status == "no_results" for b in current):
            reasons.append((EXCLUDED, "raw-material certificate, no batch results"))
        elif any(b.status == "failed" for b in current):
            reasons.append((INCOMPLETE, "report could not be extracted"))
        else:
            reasons.append((INCOMPLETE, "report not available"))
    batch = usable[0] if usable else None
    p.batch = batch

    if batch is not None:
        if batch.basis_kind not in ("capsule", "gram", "percent") and decision == "include":
            reasons.append((INCOMPLETE, f"potency reported per '{batch.basis_text}', not per capsule or gram"))
        missing = [name for name, value in (("PV", batch.pv), ("AV", batch.av)) if value is None]
        if missing:
            reasons.append((INCOMPLETE, f"missing {' and '.join(missing)}: oxidation cannot be assessed"))
        ox = oxidation_failures(batch, cfg)
        if ox:
            reasons.append((EXCLUDED, "oxidation cutoff: " + "; ".join(ox)))
        acid = acid_failure(batch, cfg)
        if acid:
            reasons.append((EXCLUDED, "acid value cutoff: " + acid))
        contaminants = contaminant_failures(batch)
        if contaminants:
            reasons.append((EXCLUDED, "contaminant over IFOS limit: " + "; ".join(contaminants)))

        # Flags that never exclude.
        if batch.acid_value is None:
            p.flags.append("acid value missing")
        reported = {c.analyte for c in batch.contaminants if c.result_value is not None or c.compliance}
        absent = [a for a in CONTAMINANTS if a not in reported]
        if absent:
            p.flags.append("contaminants not reported: " + ", ".join(absent))
        if batch.totox_source == "computed":
            p.flags.append("TOTOX computed as 2*PV+AV")
        if set(batch.censored) & {"pv", "av", "totox"}:
            p.flags.append("oxidation value reported as '<x'; evaluated at x")
        if batch.method == "llm":
            p.flags.append("values transcribed by LLM fallback")
        if batch.report_date is None:
            p.flags.append("no report date; batch chosen by expiry")
        ox_limit = cfg["filters"]["oxidation"]["totox_preferred_max"]
        p.preferred = batch.totox is not None and batch.totox <= ox_limit

        # Inconsistent batches: any other unexpired batch failing any cutoff.
        others = [b for b in usable[1:] if cutoff_failures(b, cfg)]
        totoxes = [b.totox for b in usable if b.totox is not None]
        if totoxes:
            p.totox_range = (min(totoxes), max(totoxes))
        if others:
            p.inconsistent = True
            p.flags.append(f"inconsistent batches: {len(others)} of {len(usable) - 1} other unexpired batch(es) fail a cutoff"
                           + (f"; TOTOX range {p.totox_range[0]:g} to {p.totox_range[1]:g}" if p.totox_range else ""))

    # Oil form (LOCKED: TG and rTG only).
    p.form, p.form_source, p.form_evidence = resolve_form(product, usable)
    if p.form in ("EE", "PL"):
        reasons.append((EXCLUDED, {"EE": "ethyl ester form", "PL": "phospholipid or krill oil"}[p.form] + f" ({p.form_source})"))
    elif p.form == "MAG":
        reasons.append((MONOGLYCERIDE, f"monoglyceride form ({p.form_source})"))
    elif p.form is None:
        attempted = product.label is not None
        reasons.append((FORM_UNVERIFIED, ("form unverified: " if attempted else "form unverified, not attempted: ") + p.form_evidence))

    # Concentration and score.
    if batch is not None:
        p.potency_label = potency_label(batch)
        if p.potency_label:
            p.flags.append(p.potency_label)
        conc = resolve_concentration(product, batch, cfg)
        p.flags.extend(conc["flags"])
        p.epa_dha_mg_cap, p.epa_dha_mg_g = conc["mg_cap"], conc["mg_g"]
        p.oil_mg_per_capsule, p.oil_source = conc["oil_mg"], conc["oil_source"]
        epa = batch.epa_mg_cap if batch.epa_mg_cap is not None else batch.epa_mg_g
        dha = batch.dha_mg_cap if batch.dha_mg_cap is not None else batch.dha_mg_g
        p.epa_dha_ratio = round(epa / dha, 2) if epa is not None and dha else None
        p.serving_capsules = serving_capsules(product, batch)
        if p.serving_capsules and p.epa_dha_mg_cap is not None:
            p.epa_dha_mg_serving = round(p.epa_dha_mg_cap * p.serving_capsules, 1)
        target = cfg["scoring"]["target_epa_dha_mg"]
        if conc["mg_cap"] is None and conc["mg_g"] is None:
            if batch.basis_kind in ("capsule", "gram", "percent"):
                reasons.append((INCOMPLETE, "no EPA or DHA result"))
        elif not p.epa_dha_mg_g:
            detail = conc["problem"] or "capsule oil weight not found on the label"
            reasons.append((OIL_UNAVAILABLE, f"oil weight unavailable: {detail}"))
        else:
            p.oil_g_per_target = round(target / p.epa_dha_mg_g, 4)
            if batch.totox is not None:
                p.score = round(batch.totox * p.oil_g_per_target, 3)
        # Best case for the agent queue: capsule is nothing but its omega-3.
        if batch.totox is not None and conc["mg_cap"]:
            floor_oil = batch.total_omega3_mg_cap or conc["mg_cap"]
            p.optimistic_score = round(batch.totox * floor_oil / conc["mg_cap"] * target / 1000, 3)
        elif p.score is not None:
            p.optimistic_score = p.score

    p.flavored, flavor_evidence = detect_flavored(product, cfg)
    if p.flavored:
        p.flags.append(f"flavored ({flavor_evidence}): anisidine test may read high")
    elif p.flavored is None:
        p.flags.append("flavor not stated")
    if product.label:
        if product.label.antioxidants:
            p.flags.append("antioxidants: " + ", ".join(product.label.antioxidants))
        if product.label.packaging:
            p.flags.append("packaging: " + ", ".join(product.label.packaging))

    p.all_reasons = [reason for _, reason in reasons]
    if reasons:
        # One list per product. Hard exclusions outrank the informational lists.
        priority = {EXCLUDED: 0, PET: 1, INCOMPLETE: 2, MONOGLYCERIDE: 3, FORM_UNVERIFIED: 4, OIL_UNAVAILABLE: 5}
        # The capsule filter and pet list come first whatever else is wrong.
        first = reasons[0]
        chosen = first if first[1].startswith(("not a capsule", "chewable")) else min(reasons, key=lambda r: priority[r[0]])
        p.list_name, p.reason = chosen
        if p.list_name == EXCLUDED and any(r[0] == PET for r in reasons) and not chosen[1].startswith(("not a capsule", "chewable")):
            p.list_name, p.reason = next(r for r in reasons if r[0] == PET)
    else:
        p.list_name = FLAVORED if p.flavored else RANKED
    return p


# --- ranking ----------------------------------------------------------------------
def assign_ranks(placements: list[Placement], cfg) -> None:
    """LOCKED: lower oxidation load is better; scores within the tie margin of the
    best score in a group are ties, broken by lower AV, then higher EPA+DHA per
    capsule. Groups are anchored on their best score (DECISIONS P-3)."""
    margin = cfg["scoring"]["tie_margin"]
    remaining = sorted(placements, key=lambda p: (p.score, p.product.product_num))
    rank, group = 1, 0
    while remaining:
        group += 1
        anchor = remaining[0].score
        tied = [p for p in remaining if p.score <= anchor * (1 + margin) + 1e-12]
        remaining = remaining[len(tied):]
        tied.sort(key=lambda p: (p.batch.av if p.batch.av is not None else float("inf"),
                                 -(p.epa_dha_mg_cap or 0), p.score, p.product.product_num))
        for p in tied:
            p.rank, p.tie_group = rank, group
            rank += 1


def rank_products(products: list[Product], cfg, today: dt.date | None = None) -> RankResult:
    today = today or cfg.today
    placements = [place(product, cfg, today) for product in products]
    for name in (RANKED, FLAVORED):
        assign_ranks([p for p in placements if p.list_name == name], cfg)
    return RankResult(as_of=today, placements=placements)


def agent_queue(result: RankResult, cfg) -> tuple[list[Placement], float | None]:
    """Products worth sending to the label agent, best possible score first, and
    the score a candidate must beat to matter for the top N (DECISIONS D-4).

    A candidate is a product whose only obstacles are an unverified form or a
    missing oil weight. Once a candidate's optimistic score is worse than the
    Nth ranked score by more than the tie margin, nothing later in the queue can
    change the top N.
    """
    top_n, margin = cfg["scoring"]["top_n"], cfg["scoring"]["tie_margin"]
    label_only = {FORM_UNVERIFIED, OIL_UNAVAILABLE}
    candidates = [p for p in result.placements
                  if p.list_name in label_only and p.optimistic_score is not None
                  and all(r.startswith(("form unverified", "oil weight unavailable")) for r in p.all_reasons)]
    candidates.sort(key=lambda p: (p.optimistic_score, p.product.product_num))
    ranked = result.of(RANKED)
    threshold = ranked[top_n - 1].score * (1 + margin) if len(ranked) >= top_n else None
    return candidates, threshold
