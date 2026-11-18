"""Azure DI engines against a mocked REST API (submit -> poll -> result)."""
import json

import httpx
import pytest

from app.config import get_settings
from app.pipeline.engines.azure_di import AzureHighResFallback, AzureInvoiceEngine, AzureReadRulesEngine
from app.pipeline.preprocess import analyze
from app.pipeline.validation import validate
from app.schemas import DocType, ExtractedDocument, Party
from tests.samples import BUYER_GSTIN, VENDOR_GSTIN

READ_CONTENT = f"""Sharma Industrial Supplies Pvt Ltd
GSTIN: {VENDOR_GSTIN}
TAX INVOICE
Invoice No: SIS/2025-26/0142
Invoice Date: 14/08/2025
Bill To:
Tutorlabs Learning Solutions LLP
GSTIN: {BUYER_GSTIN}
1 Steel Office Chair 9401 10 4,500.00 45,000.00
2 Whiteboard 6x4 ft 9610 4 3,250.00 13,000.00
3 Installation Charges 9987 1 2,000.00 2,000.00
Sub Total 60,000.00
IGST @ 18% 10,800.00
Grand Total 70,800.00
Payment Terms: Net 30 days"""


def cur(v):
    return {"type": "currency", "valueCurrency": {"amount": v, "currencyCode": "INR"}, "content": str(v)}


def s(v):
    return {"type": "string", "valueString": v, "content": v}


INVOICE_FIELDS = {
    "VendorName": s("Sharma Industrial Supplies Pvt Ltd"), "VendorTaxId": s(VENDOR_GSTIN[:-1] + "." + VENDOR_GSTIN[-1]),
    "CustomerName": s("Tutorlabs Learning Solutions LLP"),  # no CustomerTaxId: must come from rules merge
    "InvoiceId": s("SIS/2025-26/0142"), "InvoiceDate": {"type": "date", "valueDate": "2025-08-14"},
    "DueDate": {"type": "date", "valueDate": "2025-09-13"},
    "SubTotal": cur(60000), "TotalTax": cur(10800), "InvoiceTotal": cur(70800), "PaymentTerm": s("Net 30 days"),
    "Items": {"type": "array", "valueArray": [
        {"type": "object", "valueObject": {"Description": s("Steel Office Chair"), "Quantity": {"type": "number", "valueNumber": 10},
                                           "UnitPrice": cur(4500), "Amount": cur(45000), "ProductCode": s("9401")}},
        {"type": "object", "valueObject": {"Description": s("Whiteboard 6x4 ft"), "Quantity": {"type": "number", "valueNumber": 4},
                                           "UnitPrice": cur(3250), "Amount": cur(13000), "TaxRate": s("18 %")}},
        {"type": "object", "valueObject": {"Description": s("Installation Charges"), "Quantity": {"type": "number", "valueNumber": 1},
                                           "UnitPrice": cur(2000), "Amount": cur(2000)}},
    ]},
}


class FakeAzure:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["Ocp-Apim-Subscription-Key"] == "test-key"
        if request.method == "POST":
            assert "base64Source" in json.loads(request.content)
            model = request.url.path.split("/")[-1].split(":")[0]
            return httpx.Response(202, headers={"Operation-Location": f"https://di.test/results/{model}"})
        self.polls += 1
        if self.polls == 1:
            return httpx.Response(200, json={"status": "running"})
        model = request.url.path.split("/")[-1]
        result = {"content": READ_CONTENT, "pages": [{"words": [{"content": "x", "confidence": 0.97}]}]}
        if model == "prebuilt-invoice":
            result["documents"] = [{"docType": "invoice", "confidence": 0.93, "fields": INVOICE_FIELDS}]
        return httpx.Response(200, json={"status": "succeeded", "analyzeResult": result})


@pytest.fixture()
def azure(monkeypatch, sample_dir):
    monkeypatch.setenv("ADI_AZURE_DI_ENDPOINT", "https://di.test")
    monkeypatch.setenv("ADI_AZURE_DI_KEY", "test-key")
    get_settings.cache_clear()
    fake = FakeAzure()
    yield fake, httpx.MockTransport(fake.handler), analyze(sample_dir / "invoice_scanned.pdf", "application/pdf", "scan.pdf")
    get_settings.cache_clear()


def test_read_engine_runs_rules_over_azure_ocr(azure, monkeypatch):
    fake, transport, ctx = azure
    import app.pipeline.engines.azure_di as mod
    monkeypatch.setattr(mod.AzureClient, "__init__", _patched_init(mod.AzureClient.__init__))
    r = AzureReadRulesEngine(transport).extract(ctx, DocType.INVOICE)
    assert "prebuilt-read:analyze" in str(fake.calls[0].url) and "features" not in str(fake.calls[0].url)
    assert r.document.total == 70800 and r.document.vendor.gstin == VENDOR_GSTIN
    assert len(r.document.line_items) == 3 and r.cost_usd == get_settings().azure_di_read_cost_per_page
    assert validate(r.document, r.engine_confidence).passed


def test_invoice_engine_maps_fields_and_fills_gst_from_rules(azure, monkeypatch):
    fake, transport, ctx = azure
    import app.pipeline.engines.azure_di as mod
    monkeypatch.setattr(mod.AzureClient, "__init__", _patched_init(mod.AzureClient.__init__))
    r = AzureInvoiceEngine(transport).extract(ctx, DocType.INVOICE)
    d = r.document
    assert (d.document_number, d.document_date, d.currency, d.total) == ("SIS/2025-26/0142", "2025-08-14", "INR", 70800)
    assert d.line_items[1].tax_rate == 18 and d.line_items[0].hsn_sac == "9401"
    assert d.igst == 10800 and d.buyer.gstin == BUYER_GSTIN  # Azure gave neither; rules over Azure's text did
    assert r.engine_confidence == 0.93 and validate(d, r.engine_confidence).passed


def test_highres_fallback_requests_addon_and_merges_previous(azure, monkeypatch):
    fake, transport, ctx = azure
    import app.pipeline.engines.azure_di as mod
    monkeypatch.setattr(mod.AzureClient, "__init__", _patched_init(mod.AzureClient.__init__))
    eng = AzureHighResFallback(transport)
    eng.previous_results = [ExtractedDocument(po_number="PO-77", vendor=Party(pan="AAPFU0939F"))]
    r = eng.extract(ctx, DocType.INVOICE)
    assert "features=ocrHighResolution" in str(fake.calls[0].url)
    assert r.document.po_number == "PO-77" and r.document.vendor.pan == "AAPFU0939F"
    s_ = get_settings()
    assert r.cost_usd == round(s_.azure_di_invoice_cost_per_page + s_.azure_di_highres_cost_per_page, 6)


def test_azure_failure_status_raises(azure, monkeypatch):
    _, _, ctx = azure
    import app.pipeline.engines.azure_di as mod
    monkeypatch.setattr(mod.AzureClient, "__init__", _patched_init(mod.AzureClient.__init__))

    def handler(req):
        if req.method == "POST":
            return httpx.Response(202, headers={"Operation-Location": "https://di.test/r/1"})
        return httpx.Response(200, json={"status": "failed", "error": {"code": "InvalidContent"}})

    with pytest.raises(RuntimeError, match="InvalidContent"):
        AzureInvoiceEngine(httpx.MockTransport(handler)).extract(ctx)


def _patched_init(orig):
    def init(self, transport=None, poll_interval=1.5):
        orig(self, transport=transport, poll_interval=0)  # no sleeping in tests
    return init


def test_row_text_rebuilds_rows_from_polygons():
    from app.pipeline.engines.azure_di import AnalyzeResult

    def word(text, x, y):  # polygon: 4 corners, x/y pairs, in inches
        return {"content": text, "confidence": 0.99, "polygon": [x, y, x + 0.5, y, x + 0.5, y + 0.15, x, y + 0.15]}

    raw = {"content": "Grand Total\nIGST @ 18%\n70,800.00\n10,800.00",  # reading order splits labels/values
           "pages": [{"words": [word("IGST", 1, 5.0), word("@", 1.6, 5.0), word("18%", 1.9, 5.01), word("10,800.00", 6, 5.02),
                                word("Grand", 1, 5.3), word("Total", 1.6, 5.31), word("70,800.00", 6, 5.3)]}]}
    assert AnalyzeResult(raw, 1).row_text().splitlines() == ["IGST @ 18% 10,800.00", "Grand Total 70,800.00"]
