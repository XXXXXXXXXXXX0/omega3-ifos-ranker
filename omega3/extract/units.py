"""Units: quantities like '<= 45 ppb', potency bases like 'mg / 2 softgels',
expiry dates in the formats IFOS reports actually use."""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass

OPS = {"<=": "<=", "≤": "<=", "=<": "<=", "<": "<", ">=": ">=", "≥": ">=", ">": ">"}
_QTY = re.compile(r"(?P<op><=|=<|>=|≤|≥|<|>)?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)\s*(?P<unit>[^\d<>≤≥=]*)")


@dataclass
class Quantity:
    text: str
    op: str | None
    value: float
    unit: str


def find_quantities(text: str) -> list[Quantity]:
    """All 'op number unit' groups in a string, left to right.

    Parenthesised notes such as '(2005)' in 'WHO-TEQ (2005)' are removed first
    so the year is not read as a value.
    """
    cleaned = re.sub(r"\([^()]*\)", " ", text)
    out = []
    for m in _QTY.finditer(cleaned):
        if re.fullmatch(r"0\d+", m.group("num")):
            # "00016" is a misprint (a lost decimal point), not sixteen. Refuse to read it.
            raise ValueError(f"misprinted number '{m.group('num')}'")
        unit = re.sub(r"\s+", " ", m.group("unit")).strip(" ,;")
        out.append(Quantity(text=re.sub(r"\s+", " ", m.group(0)).strip(), op=OPS.get(m.group("op") or ""),
                            value=float(m.group("num").replace(",", "")), unit=unit))
    return out


# --- potency basis ------------------------------------------------------------
CAPSULE_WORDS = r"softgels?|soft\s*gels?|soft-gels?|capsules?|caps?|gelcaps?|gel\s*caps?|sgels?|softgel\s+capsules?|pearls?|minigels?|mini\s*softgels?|liquid\s+softgels?"
_BASIS = re.compile(
    r"^\s*(?P<mass>mg|milligrams?|g|grams?)\s*(?:/|per|\s(?=\d))\s*(?P<count>\d+(?:\.\d+)?)?\s*(?P<unit>.+?)\s*$", re.I)   # "mg 2 softgels" too


@dataclass
class Basis:
    text: str
    kind: str        # capsule | gram | volume | percent | other
    count: float     # e.g. 2 for "mg / 2 softgels"; grams for kind=gram
    expressed_as: str | None = None   # "TG" for "mg per softgel as TG": how the amount is expressed, not proof of form

    def per_unit(self, value: float | None) -> float | None:
        return None if value is None else value / self.count


_EXPRESSED_AS = re.compile(r"\s+as\s+(TG|EE|triglycerides?|ethyl\s+esters?)\s*$", re.I)
# Misspellings and odd forms seen on real reports, mapped to the word meant.
_UNIT_FIXES = [(r"^sofgels?$|^softgrls?$|^sotgels?$", "softgel"), (r"^/\s*", ""),
               (r"^chewable\s+(?:gel|softgel)s?$", "softgel")]


def parse_basis(text: str) -> Basis | None:
    """Parse the potency unit line. Returns None if it is not a 'mg per X' unit."""
    raw = re.sub(r"\s+", " ", text).strip()
    expressed = None
    m = _EXPRESSED_AS.search(raw)
    if m:
        expressed = "EE" if m.group(1).lower().startswith(("ee", "ethyl")) else "TG"
        basis = parse_basis(raw[: m.start()])
        if basis is not None:
            basis.text, basis.expressed_as = raw, expressed
        return basis
    if re.fullmatch(r"(?:area\s*)?%|percent(?:age)?|area\s+percentage\s*\(?%?\)?", raw, re.I):
        return Basis(raw, "percent", 1.0)
    m = _BASIS.match(raw)
    if not m:
        return None
    count = float(m.group("count")) if m.group("count") else 1.0
    unit = m.group("unit").strip().lower()
    for pattern, replacement in _UNIT_FIXES:
        unit = re.sub(pattern, replacement, unit).strip()
    count_in_unit = re.match(r"^(\d+(?:\.\d+)?)\s+(.+)$", unit)      # "mg per / 2 softgels"
    if count_in_unit and not m.group("count"):
        count, unit = float(count_in_unit.group(1)), count_in_unit.group(2)
    if count <= 0:
        return None
    if m.group("mass").lower().startswith("g"):
        # "g / 100 g": values are grams, so one gram of oil holds value * 1000 / 100 mg.
        if not re.fullmatch(r"g|grams?|gm", unit):
            return None
        return Basis(raw, "gram", count / 1000.0)
    if re.fullmatch(CAPSULE_WORDS, unit, re.I):
        return Basis(raw, "capsule", count)
    if re.fullmatch(r"g|grams?|gm", unit):
        return Basis(raw, "gram", count)
    if unit in ("mg",) :                     # "mg per 1000 mg"
        return Basis(raw, "gram", count / 1000.0)
    if re.fullmatch(r"ml|millilit(?:er|re)s?|tsps?|teaspoons?|tbsps?|tablespoons?|drops?|l", unit):
        return Basis(raw, "volume", count)
    return Basis(raw, "other", count)


# --- expiry -------------------------------------------------------------------
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_MONTHS["sept"] = 9


def _eom(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def parse_report_expiry(text: str, hint: dt.date | None = None) -> dt.date | None:
    """Parse a report's expiry text. Month-only dates resolve to the month end.

    `hint` (the listing's expiry) settles DD/MM vs MM/DD when both are possible.
    Returns None rather than guessing when the text cannot be read.
    """
    raw = (text or "").strip()
    order = None
    fmt = re.search(r"\(([DMY/\-. ]+)\)", raw, re.I)
    if fmt:
        letters = re.sub(r"[^DMY]", "", fmt.group(1).upper())
        order = "DMY" if letters.startswith("D") else "MDY" if letters.startswith("M") else "YMD" if letters.startswith("Y") else None
    s = re.sub(r"\([^()]*\)", " ", raw).strip().rstrip(".")
    s = re.sub(r"\s*([/.\-])\s*", r"\1", s)                       # "10 / 2028" -> "10/2028"
    try:
        m = re.fullmatch(r"(\d{1,2})[\s\-/.]+([A-Za-z]{3,9})[\s\-/.,]+(\d{4})", s)          # 30-Apr-2028, 30/NOV/2026
        if m and m.group(2).lower() in _MONTHS:
            return dt.date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        m = re.fullmatch(r"([A-Za-z]{3,9})\.?[\s\-/.]+(\d{1,2}),?[\s\-/.]+(\d{4})", s)       # Apr 30, 2028
        if m and m.group(1).lower() in _MONTHS:
            return dt.date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        m = re.fullmatch(r"([A-Za-z]{3,9})\.?[\s\-/.,]+(\d{4})", s)                          # Apr 2028
        if m and m.group(1).lower() in _MONTHS:
            return _eom(int(m.group(2)), _MONTHS[m.group(1).lower()])
        m = re.fullmatch(r"(\d{4})[\-/.](\d{1,2})[\-/.](\d{1,2})", s)                        # 2028-04-30
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.fullmatch(r"(\d{1,2})[\-/.](\d{4})", s)                                       # 10/2027
        if m:
            return _eom(int(m.group(2)), int(m.group(1)))
        m = re.fullmatch(r"(\d{4})[\-/.](\d{1,2})", s)                                       # 2027-10
        if m:
            return _eom(int(m.group(1)), int(m.group(2)))
        m = re.fullmatch(r"(\d{1,2})[\-/.](\d{1,2})[\-/.](\d{4})", s)                        # 03/12/2026
        if m:
            a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if (order == "DMY" and b > 12 >= a) or (order == "MDY" and a > 12 >= b):
                order = None                       # the stated order is impossible; use the only valid reading
            if order == "MDY" or (order is None and b > 12 >= a):
                return dt.date(year, a, b)
            if order == "DMY" or a > 12 >= b:
                return dt.date(year, b, a)
            if a == b:
                return dt.date(year, a, a)
            # Both readings are valid dates: let the listing's date decide.
            options = [dt.date(year, b, a), dt.date(year, a, b)]
            if hint in options:
                return hint
            return None
    except ValueError:
        return None
    return None


# --- contaminant units ----------------------------------------------------------------
_MASS_FRACTION = {"ppm": 1e-6, "mg/kg": 1e-6, "ug/g": 1e-6, "µg/g": 1e-6,
                  "ppb": 1e-9, "ug/kg": 1e-9, "µg/kg": 1e-9, "ng/g": 1e-9,
                  "ppt": 1e-12, "ng/kg": 1e-12, "pg/g": 1e-12}


def mass_fraction(unit: str | None) -> float | None:
    """Scale factor of a concentration unit ('ppt WHO-TEQ' -> 1e-12). None if unknown."""
    if not unit:
        return None
    u = re.sub(r"who[\s-]*teq|\(.*?\)|\?", "", unit, flags=re.I).strip().lower().replace(" ", "")
    return _MASS_FRACTION.get(u)
