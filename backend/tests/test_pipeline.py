from app.pipeline.complexity import classify
from app.pipeline.engines.base import EngineResult
from app.pipeline.engines.rules import RulesEngine
from app.pipeline.identify import identify
from app.pipeline.normalize import gstin_checksum_ok, parse_amount, parse_date
from app.pipeline.orchestrator import Orchestrator
from app.pipeline.preprocess import analyze
from app.pipeline.validation import validate
from app.schemas import Complexity, DocType, ExtractedDocument, LineItem, Party
from tests.samples import BUYER_GSTIN, VENDOR_GSTIN


def ctx_for(sample_dir, name, mime="application/pdf"):
    return analyze(sample_dir / name, mime, name)


# ---------- normalizers ----------
def test_amounts_and_dates():
    assert parse_amount("3,10,000.00") == 310000.0
    assert parse_amount("₹ 1,234.50") == 1234.5
    assert parse_amount("1.234,56") == 1234.56
    assert parse_amount("(500.00)") == -500.0
    assert parse_date("14/08/2025") == "2025-08-14"
    assert parse_date("5-Sep-2025") == "2025-09-05"
    assert parse_date("2025-09-05") == "2025-09-05"
    assert parse_date("September 5, 2025") == "2025-09-05"
    assert parse_date("31/02/2025") is None


def test_gstin_checksum():
    assert gstin_checksum_ok(VENDOR_GSTIN)
    assert gstin_checksum_ok(BUYER_GSTIN)
    assert not gstin_checksum_ok(VENDOR_GSTIN[:-1] + "X")  # one misread character


# ---------- identification + complexity ----------
def test_identify_types(sample_dir):
    assert identify(ctx_for(sample_dir, "invoice_digital.pdf").text, "invoice_digital.pdf").doc_type == DocType.INVOICE
    assert identify(ctx_for(sample_dir, "quotation_digital.pdf").text, "q.pdf").doc_type == DocType.QUOTATION
    letter = identify(ctx_for(sample_dir, "letter.pdf").text, "letter.pdf")
    assert letter.doc_type == DocType.OTHER and letter.confidence >= 0.6


def test_complexity_routing(sample_dir):
    assert classify(ctx_for(sample_dir, "invoice_digital.pdf")).level == Complexity.EASY
    assert classify(ctx_for(sample_dir, "invoice_email.html", "text/html")).level == Complexity.EASY
    scanned = classify(ctx_for(sample_dir, "invoice_scanned.pdf"))
    assert scanned.level in (Complexity.MEDIUM, Complexity.DIFFICULT)
    assert any("scanned" in r for r in scanned.reasons)


# ---------- tier 1 rules engine ----------
def test_rules_engine_digital_invoice(sample_dir):
    r = RulesEngine().extract(ctx_for(sample_dir, "invoice_digital.pdf"), DocType.INVOICE)
    d = r.document
    assert d.document_number == "SIS/2025-26/0142"
    assert (d.document_date, d.due_date) == ("2025-08-14", "2025-09-13")
    assert d.vendor.name == "Sharma Industrial Supplies Pvt Ltd"
    assert (d.vendor.gstin, d.buyer.gstin) == (VENDOR_GSTIN, BUYER_GSTIN)
    assert [li.amount for li in d.line_items] == [45000.0, 13000.0, 2000.0]
    assert (d.subtotal, d.igst, d.total) == (60000.0, 10800.0, 70800.0)
    assert "Net 30" in d.payment_terms
    assert validate(d, r.engine_confidence).passed


def test_rules_engine_quotation_indian_grouping(sample_dir):
    d = RulesEngine().extract(ctx_for(sample_dir, "quotation_digital.pdf"), DocType.QUOTATION).document
    assert d.valid_until == "2025-10-02"
    assert d.line_items[0].unit_price == 62000.0 and d.line_items[0].amount == 310000.0
    assert (d.cgst, d.sgst, d.total) == (29700.0, 29700.0, 389400.0)


def test_rules_engine_email_body(sample_dir):
    d = RulesEngine().extract(ctx_for(sample_dir, "invoice_email.html", "text/html"), DocType.INVOICE).document
    assert d.vendor.name == "Quick Prints & Co" and d.total == 10030.0 and len(d.line_items) == 2


# ---------- validation ----------
def _good() -> ExtractedDocument:
    return ExtractedDocument(
        document_type=DocType.INVOICE, document_number="A-1", document_date="2025-08-01", currency="INR",
        vendor=Party(name="V", gstin=VENDOR_GSTIN), buyer=Party(name="B", gstin=BUYER_GSTIN),
        line_items=[LineItem(description="x", quantity=2, unit_price=50, amount=100)],
        subtotal=100, igst=18, tax_total=18, total=118,
    )


def test_validation_catches_math_and_gstin_errors():
    assert validate(_good(), 0.9).passed
    bad = _good()
    bad.total = 181  # transposed digits
    rep = validate(bad, 0.99)
    assert not rep.passed and any(i.code == "total_mismatch" for i in rep.critical)
    bad = _good()
    bad.vendor.gstin = VENDOR_GSTIN[:-1] + "X"
    assert any(i.code == "bad_checksum" for i in validate(bad).issues)
    bad = _good()
    bad.line_items[0].amount = 90
    assert any(i.code == "line_math" for i in validate(bad).issues)


# ---------- orchestration ----------
class FakeEngine:
    def __init__(self, name, tier, doc=None, available=True, cost=0.01, raises=None):
        self.name, self.tier, self._doc, self._avail, self._cost, self._raises = name, tier, doc, available, cost, raises
        self.calls = 0

    def available(self):
        return self._avail

    def extract(self, ctx, hint=None):
        self.calls += 1
        if self._raises:
            raise self._raises
        return EngineResult(document=self._doc, engine_confidence=0.9, cost_usd=self._cost)


def test_easy_doc_accepted_at_tier1_without_calling_azure(sample_dir):
    azure = FakeEngine("azure_invoice", 4, _good())
    out = Orchestrator([RulesEngine(), azure]).run(ctx_for(sample_dir, "invoice_digital.pdf"), Complexity.EASY, DocType.INVOICE)
    assert out.accepted and out.best.engine == "rules" and out.total_cost == 0 and azure.calls == 0


def test_escalates_on_validation_failure_then_accepts(sample_dir):
    broken = _good()
    broken.total = 999
    t2 = FakeEngine("local_ocr", 2, broken, cost=0)
    t3 = FakeEngine("azure_read", 3, _good(), cost=0.0015)
    t4 = FakeEngine("azure_invoice", 4, _good(), cost=0.01)
    out = Orchestrator([t2, t3, t4]).run(ctx_for(sample_dir, "invoice_scanned.pdf"), Complexity.MEDIUM, DocType.INVOICE)
    assert out.accepted and out.best.engine == "azure_read"
    assert [a.engine for a in out.attempts] == ["local_ocr", "azure_read"] and t4.calls == 0
    assert out.total_cost == 0.0015


def test_difficult_starts_at_azure_invoice_and_skips_unavailable(sample_dir):
    t2 = FakeEngine("local_ocr", 2, _good())
    t3 = FakeEngine("azure_read", 3, _good())
    t4 = FakeEngine("azure_invoice", 4, None, available=False)
    t5 = FakeEngine("azure_invoice_highres", 5, _good())
    out = Orchestrator([t2, t3, t4, t5]).run(ctx_for(sample_dir, "invoice_scanned.pdf"), Complexity.DIFFICULT, DocType.INVOICE)
    assert t2.calls == t3.calls == 0 and out.attempts[0].skipped and out.best.engine == "azure_invoice_highres" and out.accepted


def test_all_fail_keeps_best_attempt_for_review(sample_dir):
    worse, better = _good(), _good()
    worse.total, worse.document_number = 5, None
    better.total = 200
    out = Orchestrator([FakeEngine("a", 2, worse), FakeEngine("b", 3, better), FakeEngine("c", 4, raises=RuntimeError("boom"))]).run(
        ctx_for(sample_dir, "invoice_scanned.pdf"), Complexity.MEDIUM, DocType.INVOICE)
    assert not out.accepted and out.best.engine == "b" and out.attempts[-1].error == "RuntimeError: boom"


def test_number_key_folds_ocr_confusions():
    from app.service import number_key

    assert number_key("SIS/2025-26/0142") == number_key("S1S/2025-26/O142") == number_key("sis 2025 26 0142")
    assert number_key("SIS/2025-26/0142") != number_key("SIS/2025-26/0143")
