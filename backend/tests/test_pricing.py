"""Dealer cost price files, Kubota model/series enrichment, and cost-based margins."""
import io

import pytest
from fastapi.testclient import TestClient

from app import worker
from app.db import session_scope
from app.pipeline.derive import apply_dealer_costs
from app.pipeline.kubota import enrich_models, machine_models, part_code, series_for
from app.pricing import CostLookup, model_key, parse_price_file
from app.schemas import ExtractedDocument, LineItem, OptionItem

PRICE_CSV = (b"Model #,Description,MSRP,Dealer Net\n"
             b"SVL75-3HFWVRCC,75HP CTL cab hi-flow,\"$98,500.00\",\"$74,900.00\"\n"
             b"K7875A,24in QA trenching bucket,2087,1450\n"
             b"AP-HD74LT,74in tooth bucket,2600,1900\n")


def test_parse_csv_and_lookup_tolerates_separators():
    p = parse_price_file(PRICE_CSV, "prices.csv")
    assert len(p.entries) == 3 and p.entries[0]["dealer_cost"] == 74900 and p.entries[0]["msrp"] == 98500
    lk = CostLookup({model_key(e["model_number"]): e["dealer_cost"] for e in p.entries})
    assert lk.cost("svl75‐3hfwvrcc") == 74900  # unicode hyphen, lower case
    assert lk.cost("SVL97-3HFCC") is None


def test_parse_xlsx(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Kubota dealer price list 2026"])
    ws.append([])
    ws.append(["Part Number", "Description", "List Price", "Dealer Cost"])
    ws.append(["KX040-5R3AP", "Excavator cab", 82000, 61500])
    buf = io.BytesIO()
    wb.save(buf)
    p = parse_price_file(buf.getvalue(), "prices.xlsx")
    assert p.entries == [{"model_number": "KX040-5R3AP", "dealer_cost": 61500, "msrp": 82000, "description": "Excavator cab"}]


def test_unrecognised_file_reports_why():
    p = parse_price_file(b"foo,bar\n1,2\n", "x.csv")
    assert not p.entries and "dealer cost column" in p.warnings[0]


def test_models_and_series():
    assert machine_models("KUBOTA SVL75-3HFWVRCC 47407 ... K7875A ... SVL75") == ["SVL75-3HFWVRCC"]
    assert series_for("KX040-5R3AP") == "K Series" and series_for("SVL97-3HFCC") == "S Series"
    assert series_for("SCL1000") is None and series_for("M62") is None  # no reliable mapping: left for review
    assert part_code("New 2025 LAND PRIDE AP-HD74LT 74\" TOOTH BUCKET") == "AP-HD74LT"
    assert part_code("Serial KBCZ053CHS1J47126") is None  # VIN-like serials are not part codes
    d = ExtractedDocument(line_items=[LineItem(description="Kubota KX040-5R3AP $72,304")])
    enrich_models(d)
    assert (d.line_items[0].model, d.line_items[0].series) == ("KX040-5R3AP", "K Series")


def _lookup():
    return CostLookup({"SVL753HFWVRCC": 74900.0, "K7875A": 1450.0}, source="prices.csv")


def test_margin_from_dealer_cost_machine_only():
    d = ExtractedDocument(line_items=[LineItem(model="SVL75-3HFWVRCC", quantity=1, unit_price=82500, amount=82500)])
    notes = apply_dealer_costs(d, _lookup())
    li = d.line_items[0]
    assert li.margin_amount == 7600 and li.margin_percent == round(7600 / 82500 * 100, 2)
    assert d.margin_amount == 7600 and "prices.csv" in d.margin_source and "machine only" in notes[0]


def test_margin_includes_options_when_all_costed():
    li = LineItem(model="SVL75-3HFWVRCC", base_price=80000, quantity=1, options=[OptionItem(name="K7875A bucket", price=2000)])
    d = ExtractedDocument(line_items=[li])
    apply_dealer_costs(d, _lookup())
    assert li.margin_amount == 82000 - (74900 + 1450)


def test_printed_margin_is_not_overwritten_and_unknown_models_skipped():
    d = ExtractedDocument(line_items=[LineItem(model="SVL75-3HFWVRCC", amount=82500, margin_amount=5000),
                                      LineItem(model="ZZ999", amount=1000)])
    apply_dealer_costs(d, _lookup())
    assert d.line_items[0].margin_amount == 5000 and d.line_items[1].margin_amount is None


@pytest.fixture()
def client(monkeypatch):
    from app.service import process_document

    def run_inline(doc_id):
        with session_scope() as s:
            process_document(s, doc_id)

    monkeypatch.setattr(worker, "enqueue", run_inline)
    from app.api.main import app

    with TestClient(app) as c:
        yield c


def test_price_file_upload_recalculates_existing_quotes(client):
    from tests.samples import dealer_quote_pdf

    # A quote with no printed margin
    pdf = dealer_quote_pdf(with_margin=False)
    doc_id = client.post("/api/documents/upload", files={"files": ("q-nomargin.pdf", pdf, "application/pdf")}).json()["documents"][0]["id"]
    before = client.get(f"/api/documents/{doc_id}").json()["record"]["data"]
    assert before["margin_amount"] is None

    csv = b"Model,Dealer Cost\n5050E 4WD,790000\n"
    r = client.post("/api/price-files", files={"file": ("tractor-costs.csv", csv, "text/csv")})
    assert r.status_code == 200 and r.json()["row_count"] == 1 and r.json()["records_recalculated"] >= 1
    after = client.get(f"/api/documents/{doc_id}").json()["record"]["data"]
    # options have no dealer cost in this file, so the margin covers the machine alone
    assert after["line_items"][0]["margin_amount"] == 850000 - 790000 == after["margin_amount"]
    assert after["margin_percent"] == round(60000 / 850000 * 100, 2)
    assert after["margin_source"].startswith("dealer cost: tractor-costs.csv")

    assert client.post("/api/price-files", files={"file": ("bad.csv", b"a,b\n", "text/csv")}).status_code == 422
    pf_id = r.json()["id"]
    assert client.get(f"/api/price-files/{pf_id}/entries").json()["items"][0]["dealer_cost"] == 790000


def test_template_rows_with_zero_cost_are_ignored():
    from pathlib import Path

    data = (Path(__file__).resolve().parents[2] / "docs" / "price-file-template.csv").read_bytes()
    p = parse_price_file(data, "price-file-template.csv")
    assert p.entries == []  # placeholders only: nothing that would produce a fake 100% margin
