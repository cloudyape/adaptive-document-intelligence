"""Dealer-quote fields: Series, Model, Options, Base Price, Margin By, Margin By %, Comment/Notes."""
import pytest

from app.pipeline.derive import derive
from app.pipeline.engines.rules import RulesEngine, _cut, parse_options_text
from app.pipeline.identify import identify
from app.pipeline.merge import merge
from app.pipeline.preprocess import DocumentContext, PageInfo, analyze
from app.pipeline.validation import validate
from app.schemas import DocType, ExtractedDocument, LineItem, OptionItem, Party
from tests import samples


@pytest.fixture(scope="module")
def quote_ctx(tmp_path_factory):
    p = tmp_path_factory.mktemp("dq") / "dealer_quote.pdf"
    p.write_bytes(samples.dealer_quote_pdf())
    return analyze(p, "application/pdf", p.name)


def test_label_style_quote_extracts_all_dealer_fields(quote_ctx):
    ident = identify(quote_ctx.text, "dealer_quote.pdf")
    assert ident.doc_type == DocType.QUOTATION
    r = RulesEngine().extract(quote_ctx, ident.doc_type)
    d = r.document
    li = d.line_items[0]
    assert (li.series, li.model, li.base_price) == ("5E Series", "5050E 4WD", 850000)
    assert [(o.name, o.price) for o in li.options] == [("Front End Loader", 120000), ("Rotavator 7 ft", 65000)]
    assert li.amount == 1035000 == d.subtotal  # base + options, cross-checked against the printed subtotal
    assert (li.margin_amount, li.margin_percent) == (51750, 5.0)
    assert (d.margin_amount, d.margin_percent) == (51750, 5.0)
    assert d.notes.startswith("Delivery within 3 weeks") and d.notes.endswith("first two free services.")
    assert d.valid_until == "2026-12-10" and d.total == 1159200
    rep = validate(d, r.engine_confidence)
    assert rep.passed, rep.messages()


def test_table_style_multi_unit_quote():
    rows = [
        ["Series", "Model", "Options", "Qty", "Base Price", "Margin", "Margin %", "Amount"],
        ["5E Series", "5050E", "Loader 1,20,000; Canopy 15,000", "2", "8,50,000", "98,500", "5", "19,70,000"],
        ["3E Series", "3028EN", "", "1", "6,10,000", "30,500", "5%", "6,10,000"],
    ]
    idx, cols = RulesEngine._find_header(rows)
    assert idx == 0 and cols["base_price"] == 4 and cols["margin_percent"] == 6 and cols["margin_amount"] == 5
    items = [RulesEngine._row_to_item(r, cols) for r in rows[1:]]
    assert items[0].model == "5050E" and items[0].base_price == 850000 and items[0].quantity == 2
    assert [(o.name, o.price) for o in items[0].options] == [("Loader", 120000), ("Canopy", 15000)]
    assert items[1].margin_percent == 5 and items[1].options == []


def test_options_parsing_and_label_cut():
    opts = parse_options_text("Sunroof ₹45,000; Tow hitch 12,000/-, Mud flaps")
    assert [(o.name, o.price) for o in opts] == [("Sunroof", 45000), ("Tow hitch", 12000), ("Mud flaps", None)]
    assert _cut("5E Series Quotation No: Q-1") == "5E Series"
    assert _cut("5050E 4WD Date: 10/11/2026") == "5050E 4WD"


def test_derive_fills_missing_margin_half():
    d = ExtractedDocument(line_items=[LineItem(base_price=1000, options=[OptionItem(name="x", price=200)], margin_percent=10)])
    derive(d)
    assert d.line_items[0].margin_amount == 120  # 10% of base + options
    assert (d.margin_amount, d.margin_percent) == (120, 10)

    d = ExtractedDocument(line_items=[LineItem(amount=5000, margin_amount=250)])
    derive(d)
    assert d.line_items[0].margin_percent == 5


def _quote(**kw) -> ExtractedDocument:
    base = dict(document_type=DocType.QUOTATION, document_number="Q-1", document_date="2026-11-10", currency="INR",
                vendor=Party(name="Dealer"), subtotal=1000, tax_total=0, total=1000,
                line_items=[LineItem(description="Unit", quantity=1, unit_price=1000, amount=1000, base_price=1000)])
    base.update(kw)
    return ExtractedDocument(**base)


def test_margin_validation():
    assert validate(_quote(margin_amount=50, margin_percent=5)).passed
    rep = validate(_quote(margin_amount=50, margin_percent=500))
    assert any(i.code == "margin_range" and i.severity == "error" for i in rep.issues) and not rep.passed
    rep = validate(_quote(margin_amount=50, margin_percent=8))
    assert any(i.code == "margin_mismatch" for i in rep.issues)
    rep = validate(_quote(margin_amount=5000))
    assert any(i.code == "margin_exceeds_total" for i in rep.issues)


def test_base_plus_options_mismatch_warns():
    li = LineItem(description="Unit", quantity=1, unit_price=1300, amount=1300, base_price=1000,
                  options=[OptionItem(name="a", price=200)])
    rep = validate(_quote(line_items=[li], subtotal=1300, total=1300))
    assert any(i.code == "base_plus_options" for i in rep.issues)


def test_merge_carries_dealer_fields_from_rules_into_azure_result():
    azure = ExtractedDocument(line_items=[LineItem(description="Tractor", amount=1035000)])
    rules = ExtractedDocument(notes="n", margin_amount=51750, line_items=[
        LineItem(series="5E Series", model="5050E", base_price=850000, options=[OptionItem(name="Loader", price=120000)])])
    m = merge(azure, rules)
    li = m.line_items[0]
    assert (li.description, li.series, li.model, li.base_price) == ("Tractor", "5E Series", "5050E", 850000)
    assert li.options[0].name == "Loader" and m.notes == "n" and m.margin_amount == 51750
    assert rules.line_items[0].options[0] is not li.options[0]  # copied, not shared


def test_existing_database_gets_new_columns(tmp_path, monkeypatch):
    """A DB created before these fields existed is upgraded in place, keeping its rows."""
    from sqlalchemy import create_engine, inspect, text

    import app.db as db

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE line_items (id INTEGER PRIMARY KEY, record_id INTEGER, position INTEGER, description TEXT)"))
        c.execute(text("INSERT INTO line_items (record_id, position, description) VALUES (1, 0, 'old row')"))
    monkeypatch.setattr(db, "engine", eng)
    added = db.add_missing_columns()
    cols = {c["name"] for c in inspect(eng).get_columns("line_items")}
    assert {"series", "model", "options", "base_price", "margin_amount", "margin_percent"} <= cols
    assert "line_items.margin_percent" in added
    with eng.connect() as c:
        assert c.execute(text("SELECT description FROM line_items")).scalar() == "old row"
    assert db.add_missing_columns() == []  # idempotent


def test_text_ctx_fields():
    text = "ACME Motors\nQuotation No: Q-77\nModel: XUV 7OO AX7\nOptions: Sunroof 45,000, Dashcam 8,000\nNotes: Ex-showroom Pune"
    d = RulesEngine().extract(DocumentContext(None, "text/plain", "q.txt", [PageInfo(1, text, "text_layer")]), DocType.QUOTATION).document
    assert d.line_items[0].model == "XUV 7OO AX7" and len(d.line_items[0].options) == 2 and d.notes == "Ex-showroom Pune"
