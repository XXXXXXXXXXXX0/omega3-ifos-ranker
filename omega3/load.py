"""Read ranking inputs from SQLite and write ranking results back.

This is the only I/O around the ranker: rank.py itself never touches the DB.
"""
from __future__ import annotations

import datetime as dt
import json

from .extract.terms import CONTAMINANTS
from .rank import Batch, Contaminant, LabelFacts, Placement, Product, RankResult


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _json(value: str | None, default):
    return json.loads(value) if value else default


def load_products(conn) -> list[Product]:
    """Every listed product with its listed batches, extractions and label facts."""
    labels = {}
    for r in conn.execute("SELECT * FROM label_facts"):
        labels[r["product_num"]] = LabelFacts(
            status=r["status"], form=r["form"], form_source_url=r["form_source_url"], form_evidence=r["form_evidence"],
            oil_mg_per_capsule=r["oil_mg_per_capsule"], oil_source_url=r["oil_source_url"], oil_evidence=r["oil_evidence"],
            serving_capsules=r["serving_capsules"], servings_per_bottle=r["servings_per_bottle"],
            flavored=None if r["flavored"] is None else bool(r["flavored"]), flavor_evidence=r["flavor_evidence"],
            pet=None if r["pet"] is None else bool(r["pet"]), pet_evidence=r["pet_evidence"],
            antioxidants=_json(r["antioxidants"], []), packaging=_json(r["packaging"], []), reasoning=r["reasoning"])

    contaminants: dict[str, list[Contaminant]] = {}
    marks = ",".join("?" for _ in CONTAMINANTS)
    for r in conn.execute(f"SELECT * FROM measurements WHERE analyte IN ({marks})", CONTAMINANTS):
        contaminants.setdefault(r["filename"], []).append(Contaminant(
            analyte=r["analyte"], label=r["label_raw"], result_text=r["result_text"], result_value=r["result_value"],
            limit_text=r["spec_text"], limit_value=r["spec_value"], limit_op=r["spec_op"], compliance=r["compliance"],
            result_op=r["result_op"], result_unit=r["result_unit"], limit_unit=r["spec_unit"]))

    batches: dict[str, list[Batch]] = {}
    rows = conn.execute(
        """
        SELECT b.product_num, b.lot, b.expiry_date, b.report_filename, r.report_date, e.*
        FROM batches b
        LEFT JOIN reports r ON r.filename = b.report_filename AND r.status = 'ok'
        LEFT JOIN extractions e ON e.filename = b.report_filename AND e.sha256 = r.sha256
        WHERE b.listed = 1
        ORDER BY b.product_num, b.lot
        """)
    for r in rows:
        censored = [name for name in ("pv", "av", "totox", "acid_value") if r[f"{name}_censored"]]
        batches.setdefault(r["product_num"], []).append(Batch(
            lot=r["lot"], expiry_date=_date(r["expiry_date"]), report_filename=r["report_filename"],
            report_date=_date(r["report_date"]), status=r["status"], method=r["method"],
            basis_text=r["basis_text"], basis_kind=r["basis_kind"], basis_count=r["basis_count"], basis_expressed_as=r["basis_expressed_as"],
            epa_mg_cap=r["epa_mg_cap"], dha_mg_cap=r["dha_mg_cap"], total_omega3_mg_cap=r["total_omega3_mg_cap"],
            epa_mg_g=r["epa_mg_g"], dha_mg_g=r["dha_mg_g"], total_omega3_mg_g=r["total_omega3_mg_g"],
            epa_dha_mg_cap=r["epa_dha_mg_cap"], epa_dha_mg_g=r["epa_dha_mg_g"],
            pv=r["pv"], av=r["av"], totox=r["totox"], totox_source=r["totox_source"], acid_value=r["acid_value"],
            censored=censored, contaminants=contaminants.get(r["report_filename"], []),
            report_form=r["report_form"], report_form_evidence=r["report_form_evidence"],
            area_pct=_json(r["area_pct"], {}), directions=r["directions"], warnings=_json(r["warnings"], [])))

    products = []
    for r in conn.execute("SELECT * FROM products WHERE listed = 1 ORDER BY product_num"):
        products.append(Product(
            product_num=r["product_num"], name=r["name"], brand=r["brand"] or r["brand_id"], summary=r["summary"] or "",
            product_type=r["product_type"], rda=r["rda"], website=r["website"],
            batches=batches.get(r["product_num"], []), label=labels.get(r["product_num"])))
    return products


def placement_row(p: Placement) -> dict:
    """Flat, JSON-safe view of a placement: what the report and changelog read."""
    b = p.batch
    return {
        "product_num": p.product.product_num, "brand": p.product.brand, "product": p.product.name,
        "product_type": p.product.product_type, "website": p.product.website,
        "list": p.list_name, "reason": p.reason, "all_reasons": p.all_reasons,
        "rank": p.rank, "tie_group": p.tie_group, "score": p.score, "optimistic_score": p.optimistic_score,
        "lot": b.lot if b else None, "report_filename": b.report_filename if b else None,
        "report_date": b.report_date.isoformat() if b and b.report_date else None,
        "expiry_date": b.expiry_date.isoformat() if b and b.expiry_date else None,
        "totox": b.totox if b else None, "totox_source": b.totox_source if b else None,
        "pv": b.pv if b else None, "av": b.av if b else None, "acid_value": b.acid_value if b else None,
        "censored": b.censored if b else [],
        "epa": (b.epa_mg_cap if b.epa_mg_cap is not None else b.epa_mg_g) if b else None,
        "dha": (b.dha_mg_cap if b.dha_mg_cap is not None else b.dha_mg_g) if b else None,
        "basis_text": b.basis_text if b else None,
        "epa_dha_mg_cap": p.epa_dha_mg_cap, "epa_dha_mg_serving": p.epa_dha_mg_serving, "epa_dha_mg_g": p.epa_dha_mg_g,
        "serving_capsules": p.serving_capsules, "epa_dha_ratio": p.epa_dha_ratio, "potency_label": p.potency_label,
        "oil_mg_per_capsule": p.oil_mg_per_capsule, "oil_source": p.oil_source, "oil_g_per_target": p.oil_g_per_target,
        "form": p.form, "form_source": p.form_source, "form_evidence": p.form_evidence,
        "preferred": p.preferred, "flavored": p.flavored, "inconsistent": p.inconsistent,
        "totox_range": list(p.totox_range) if p.totox_range else None,
        "unexpired_batches": p.unexpired_batches, "expired_dropped": p.expired_dropped, "flags": p.flags,
    }


def save_results(conn, run_id: str, result: RankResult, stats: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO runs (run_id, as_of_date, stats) VALUES (?, ?, ?)",
                 (run_id, result.as_of.isoformat(), json.dumps(stats, default=str)))
    conn.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
    for p in result.placements:
        row = placement_row(p)
        conn.execute(
            "INSERT INTO results (run_id, product_num, lot, list_name, rank, score, reason, detail) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, row["product_num"], row["lot"], p.list_name, p.rank, p.score, p.reason, json.dumps(row, ensure_ascii=False)))
    conn.commit()
