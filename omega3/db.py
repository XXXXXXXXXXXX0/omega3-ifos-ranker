"""SQLite schema and small helpers. One file DB, committed to the repo."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_num     TEXT PRIMARY KEY,          -- Nutrasource's own stable ID
    brand_id        TEXT,
    brand           TEXT,
    name            TEXT NOT NULL,
    summary         TEXT,
    product_type    TEXT,                      -- IFOS "Product Type": format, NOT oil form
    rda             TEXT,
    website         TEXT,
    page_sha256     TEXT,
    listed          INTEGER NOT NULL DEFAULT 1,
    format_decision TEXT,                      -- include | exclude
    format_reason   TEXT,
    chewable        INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT NOT NULL,
    last_checked    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand_id);

CREATE TABLE IF NOT EXISTS batches (
    product_num     TEXT NOT NULL REFERENCES products(product_num),
    lot             TEXT NOT NULL,
    expiry_date     TEXT,                      -- ISO date from the listing
    expiry_text     TEXT,
    report_filename TEXT,
    listed          INTEGER NOT NULL DEFAULT 1,  -- 0 once it leaves the listing
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    PRIMARY KEY (product_num, lot)
);
CREATE INDEX IF NOT EXISTS idx_batches_lot ON batches(lot);
CREATE INDEX IF NOT EXISTS idx_batches_report ON batches(report_filename);

-- Raw file manifest: one row per report file ever seen.
CREATE TABLE IF NOT EXISTS reports (
    filename        TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    status          TEXT NOT NULL,             -- ok | error
    error           TEXT,
    sha256          TEXT,
    bytes           INTEGER,
    etag            TEXT,
    last_modified   TEXT,
    pdf_created     TEXT,                      -- ISO date from PDF metadata
    pdf_producer    TEXT,
    pages           INTEGER,
    report_date     TEXT,                      -- stand-in for test date (P-1)
    report_date_source TEXT,
    first_seen      TEXT NOT NULL,
    last_checked    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_sha ON reports(sha256);

-- One row per successfully classified report.
CREATE TABLE IF NOT EXISTS extractions (
    filename        TEXT PRIMARY KEY REFERENCES reports(filename),
    sha256          TEXT NOT NULL,
    template        TEXT NOT NULL,             -- T1 | T1-2024 | RM | unknown
    method          TEXT NOT NULL,             -- parser | llm
    model           TEXT,                      -- for method=llm
    status          TEXT NOT NULL,             -- ok | no_results | failed
    error           TEXT,
    brand_on_report TEXT,
    product_on_report TEXT,
    lot_on_report   TEXT,
    expiry_on_report TEXT,
    directions      TEXT,
    basis_text      TEXT,                      -- e.g. "mg / 2 softgels"
    basis_kind      TEXT,                      -- capsule | gram | ml | other
    basis_count     REAL,                      -- 2 for "mg / 2 softgels"
    epa_orig        REAL, dha_orig REAL, total_omega3_orig REAL, epa_dha_orig REAL,
    epa_spec        REAL, dha_spec REAL, total_omega3_spec REAL, epa_dha_spec REAL,
    epa_mg_cap      REAL, dha_mg_cap REAL, total_omega3_mg_cap REAL, epa_dha_mg_cap REAL,
    epa_mg_g        REAL, dha_mg_g REAL, total_omega3_mg_g REAL, epa_dha_mg_g REAL,
    conversion      TEXT,                      -- human-readable conversion applied
    pv REAL, pv_censored INTEGER,
    av REAL, av_censored INTEGER,
    totox REAL, totox_censored INTEGER,
    totox_source    TEXT,                      -- reported | computed
    acid_value REAL, acid_value_censored INTEGER,
    comments        TEXT,                      -- JSON list: the report's Comments block
    report_form     TEXT,                      -- oil form stated in the comments, if any
    report_form_evidence TEXT,
    area_pct        TEXT,                      -- JSON: area-% figures from the comments
    warnings        TEXT,                      -- JSON list
    extracted_at    TEXT NOT NULL
);

-- Every table row from every report, original text preserved.
CREATE TABLE IF NOT EXISTS measurements (
    filename        TEXT NOT NULL REFERENCES reports(filename),
    category        TEXT NOT NULL,             -- potency | purity | stability | heavy_metals
    analyte         TEXT NOT NULL,             -- canonical name
    label_raw       TEXT NOT NULL,
    spec_text       TEXT, spec_op TEXT, spec_value REAL, spec_unit TEXT,
    result_text     TEXT, result_op TEXT, result_value REAL, result_unit TEXT,
    compliance      TEXT,                      -- yes | no | NULL
    PRIMARY KEY (filename, analyte)
);

CREATE TABLE IF NOT EXISTS unrecognized_labels (
    filename        TEXT NOT NULL,
    line            TEXT NOT NULL,
    PRIMARY KEY (filename, line)
);

-- Label facts found by the verification agent (form, oil weight, flags).
CREATE TABLE IF NOT EXISTS label_facts (
    product_num     TEXT PRIMARY KEY REFERENCES products(product_num),
    status          TEXT NOT NULL,             -- verified | unverified
    form            TEXT,                      -- TG | rTG | EE | PL | MAG | NULL
    form_source_url TEXT,
    form_evidence   TEXT,
    oil_mg_per_capsule REAL,
    oil_source_url  TEXT,
    oil_evidence    TEXT,
    serving_capsules REAL,
    servings_per_bottle REAL,
    flavored        INTEGER,                   -- 1 | 0 | NULL unknown
    flavor_evidence TEXT,
    antioxidants    TEXT,                      -- JSON list, display only
    packaging       TEXT,                      -- JSON list, display only
    sources_checked TEXT,                      -- JSON list of URLs
    page_hashes     TEXT,                      -- JSON {url: sha256}
    reasoning       TEXT,
    model           TEXT,
    tool_calls      INTEGER,
    cost_usd        REAL,
    verified_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

-- Batch list as it stood at each run: lets the changelog name new and expired lots.
CREATE TABLE IF NOT EXISTS run_batches (
    run_id          TEXT NOT NULL,
    product_num     TEXT NOT NULL,
    lot             TEXT NOT NULL,
    expiry_date     TEXT,
    PRIMARY KEY (run_id, product_num, lot)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,          -- ISO timestamp
    as_of_date      TEXT NOT NULL,
    stats           TEXT                       -- JSON
);

-- Final placement of every product in a run (ranked or one of the lists).
CREATE TABLE IF NOT EXISTS results (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    product_num     TEXT NOT NULL,
    lot             TEXT,
    list_name       TEXT NOT NULL,             -- ranked | flavored | form_unverified | ...
    rank            INTEGER,
    score           REAL,
    reason          TEXT,
    detail          TEXT,                      -- JSON row for the report
    PRIMARY KEY (run_id, product_num)
);
CREATE INDEX IF NOT EXISTS idx_results_list ON results(run_id, list_name);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema (additive changes only)."""
    for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA, re.S):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for line in body.split("\n"):
            line = line.split("--")[0].strip().rstrip(",")
            if not line or line.upper().startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK")):
                continue
            for part in line.split(","):          # "epa REAL, dha REAL" style lines
                tokens = part.split()
                if len(tokens) >= 2 and tokens[0] not in have and tokens[0].isidentifier():
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {tokens[0]} {' '.join(t for t in tokens[1:] if t.upper() not in ('NOT', 'NULL', 'PRIMARY', 'KEY'))}")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def upsert(conn: sqlite3.Connection, table: str, row: dict, keys: tuple[str, ...], keep: tuple[str, ...] = ("first_seen",)) -> None:
    """Insert or update by primary key. Columns in `keep` are never overwritten."""
    cols = list(row)
    updates = [c for c in cols if c not in keys and c not in keep]
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in updates)
    )
    conn.execute(sql, [json.dumps(v) if isinstance(v, (list, dict)) else v for v in row.values()])


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
