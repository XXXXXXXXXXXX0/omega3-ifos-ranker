"""Deterministic parser for IFOS report text layers.

Templates (DECISIONS F-5):
  T1       category table, 2025 onward
  T1-2024  same tables, "Category 1:" to "Category 4:" wording
  RM       Raw Materials Certificate: no batch results
  unknown  anything else -> LLM fallback

The parser works on the plain text of the PDF. It never evaluates or follows
anything it reads; unknown content is logged, not interpreted.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .terms import ANALYTES, classify_form, match_label
from .units import Basis, find_quantities, parse_basis

COMPLIANCE_RE = re.compile(r"\s*\b(yes|no|pass|passed|fail|failed|n/?a)\s*$", re.I)
# Result cells that are legitimately non-numeric.
NON_NUMERIC_RE = re.compile(r"^(nd|n\.d\.|not\s+detected|n/?a|not\s+applicable|not\s+tested|nt|blq|bdl|<\s*loq|<\s*lod|-+|—|tbd|pending)$", re.I)
HEADER_WORDS_RE = re.compile(
    r"^(?:(?:category\s*\d\s*:?)|specification|batch\s+results?|ifos|compliance|potency|purity,?|safety|&|and|cleanliness|"
    r"stability|heavy\s+metals|comments?|\s)+$", re.I)
BASIS_LINE_RE = re.compile(r"((?:mg|milligrams?)\s*(?:/|per)\s*.+?)\s+((?:mg|milligrams?)\s*(?:/|per)\s*.+?)(?:\s+compliance)?\s*$", re.I)
FOOTER_RE = re.compile(r"IFOS\S*\s+Program is provided|Research Lane|^\s*/\s*$|certifications\.nutrasource\.ca|omega3-certification@|^\s*\d{3}-\d{3}-\d{4}", re.I)
LOT_RE = re.compile(r"Batch\s*/\s*Lot\s*#\s*:\s*(.+?)\s*$", re.I | re.M)
EXPIRY_RE = re.compile(r"Expiry\s+Date\s*:\s*(.+?)\s*$", re.I | re.M)
DIRECTION_RE = re.compile(r"Directions?\s*:\s*(.*)$", re.I)
AREA_PCT_RE = re.compile(
    r"(?P<analyte>EPA|DHA|Total\s+Omega[\s-]?3)\s*(?:area\s*%\s*)?specification\s*:\s*(?:(?P<smg>[\d.,]+)\s*mg\s*/\s*(?P<sbasis>[^()]+?)\s*\(\s*)?(?P<spct>[\d.]+)\s*%\)?"
    r"\s*[.,;]?\s*Result\s*:\s*(?:(?P<rmg>[\d.,]+)\s*mg\s*/\s*(?P<rbasis>[^()]+?)\s*\(\s*)?(?P<rpct>[\d.]+)\s*%\)?", re.I)
FORM_COMMENT_RE = re.compile(r"\b(?:EFA|EPA|DHA|omega[\s-]?3s?|fatty acids?)\b[^.\n]*?\btested as\b([^.\n]*)", re.I)


@dataclass
class Row:
    category: str
    analyte: str
    label_raw: str
    spec_text: str | None = None
    spec_op: str | None = None
    spec_value: float | None = None
    spec_unit: str | None = None
    result_text: str | None = None
    result_op: str | None = None
    result_value: float | None = None
    result_unit: str | None = None
    compliance: str | None = None


@dataclass
class Parsed:
    template: str
    status: str = "ok"                 # ok | no_results | failed
    error: str | None = None
    lot: str | None = None
    expiry_text: str | None = None
    directions: str | None = None
    basis: Basis | None = None
    rows: dict[str, Row] = field(default_factory=dict)
    comments: list[str] = field(default_factory=list)
    unrecognized: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    report_form: str | None = None         # oil form stated in the report's comments
    report_form_evidence: str | None = None
    area_pct: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        data = asdict(self)
        data["basis"] = asdict(self.basis) if self.basis else None
        return data


def detect_template(text: str) -> str:
    if re.search(r"Raw\s+Material", text, re.I) and re.search(r"Certified\s+SKUs", text, re.I):
        return "RM"
    has_tables = re.search(r"Specification\s+Batch\s+Results", text, re.I) and re.search(r"\bPotency\b", text, re.I)
    if has_tables and re.search(r"Category\s*1\s*:", text, re.I):
        return "T1-2024"
    if has_tables and re.search(r"\bStability\b", text, re.I):
        return "T1"
    return "unknown"


def _compliance(token: str | None) -> str | None:
    if not token:
        return None
    token = token.lower().replace("/", "")
    return {"yes": "yes", "pass": "yes", "passed": "yes", "no": "no", "fail": "no", "failed": "no"}.get(token)


def parse_row(analyte: str, label: str, rest: str, potency: bool) -> tuple[Row | None, str | None]:
    """Parse the cells after a row label. Returns (row, error)."""
    category = ANALYTES[analyte][0]
    row = Row(category=category, analyte=analyte, label_raw=label)
    m = COMPLIANCE_RE.search(rest)
    if m:
        row.compliance = _compliance(m.group(1))
        rest = rest[: m.start()]
    rest = rest.strip()
    if not rest:
        return None, "no values on row"
    quantities = find_quantities(rest)

    def set_spec(q):
        row.spec_text, row.spec_op, row.spec_value, row.spec_unit = q.text, q.op, q.value, q.unit or None

    def set_result(q):
        row.result_text, row.result_op, row.result_value, row.result_unit = q.text, q.op, q.value, q.unit or None

    if len(quantities) == 2:
        set_spec(quantities[0]); set_result(quantities[1])
    elif len(quantities) == 1:
        q = quantities[0]
        leftover = rest.replace(q.text, "", 1) if q.text in rest else ""
        before, _, after = rest.partition(re.search(r"\d", rest).group(0))  # position of the number
        tail = re.sub(r"\([^()]*\)", " ", rest)[len(before):]
        trailing_words = re.sub(r"^[\d.,\s]*" + re.escape(q.unit) if q.unit else r"^[\d.,\s]*", "", tail).strip()
        if before.strip() and not re.fullmatch(r"(<=|=<|>=|≤|≥|<|>)\s*", before.strip() + " "):
            # words then a number: "N/A 12.3" -> spec is non-numeric, number is the result
            row.spec_text = before.strip()
            set_result(q)
        elif NON_NUMERIC_RE.match(trailing_words or ""):
            # a number then words: "<= 5 meq/kg ND" -> number is the spec
            set_spec(q)
            row.result_text = trailing_words
        elif potency or q.op in (None, "<"):
            set_result(q)          # a lone plain value is the batch result
        else:
            set_spec(q)            # a lone "<= x" is a limit with no result
            row.result_text = None
    elif not quantities:
        if NON_NUMERIC_RE.match(rest):
            row.result_text = rest
        else:
            return None, f"unreadable cells: {rest!r}"
    else:
        return None, f"expected 2 values, found {len(quantities)}: {rest!r}"
    return row, None


def _page_split(text: str) -> tuple[str, str]:
    """(header text, table text). The tables start at the first 'Specification' heading."""
    m = re.search(r"^.*Specification\s+Batch\s+Results.*$", text, re.I | re.M)
    if not m:
        return text, ""
    return text[: m.start()], text[m.start():]


def _parse_header(header: str, out: Parsed) -> None:
    m = LOT_RE.search(header)
    out.lot = m.group(1).strip() if m else None
    m = EXPIRY_RE.search(header)
    out.expiry_text = m.group(1).strip() if m else None
    lines = header.replace("\f", "\n").split("\n")
    for i, line in enumerate(lines):
        m = DIRECTION_RE.search(line)
        if m:
            parts = [m.group(1).strip()]
            for nxt in lines[i + 1:]:
                if re.search(r"certifications@|Certifications By|^Nutrasource$|consistent with label|^Passes\b|Batch\s*/\s*Lot|Expiry\s+Date", nxt, re.I):
                    break
                parts.append(nxt.strip())
            out.directions = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip() or None
            break


def _parse_comments(lines: list[str], out: Parsed) -> None:
    out.comments = [l for l in lines if l.strip()]
    blob = " ".join(out.comments)
    for m in AREA_PCT_RE.finditer(blob):
        key = re.sub(r"\s+", " ", m.group("analyte")).lower().replace("total omega 3", "total_omega3").replace("total omega-3", "total_omega3").replace("total omega3", "total_omega3")
        num = lambda v: float(v.replace(",", "")) if v else None
        out.area_pct[key] = {"spec_mg": num(m.group("smg")), "spec_pct": num(m.group("spct")),
                             "result_mg": num(m.group("rmg")), "result_pct": num(m.group("rpct")),
                             "basis": (m.group("rbasis") or m.group("sbasis") or "").strip() or None}
    m = FORM_COMMENT_RE.search(blob)
    if m:
        form, evidence = classify_form(m.group(1))
        if form:
            out.report_form, out.report_form_evidence = form, re.sub(r"\s+", " ", m.group(0)).strip()
        else:
            out.warnings.append(f"form comment not classified: {m.group(0).strip()!r} ({evidence})")


def parse_report(text: str) -> Parsed:
    template = detect_template(text)
    out = Parsed(template=template)
    if template == "RM":
        out.status = "no_results"
        m = EXPIRY_RE.search(text)
        out.expiry_text = m.group(1).strip() if m else None
        return out
    if template == "unknown":
        out.status, out.error = "failed", "unrecognized report layout"
        return out

    header, tables = _page_split(text)
    _parse_header(header, out)
    errors: list[str] = []
    in_comments = False
    comment_lines: list[str] = []
    for raw_line in tables.replace("\f", "\n").split("\n"):
        line = raw_line.strip()
        if not line or FOOTER_RE.search(line):
            continue
        if in_comments:
            comment_lines.append(line)
            continue
        # Unit line: "mg / 2 softgels mg / 2 softgels [Compliance]", possibly prefixed by "Potency".
        stripped = re.sub(r"^\s*Potency\s+", "", line, flags=re.I)
        bm = BASIS_LINE_RE.fullmatch(stripped)
        if bm and out.basis is None and "epa" not in out.rows:
            spec_basis, result_basis = parse_basis(bm.group(1)), parse_basis(bm.group(2))
            if result_basis is None:
                errors.append(f"unreadable potency unit: {line!r}")
            else:
                out.basis = result_basis
                if spec_basis is None or (spec_basis.kind, spec_basis.count) != (result_basis.kind, result_basis.count):
                    out.warnings.append(f"specification and result units differ: {line!r}")
            continue
        if re.fullmatch(r"comments?\s*:?", line, re.I):
            in_comments = True
            continue
        if HEADER_WORDS_RE.fullmatch(line):
            continue
        hit = match_label(line)
        if hit:
            analyte, label, rest = hit
            row, err = parse_row(analyte, label, rest, potency=ANALYTES[analyte][0] == "potency")
            if err:
                errors.append(f"{label}: {err}")
            elif analyte in out.rows:
                out.warnings.append(f"duplicate row for {analyte}: {line!r}")
            else:
                out.rows[analyte] = row
            continue
        # Not a header, not a known label. If it looks like a data row, log it.
        out.unrecognized.append(line)
    _parse_comments(comment_lines, out)

    potency_rows = [a for a in ("epa", "dha", "total_omega3") if a in out.rows]
    stability_rows = [a for a in ("pv", "av", "totox", "acid_value") if a in out.rows]
    if out.basis is None:
        errors.append("potency unit line not found")
    if not potency_rows:
        errors.append("no potency rows found")
    if not stability_rows:
        errors.append("no stability rows found")
    # A line that ends in Yes/No but has an unknown label is a table row we failed to read.
    lost_rows = [l for l in out.unrecognized if COMPLIANCE_RE.search(l) and re.search(r"\d", l)]
    if lost_rows:
        out.warnings.append(f"{len(lost_rows)} table row(s) with unrecognized labels")
    if errors:
        out.status, out.error = "failed", "; ".join(errors)
    return out
