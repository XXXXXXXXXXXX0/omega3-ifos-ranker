"""Turn a parsed report into normalized, validated values. Pure functions."""
from __future__ import annotations

import datetime as dt
import re

from .parser import Parsed
from .units import parse_report_expiry


def _result(parsed: Parsed, analyte: str) -> tuple[float | None, int | None]:
    row = parsed.rows.get(analyte)
    if row is None or row.result_value is None:
        return None, None
    return row.result_value, int(row.result_op == "<")


def _spec(parsed: Parsed, analyte: str) -> float | None:
    row = parsed.rows.get(analyte)
    return row.spec_value if row else None


def _in_range(value: float | None, bounds) -> bool:
    return value is None or bounds[0] <= value <= bounds[1]


def norm_lot(lot: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (lot or "").upper())


def derive(parsed: Parsed, cfg, listing_lot: str | None = None, listing_expiry: dt.date | None = None) -> dict:
    """Normalized columns for the `extractions` table.

    Adds `status`/`error` (validation can fail a report the parser accepted)
    and `warnings`. EPA/DHA are normalized to mg per capsule when the report
    unit allows it; per-gram reports keep mg per g and are converted to
    per-capsule later, once the label's oil weight is known.
    """
    v = cfg["validation"]
    warnings = list(parsed.warnings)
    problems: list[str] = []
    out: dict = {"template": parsed.template, "status": parsed.status, "error": parsed.error,
                 "lot_on_report": parsed.lot, "expiry_on_report": parsed.expiry_text, "directions": parsed.directions,
                 "comments": parsed.comments, "report_form": parsed.report_form,
                 "report_form_evidence": parsed.report_form_evidence, "area_pct": parsed.area_pct}
    if parsed.status != "ok":
        out["warnings"] = warnings
        return out

    basis = parsed.basis
    out.update(basis_text=basis.text, basis_kind=basis.kind, basis_count=basis.count, basis_expressed_as=basis.expressed_as)
    for name in ("epa", "dha", "total_omega3", "epa_dha"):
        value, _ = _result(parsed, name)
        out[f"{name}_orig"] = value
        out[f"{name}_spec"] = _spec(parsed, name)
        out[f"{name}_mg_cap"] = out[f"{name}_mg_g"] = None
        if value is None:
            continue
        if basis.kind == "capsule":
            out[f"{name}_mg_cap"] = round(value / basis.count, 3)
        elif basis.kind == "gram":
            out[f"{name}_mg_g"] = round(value / basis.count, 3)
        elif basis.kind == "percent":
            out[f"{name}_mg_g"] = round(value * 10, 3)
    if basis.kind == "capsule":
        out["conversion"] = "none (already per capsule)" if basis.count == 1 else f"divided by {basis.count:g} ({basis.text} to mg per capsule)"
    elif basis.kind == "gram":
        out["conversion"] = ("none (mg per g of oil)" if basis.count == 1 else f"divided by {basis.count:g} ({basis.text} to mg per g)") \
            + "; mg per capsule = mg per g x label oil grams per capsule"
    elif basis.kind == "percent":
        out["conversion"] = "percent x 10 = mg per g (area % treated as weight %; approximate)"
        warnings.append("potency reported as %; treated as mg per g / 10")
    else:
        out["conversion"] = f"not converted: '{basis.text}' is not a per-capsule or per-gram unit"

    for column, analyte in (("pv", "pv"), ("av", "av"), ("acid_value", "acid_value")):
        out[column], out[f"{column}_censored"] = _result(parsed, analyte)
    reported, censored = _result(parsed, "totox")
    pv, av = out["pv"], out["av"]
    computed = round(2 * pv + av, 3) if pv is not None and av is not None else None
    if reported is not None:
        out.update(totox=reported, totox_censored=censored, totox_source="reported")
        if computed is not None and not (out["pv_censored"] or out["av_censored"]) \
                and abs(reported - computed) > cfg["filters"]["totox_crosscheck_tolerance"]:
            warnings.append(f"reported TOTOX {reported:g} differs from 2*PV+AV = {computed:g}")
    elif computed is not None:
        out.update(totox=computed, totox_censored=int(bool(out["pv_censored"] or out["av_censored"])), totox_source="computed")
    else:
        out.update(totox=None, totox_censored=None, totox_source=None)

    # --- plausibility ---------------------------------------------------------
    for name, label in (("epa", "EPA"), ("dha", "DHA")):
        if not _in_range(out[f"{name}_mg_cap"], v["epa_dha_mg_per_capsule"]):
            problems.append(f"implausible {label} {out[f'{name}_mg_cap']:g} mg per capsule")
        if not _in_range(out[f"{name}_mg_g"], v["mg_per_g"]):
            problems.append(f"implausible {label} {out[f'{name}_mg_g']:g} mg per g")
    if not _in_range(out["total_omega3_mg_cap"], v["total_omega3_mg_per_capsule"]):
        problems.append(f"implausible total omega-3 {out['total_omega3_mg_cap']:g} mg per capsule")
    if not _in_range(out["total_omega3_mg_g"], v["mg_per_g"]):
        problems.append(f"implausible total omega-3 {out['total_omega3_mg_g']:g} mg per g")
    for column, label in (("pv", "PV"), ("av", "AV"), ("totox", "TOTOX"), ("acid_value", "acid value")):
        if not _in_range(out[column], v[column]):
            problems.append(f"implausible {label} {out[column]:g}")
    for unit, bounds in (("mg_cap", v["total_omega3_mg_per_capsule"]), ("mg_g", v["mg_per_g"])):
        if out[f"epa_dha_{unit}"] is not None and not (bounds[0] <= out[f"epa_dha_{unit}"] <= bounds[1]):
            problems.append(f"implausible EPA+DHA {out[f'epa_dha_{unit}']:g} ({unit.replace('_', ' per ')})")
    epa_dha = (out["epa_orig"] or 0) + (out["dha_orig"] or 0)
    total = out["total_omega3_orig"]
    tolerance = v["epa_dha_over_total_tolerance"]
    if total is not None and epa_dha > total * (1 + tolerance) + 1:
        problems.append(f"EPA+DHA {epa_dha:g} exceeds total omega-3 {total:g}")
    elif total is not None and epa_dha > total:
        warnings.append(f"EPA+DHA {epa_dha:g} is slightly above total omega-3 {total:g} (within {tolerance:.0%}: method noise)")
    if out["epa_dha_orig"] is not None and out["epa_orig"] is not None and out["dha_orig"] is not None \
            and abs(out["epa_dha_orig"] - epa_dha) > 0.02 * epa_dha + 1:
        warnings.append(f"combined EPA+DHA row {out['epa_dha_orig']:g} differs from EPA + DHA = {epa_dha:g}")

    # --- cross-checks (warnings only) ------------------------------------------
    for row in parsed.rows.values():
        if row.spec_op in ("<=", "<") and row.spec_value is not None and row.result_value is not None and row.compliance:
            passes = row.result_value <= row.spec_value if row.spec_op == "<=" else row.result_value < row.spec_value
            if passes != (row.compliance == "yes"):
                warnings.append(f"{row.label_raw}: result {row.result_text} vs limit {row.spec_text} but compliance says {row.compliance}")
    if listing_lot is not None and parsed.lot and norm_lot(listing_lot) != norm_lot(parsed.lot) \
            and norm_lot(listing_lot) not in norm_lot(parsed.lot) and norm_lot(parsed.lot) not in norm_lot(listing_lot):
        warnings.append(f"lot on report '{parsed.lot}' differs from listing '{listing_lot}'")
    if listing_expiry is not None and parsed.expiry_text:
        on_report = parse_report_expiry(parsed.expiry_text, hint=listing_expiry)
        if on_report is None:
            warnings.append(f"expiry on report not understood: '{parsed.expiry_text}'")
        elif abs((on_report - listing_expiry).days) > 31:
            warnings.append(f"expiry on report {on_report} differs from listing {listing_expiry}")

    if problems:
        out.update(status="failed", error="validation: " + "; ".join(problems))
    out["warnings"] = warnings
    return out
