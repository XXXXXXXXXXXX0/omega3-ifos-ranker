"""Terminology from SPEC.md: report row labels and oil-form vocabulary.

Matching is case-insensitive. Every term is matched as a whole word (a term
may not be glued to a letter on either side), which is what keeps short ones
like TG, EE, PL, AV, PV and FFA from firing inside longer words.
"""
from __future__ import annotations

import re

# canonical analyte -> (category, synonyms as printed on reports)
ANALYTES: dict[str, tuple[str, list[str]]] = {
    "epa": ("potency", ["EPA", "Eicosapentaenoic Acid", "C20:5", "C20:5 n-3", "C20:5n3", "20:5n3", "20:5 n-3"]),
    "dha": ("potency", ["DHA", "Docosahexaenoic Acid", "C22:6", "C22:6 n-3", "C22:6n3", "22:6n3", "22:6 n-3"]),
    "total_omega3": ("potency", ["Total Omega 3", "Total Omega-3", "Total Omega3", "Total n-3", "Total Omega 3s",
                                 "Total Omega-3s", "Total Omega-3 Fatty Acids", "Total Omega 3 Fatty Acids"]),
    "pcbs": ("purity", ["PCBs", "PCB", "Total PCBs", "Total PCB"]),
    "dioxins_furans": ("purity", ["Dioxins and Furans", "Dioxins & Furans", "Dioxin and Furans", "Dioxins/Furans", "PCDD/F", "PCDD/Fs"]),
    "dioxin_like_pcbs": ("purity", ["Dioxin-Like PCBs", "Dioxin Like PCBs", "Dioxin-Like PCB", "DL-PCBs", "DL PCBs"]),
    "acid_value": ("stability", ["Acid Value", "Acidity", "Free Fatty Acids", "FFA"]),
    "pv": ("stability", ["Peroxide Value", "Peroxide", "PV", "POV"]),
    "av": ("stability", ["p-Anisidine Value", "p-Anisidine", "para-Anisidine Value", "Anisidine Value", "Anisidine", "AV", "p-AV", "pAV"]),
    "totox": ("stability", ["TOTOX", "Totox Value", "TOTOX Value", "Total Oxidation", "Total Oxidation Value"]),
    "mercury": ("heavy_metals", ["Mercury", "Total Mercury"]),
    "lead": ("heavy_metals", ["Lead"]),
    "cadmium": ("heavy_metals", ["Cadmium"]),
    "arsenic": ("heavy_metals", ["Arsenic", "Total Arsenic", "Inorganic Arsenic"]),
}
CONTAMINANTS = ["pcbs", "dioxins_furans", "dioxin_like_pcbs", "mercury", "lead", "cadmium", "arsenic"]

# Longest synonym first, so "Total Omega-3" wins over "Total" and
# "Dioxin-Like PCBs" over "PCBs".
_LABELS = sorted(((syn, analyte) for analyte, (_, syns) in ANALYTES.items() for syn in syns), key=lambda p: -len(p[0]))
_LABEL_RE = re.compile(
    r"^\s*(?P<label>" + "|".join(re.escape(s).replace(r"\ ", r"\s+") for s, _ in _LABELS) + r")(?![A-Za-z0-9])\s*:?\s*(?P<rest>.*)$",
    re.I,
)
_LABEL_LOOKUP = {re.sub(r"\s+", " ", s).lower(): a for s, a in _LABELS}


def match_label(line: str) -> tuple[str, str, str] | None:
    """If the line starts with a known row label, return (analyte, label_raw, rest)."""
    m = _LABEL_RE.match(line)
    if not m:
        return None
    label = re.sub(r"\s+", " ", m.group("label"))
    return _LABEL_LOOKUP[label.lower()], label, m.group("rest").strip()


# --- oil form -----------------------------------------------------------------
# Order matters only for reporting; classification looks at which families hit.
FORM_TERMS: dict[str, list[str]] = {
    "rTG": ["rTG", "r-TG", "re-TG", "reTG", "Re-esterified Triglyceride", "Re-esterified Triglycerides",
            "Reesterified Triglyceride", "Reesterified Triglycerides", "Re-esterified", "Reesterified"],
    "TG": ["Triglyceride", "Triglycerides", "Triglyceride Form", "TG", "TG Form", "Natural Triglyceride",
           "nTG", "Concentrated Triglyceride", "Triacylglycerol", "Triacylglycerols", "TAG"],
    "EE": ["Ethyl Ester", "Ethyl Esters", "EE", "Ethyl Ester Form"],
    "PL": ["Phospholipid", "Phospholipids", "PL", "Krill", "Krill Oil"],
    "MAG": ["Monoglyceride", "Monoglycerides", "Monoacylglycerol", "Monoacylglycerols", "MAG"],
}
TG_FAMILY = {"TG", "rTG"}


def _term_re(term: str) -> re.Pattern:
    body = re.escape(term).replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]?")
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.I)


_FORM_RES = {form: [(t, _term_re(t)) for t in sorted(terms, key=len, reverse=True)] for form, terms in FORM_TERMS.items()}
# Phrases where "triglyceride(s)" is about blood lipids, not the oil form.
_NOT_FORM = re.compile(
    r"(serum|blood|plasma|lower(?:s|ing)?|reduc\w*|healthy|normal|elevated)\s+(?:\w+\s+)?"
    r"(triglycerides?|triacylglycerols?)(?:\s*/\s*(?:triglycerides?|triacylglycerols?))?(?!\s+form)"
    r"|(triglycerides?|triacylglycerols?)\s+levels?",
    re.I,
)


def find_form_terms(text: str) -> dict[str, list[str]]:
    """Return {form: [matched phrases]} for every form family mentioned.

    Blood-lipid phrases ("reduce serum triglycerides", "triglyceride levels")
    are blanked first: they describe a health claim, not the oil.
    """
    cleaned = _NOT_FORM.sub(lambda m: " " * len(m.group(0)), text or "")
    found: dict[str, list[str]] = {}
    for form, patterns in _FORM_RES.items():
        for _, pattern in patterns:
            for m in pattern.finditer(cleaned):
                found.setdefault(form, [])
                phrase = m.group(0)
                if phrase not in found[form]:
                    found[form].append(phrase)
    # "re-esterified triglyceride" also matches the plain TG term; rTG is the
    # more specific reading, so drop the TG hits that sit inside an rTG hit.
    if "rTG" in found and "TG" in found:
        rtg_spans = [m.span() for _, p in _FORM_RES["rTG"] for m in p.finditer(cleaned)]
        keep = []
        for _, p in _FORM_RES["TG"]:
            for m in p.finditer(cleaned):
                # adjacent counts too: "re-esterified triglyceride", "rTG"
                if not any(s - 1 <= m.start() <= e + 1 or s <= m.end() <= e for s, e in rtg_spans):
                    keep.append(m.group(0))
        if keep:
            found["TG"] = list(dict.fromkeys(keep))
        else:
            del found["TG"]
    return found


def classify_form(text: str) -> tuple[str | None, str]:
    """Classify oil form from free text. Returns (form or None, evidence/reason).

    - only TG-family terms            -> 'rTG' if any rTG term, else 'TG'
    - only EE / only PL / only MAG    -> that form
    - nothing                         -> None ("no form stated": unverified, never assumed TG)
    - conflicting families            -> None (reason lists the conflict)
    """
    found = find_form_terms(text)
    if not found:
        return None, "no form stated"
    families = set(found)
    evidence = "; ".join(f"{form}: {', '.join(phrases)}" for form, phrases in found.items())
    if families <= TG_FAMILY:
        return ("rTG" if "rTG" in families else "TG"), evidence
    if len(families) == 1:
        return next(iter(families)), evidence
    return None, f"conflicting form terms ({evidence})"
