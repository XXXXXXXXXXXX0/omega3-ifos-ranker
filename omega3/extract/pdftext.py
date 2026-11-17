"""Read the text layer, tables and metadata of a report PDF.

PDFs are untrusted input: they are parsed for text only, never executed, and
any parser error is reported to the caller rather than raised past it.

The result is what gets committed to the repo for every report
(data/raw/text/<sha256>.json): it holds everything the extractor reads, so the
raw PDF is not needed again.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

_PDF_DATE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")


@dataclass
class PdfDoc:
    pages: list[str]                                   # text of each page
    tables: list[list[list[list[str | None]]]] = field(default_factory=list)   # page -> table -> row -> cell
    created: dt.date | None = None                     # PDF CreationDate, when present and sane
    producer: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.pages)

    def to_json(self) -> str:
        return json.dumps({"pages": self.pages, "tables": self.tables,
                           "created": self.created.isoformat() if self.created else None,
                           "producer": self.producer}, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "PdfDoc":
        data = json.loads(raw)
        created = dt.date.fromisoformat(data["created"]) if data.get("created") else None
        return cls(pages=data["pages"], tables=data.get("tables") or [], created=created, producer=data.get("producer"))


def parse_pdf_date(raw: str | None) -> dt.date | None:
    match = _PDF_DATE.search(raw or "")
    if not match:
        return None
    try:
        return dt.date(*map(int, match.groups()))
    except ValueError:
        return None


def read_pdf(path: Path | str) -> PdfDoc:
    with pdfplumber.open(str(path)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
        tables = [page.extract_tables() for page in pdf.pages]
        meta = pdf.metadata or {}
    return PdfDoc(
        pages=pages,
        tables=tables,
        created=parse_pdf_date(str(meta.get("CreationDate") or "")),
        producer=(str(meta.get("Creator") or meta.get("Producer") or "") or None),
    )
