"""Deterministic parser for IFOS reports.

Input is a PdfDoc: page text plus the table cells pdfplumber recovered from
the PDF's ruled tables. Rows are read cell by cell, so wrapped cells (a result
that breaks over two lines) cannot be mis-assigned the way they can when
parsing flat text.

Templates (DECISIONS F-5):
  T1       category table, 2025 onward
  T1-2024  same tables, "Category 1:" to "Category 4:" wording
  RM       Raw Materials Certificate: no batch results
  unknown  anything else -> LLM fallback

Nothing read from a report is ever evaluated or followed as an instruction;
unknown content is logged, not interpreted.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .pdftext import PdfDoc

# Bump when parsing or derivation changes, so stored extractions are redone on the next run.
PARSER_VERSION = 5
from .terms import ANALYTES, classify_form, match_label
from .units import Basis, find_quantities, parse_basis

COMPLIANCE_RE = re.compile(r"^(yes|no|pass|passed|fail|failed|n/?a|-+)\.?\s*[*†‡]*$", re.I)   # "YES*": footnote mark
# Result cells that are legitimately non-numeric.
NON_NUMERIC_RE = re.compile(
    r"^(nd|n\.d\.|not\s+detected|none\s+detected|n/?a|not\s+applicable|not\s+tested|nt|blq|bdl|<\s*loq|<\s*lod|-+|—|tbd|pending)\.?$", re.I)
BASIS_CELL_RE = re.compile(
    r"^(?:(?:specification|batch\s+results?)\s+)?"                       # "Specification mg per cap" (merged header cell)
    r"(?:(?:mg|milligrams?)\s*(?:/|per\b|\s(?=\d)).*|g\s*(?:/|per\b)\s*\d+\s*g|(?:area\s*)?%|area\s+percentage\s*\(?%?\)?)$", re.I)
_BASIS_PREFIX = re.compile(r"^(?:specification|batch\s+results?)\s+", re.I)
LOT_RE = re.compile(r"Batch\s*/\s*Lot\s*#\s*:\s*(.+?)\s*$", re.I | re.M)
EXPIRY_RE = re.compile(r"Expiry\s+Date\s*:\s*(.+?)\s*$", re.I | re.M)
DIRECTION_RE = re.compile(r"Directions?\s*:\s*(.*)$", re.I)
DIRECTION_STOP_RE = re.compile(
    r"certifications@|Certifications By|^Nutrasource$|consistent with label|^Passes\b|Batch\s*/\s*Lot|Expiry\s+Date", re.I)
AREA_PCT_RE = re.compile(
    r"(?P<analyte>EPA|DHA|Total\s+Omega[\s-]?3)\s*(?:area\s*%\s*)?specification\s*:\s*"
    r"(?:(?P<smg>[\d.,]+)\s*mg\s*/\s*(?P<sbasis>[^()]+?)\s*\(\s*)?(?P<spct>\d+(?:\.\d+)?)\s*%\)?"
    r"\s*[.,;]?\s*Result\s*:\s*(?:(?P<rmg>[\d.,]+)\s*mg\s*/\s*(?P<rbasis>[^()]+?)\s*\(\s*)?(?P<rpct>\d+(?:\.\d+)?)\s*%\)?", re.I)
FORM_COMMENT_RE = re.compile(r"\b(?:EFAs?|EPA|DHA|omega[\s-]?3s?|fatty acids?)\b[^.\n]*?\btested as\b([^.\n]*)", re.I)


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
    misprints: list[str] = field(default_factory=list)


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
    report_form: str | None = None           # oil form stated in the report's comments
    report_form_evidence: str | None = None
    area_pct: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        data = asdict(self)
        data["basis"] = asdict(self.basis) if self.basis else None
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Parsed":
        data = dict(data)
        basis = data.pop("basis", None)
        rows = data.pop("rows", {}) or {}
        out = cls(**data)
        out.basis = Basis(**basis) if basis else None
        out.rows = {k: Row(**v) for k, v in rows.items()}
        return out


def detect_template(text: str) -> str:
    if re.search(r"Raw\s+Material", text, re.I) and re.search(r"Certified\s+SKUs", text, re.I):
        return "RM"
    has_tables = re.search(r"Specification\s+Batch\s+Results", text, re.I) and re.search(r"\bPotency\b", text, re.I)
    if has_tables and re.search(r"Category\s*1\s*:", text, re.I):
        return "T1-2024"
    if has_tables and re.search(r"\bStability\b", text, re.I):
        return "T1"
    return "unknown"


def _clean(cell) -> str:
    return re.sub(r"\s+", " ", cell or "").strip()


def _compliance(token: str | None) -> str | None:
    return {"yes": "yes", "pass": "yes", "passed": "yes", "no": "no", "fail": "no", "failed": "no"}.get(
        re.sub(r"[\s.*†‡]+$", "", (token or "").lower()))


def _fill(row: Row, role: str, cell: str) -> str | None:
    """Put one cell into the row. Returns an error string if it cannot be read."""
    if role == "compliance":
        if not COMPLIANCE_RE.match(cell):
            return f"unreadable compliance cell {cell!r}"
        row.compliance = _compliance(cell)
        return None
    try:
        quantities = find_quantities(cell)
    except ValueError as exc:
        # A misprinted number is kept as text with no value. The row survives;
        # whatever depends on the value treats it as not reported.
        setattr(row, f"{role}_text", cell)
        row.misprints.append(f"{role}: {exc}")
        return None
    if len(quantities) == 1 and not NON_NUMERIC_RE.match(cell):
        q = quantities[0]
        if role == "spec":
            row.spec_text, row.spec_op, row.spec_value, row.spec_unit = cell, q.op, q.value, q.unit or None
        else:
            row.result_text, row.result_op, row.result_value, row.result_unit = cell, q.op, q.value, q.unit or None
        return None
    if not quantities or NON_NUMERIC_RE.match(cell):
        # Words instead of a number ("ND", "N/A"): keep the text, no value.
        if NON_NUMERIC_RE.match(cell) or role == "spec":
            setattr(row, f"{role}_text", cell)
            return None
        return f"unreadable {role} cell {cell!r}"
    return f"{role} cell has {len(quantities)} numbers: {cell!r}"


def _parse_table(table: list[list], out: Parsed, errors: list[str]) -> None:
    rows = [[_clean(c) for c in row] for row in table]
    flat = [c for row in rows for c in row if c]
    if not flat:
        return
    if any(re.fullmatch(r"comments?:?", c, re.I) for c in rows[0] if c):
        for row in table:
            for cell in row:
                for line in (cell or "").split("\n"):
                    line = line.strip()
                    if line and not re.fullmatch(r"comments?:?", line, re.I):
                        out.comments.append(line)
        return

    data_rows: list[tuple[str, str, list[tuple[int, str]]]] = []
    for row in rows:
        cells = [(i, c) for i, c in enumerate(row) if c]
        if not cells:
            continue
        hit = match_label(cells[0][1])
        # The label cell must be the label alone (a trailing footnote mark is fine).
        if hit and (re.fullmatch(r"[\s*†‡:()0-9a-z]{0,4}", hit[2], re.I) or re.fullmatch(r"\([^()]{2,40}\)", hit[2])):
            data_rows.append((hit[0], hit[1], cells[1:]))
            continue
        basis_cells = [_BASIS_PREFIX.sub("", c) for _, c in cells if BASIS_CELL_RE.match(c)]
        if basis_cells and out.basis is None:
            parsed = [parse_basis(c) for c in basis_cells]
            if parsed[-1] is None:
                errors.append(f"unreadable potency unit {basis_cells[-1]!r}")
            else:
                out.basis = parsed[-1]          # the Batch Results column's unit
                if len(parsed) > 1 and (parsed[0] is None or (parsed[0].kind, parsed[0].count) != (parsed[-1].kind, parsed[-1].count)):
                    out.warnings.append(f"specification and result units differ: {basis_cells}")
            continue
        # Anything else is a header row, unless it looks like data: then the
        # label is one we do not know, and it is logged rather than dropped.
        if len(cells) >= 3 and COMPLIANCE_RE.match(cells[-1][1]) and any(re.search(r"\d", c) for _, c in cells[1:]):
            out.unrecognized.append(" | ".join(c for _, c in cells))

    # Column roles come from fully populated rows of this same table.
    roles: dict[int, str] = {}
    for _, _, values in data_rows:
        if len(values) == 3:
            roles.update({values[0][0]: "spec", values[1][0]: "result", values[2][0]: "compliance"})
    for analyte, label, values in data_rows:
        row = Row(category=ANALYTES[analyte][0], analyte=analyte, label_raw=label)
        if len(values) == 3:
            assigned = list(zip(("spec", "result", "compliance"), (v for _, v in values)))
        elif values and all(i in roles for i, _ in values):
            assigned = [(roles[i], v) for i, v in values]        # partial row, columns known
        else:
            errors.append(f"{label}: cannot assign {len(values)} cell(s) to columns: {[v for _, v in values]}")
            continue
        row_errors = [e for e in (_fill(row, role, cell) for role, cell in assigned) if e]
        out.warnings.extend(f"{label}: {m}" for m in row.misprints)
        if (not row_errors and row.category == "potency" and row.result_unit and row.result_unit.startswith("%")
                and out.basis is not None and out.basis.kind != "percent"):
            # "Total Omega-3 | 90% | 93.3%" in a mg-per-softgel table: a share of the oil, not a mass.
            out.area_pct[analyte] = {"spec_mg": None, "spec_pct": row.spec_value if (row.spec_unit or "").startswith("%") else None,
                                     "result_mg": None, "result_pct": row.result_value, "basis": None}
            out.warnings.append(f"{label} reported as % in a '{out.basis.text}' table: kept as area %, not as mg")
            continue
        if row_errors:
            errors.append(f"{label}: " + "; ".join(row_errors))
        elif analyte in out.rows:
            out.warnings.append(f"duplicate row for {label}")
        else:
            out.rows[analyte] = row


def _parse_header(text: str, out: Parsed) -> None:
    m = LOT_RE.search(text)
    out.lot = m.group(1).strip() if m else None
    m = EXPIRY_RE.search(text)
    out.expiry_text = m.group(1).strip() if m else None
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = DIRECTION_RE.search(line)
        if m:
            parts = [m.group(1).strip()]
            for nxt in lines[i + 1:]:
                if DIRECTION_STOP_RE.search(nxt):
                    break
                parts.append(nxt.strip())
            out.directions = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip() or None
            break


def _parse_comments(out: Parsed) -> None:
    blob = " ".join(out.comments)
    for m in AREA_PCT_RE.finditer(blob):
        key = "total_omega3" if m.group("analyte").lower().startswith("total") else m.group("analyte").lower()
        num = lambda v: float(v.replace(",", "")) if v else None  # noqa: E731
        out.area_pct[key] = {"spec_mg": num(m.group("smg")), "spec_pct": num(m.group("spct")),
                             "result_mg": num(m.group("rmg")), "result_pct": num(m.group("rpct")),
                             "basis": _clean(m.group("rbasis") or m.group("sbasis")) or None}
    m = FORM_COMMENT_RE.search(blob)
    if m:
        form, evidence = classify_form(m.group(1))
        if form:
            out.report_form, out.report_form_evidence = form, _clean(m.group(0))
        else:
            out.warnings.append(f"form comment not classified: {_clean(m.group(0))!r} ({evidence})")


def parse_report(doc: PdfDoc) -> Parsed:
    text = doc.text
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

    _parse_header(doc.pages[0] if doc.pages else "", out)
    errors: list[str] = []
    for page_tables in doc.tables:
        for table in page_tables:
            _parse_table(table, out, errors)
    _parse_comments(out)

    if out.basis is None:
        errors.append("potency unit not found")
    if not any(a in out.rows for a in ("epa", "dha", "epa_dha", "total_omega3")):
        if out.unrecognized:
            # The table was read, it just lists other fatty acids (an omega-7
            # product, say). Not a parse failure; the ranker files it as incomplete.
            out.warnings.append("no EPA or DHA row; potency table lists: " + "; ".join(u.split(" | ")[0] for u in out.unrecognized))
        else:
            errors.append("no potency rows found")
    if not any(a in out.rows for a in ("pv", "av", "totox", "acid_value")):
        errors.append("no stability rows found")
    if errors:
        out.status, out.error = "failed", "; ".join(errors)
    return out
