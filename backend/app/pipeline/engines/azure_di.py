"""Azure AI Document Intelligence (REST, API 2024-11-30) - the only paid engines.

Tier 3  AzureReadRulesEngine     prebuilt-read OCR (cheapest Azure model) -> local rules engine
Tier 4  AzureInvoiceEngine       prebuilt-invoice fields + line items, gaps filled by rules over its OCR text
Tier 5  AzureHighResFallback     prebuilt-invoice + ocrHighResolution add-on, merged with earlier attempts

Per-page prices come from settings (ADI_AZURE_DI_*_COST_PER_PAGE) - set them from your Azure pricing.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass, replace

import httpx

from app.config import get_settings
from app.pipeline.engines.base import EngineResult, EngineUnavailable
from app.pipeline.engines.rules import RulesEngine
from app.pipeline.merge import merge
from app.pipeline.normalize import parse_amount, parse_date
from app.pipeline.preprocess import IMAGE_MIMES, PDF_MIME, DocumentContext, PageInfo, _rows_from_words
from app.schemas import DocType, ExtractedDocument, LineItem, Party

log = logging.getLogger(__name__)
AZURE_MIMES = {PDF_MIME, *IMAGE_MIMES}


@dataclass
class AnalyzeResult:
    raw: dict
    pages: int

    @property
    def content(self) -> str:
        return self.raw.get("content") or ""

    def row_text(self) -> str:
        """Text rebuilt into visual rows from word polygons, so labels sit next to their amounts.

        Azure's `content` is in reading order, which separates a "Grand Total" label from its value
        in multi-column layouts. Falls back to `content` when words have no polygons.
        """
        out = []
        for page in self.raw.get("pages", []):
            data = {"text": [], "top": [], "left": [], "height": []}
            for w in page.get("words", []):
                poly = w.get("polygon") or []
                if len(poly) < 8:
                    continue
                ys, xs = poly[1::2], poly[0::2]
                data["text"].append(w.get("content", ""))
                data["top"].append(min(ys))
                data["left"].append(min(xs))
                data["height"].append(max(ys) - min(ys))
            if data["text"]:
                out.append(_rows_from_words(data))
        return "\n".join(out) if out else self.content

    def word_confidence(self) -> float | None:
        confs = [w["confidence"] for p in self.raw.get("pages", []) for w in p.get("words", []) if "confidence" in w]
        return sum(confs) / len(confs) if confs else None


class AzureClient:
    def __init__(self, transport: httpx.BaseTransport | None = None, poll_interval: float = 1.5):
        s = get_settings()
        if not (s.azure_di_endpoint and s.azure_di_key):
            raise EngineUnavailable("set ADI_AZURE_DI_ENDPOINT and ADI_AZURE_DI_KEY")
        self.base = s.azure_di_endpoint.rstrip("/")
        self.headers = {"Ocp-Apim-Subscription-Key": s.azure_di_key}
        self.api_version = s.azure_di_api_version
        self.timeout = s.azure_di_timeout_seconds
        self.poll_interval = poll_interval
        self._transport = transport

    def analyze(self, model_id: str, data: bytes, features: list[str] | None = None) -> AnalyzeResult:
        params = {"api-version": self.api_version}
        if features:
            params["features"] = ",".join(features)
        url = f"{self.base}/documentintelligence/documentModels/{model_id}:analyze"
        with httpx.Client(timeout=60, transport=self._transport) as http:
            resp = http.post(url, params=params, headers=self.headers, json={"base64Source": base64.b64encode(data).decode()})
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", "5")))
                resp = http.post(url, params=params, headers=self.headers, json={"base64Source": base64.b64encode(data).decode()})
            resp.raise_for_status()
            op_url = resp.headers["Operation-Location"]
            deadline = time.monotonic() + self.timeout
            while True:
                poll = http.get(op_url, headers=self.headers)
                poll.raise_for_status()
                body = poll.json()
                status = body.get("status")
                if status == "succeeded":
                    break
                if status == "failed":
                    raise RuntimeError(f"Azure DI {model_id} failed: {body.get('error')}")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Azure DI {model_id} still '{status}' after {self.timeout}s")
                time.sleep(float(poll.headers.get("Retry-After", self.poll_interval)) if self.poll_interval else 0)
        result = body.get("analyzeResult") or {}
        return AnalyzeResult(raw=result, pages=len(result.get("pages") or []) or 1)


def _client_or_unavailable(ctx: DocumentContext, transport=None) -> AzureClient:
    if ctx.mime_type not in AZURE_MIMES:
        raise EngineUnavailable(f"Azure DI not used for {ctx.mime_type}")
    return AzureClient(transport=transport)


def _rules_over_text(ctx: DocumentContext, text: str, confidence: float | None, hint: DocType | None) -> EngineResult:
    """Run the local rules engine on text recognised by Azure."""
    pages = [PageInfo(1, text, "ocr", confidence * 100 if confidence is not None else None)]
    return RulesEngine().extract(replace(ctx, pages=pages, _page_images=None), hint)


class _AzureBase:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport  # injectable for tests

    def available(self) -> bool:
        s = get_settings()
        return bool(s.azure_di_endpoint and s.azure_di_key)


class AzureReadRulesEngine(_AzureBase):
    name = "azure_read"
    tier = 3

    def available(self) -> bool:
        return super().available() and get_settings().azure_di_read_enabled

    def extract(self, ctx: DocumentContext, doc_type_hint: DocType | None = None) -> EngineResult:
        result = _client_or_unavailable(ctx, self.transport).analyze("prebuilt-read", ctx.raw_bytes)
        er = _rules_over_text(ctx, result.row_text(), result.word_confidence(), doc_type_hint)
        er.cost_usd = round(result.pages * get_settings().azure_di_read_cost_per_page, 6)
        er.model = "prebuilt-read"
        return er


# ---------- prebuilt-invoice mapping ----------
def _val(field: dict | None):
    if not field:
        return None
    for key in ("valueCurrency", "valueNumber", "valueDate", "valueString", "valueInteger"):
        if key in field:
            v = field[key]
            return v.get("amount") if key == "valueCurrency" and isinstance(v, dict) else v
    if "valueAddress" in field or field.get("type") == "address":
        return field.get("content")
    return field.get("content")


def _num(field: dict | None) -> float | None:
    v = _val(field)
    return v if isinstance(v, (int, float)) else parse_amount(v)


def _rate(field: dict | None) -> float | None:
    v = _val(field)
    return parse_amount(str(v).replace("%", "")) if v not in (None, "") else None


def map_invoice(result: AnalyzeResult, hint: DocType | None) -> tuple[ExtractedDocument, float]:
    docs = result.raw.get("documents") or [{}]
    f = docs[0].get("fields", {})
    items = []
    for it in (f.get("Items") or {}).get("valueArray", []):
        o = it.get("valueObject", {})
        items.append(LineItem(
            description=_val(o.get("Description")), hsn_sac=_val(o.get("ProductCode")),
            quantity=_num(o.get("Quantity")), unit=_val(o.get("Unit")), unit_price=_num(o.get("UnitPrice")),
            tax_rate=_rate(o.get("TaxRate")), amount=_num(o.get("Amount")),
        ))
    total_field = f.get("InvoiceTotal") or f.get("AmountDue") or {}
    currency = (total_field.get("valueCurrency") or {}).get("currencyCode")
    date = _val(f.get("InvoiceDate"))
    due = _val(f.get("DueDate"))
    doc = ExtractedDocument(
        document_type=hint or DocType.INVOICE,
        document_number=_val(f.get("InvoiceId")),
        document_date=parse_date(date) or date,
        due_date=parse_date(due) or due,
        po_number=_val(f.get("PurchaseOrder")),
        currency=currency,
        vendor=Party(name=_val(f.get("VendorName")) or _val(f.get("VendorAddressRecipient")),
                     address=_val(f.get("VendorAddress")), gstin=_val(f.get("VendorTaxId"))),
        buyer=Party(name=_val(f.get("CustomerName")) or _val(f.get("CustomerAddressRecipient")),
                    address=_val(f.get("CustomerAddress")), gstin=_val(f.get("CustomerTaxId"))),
        line_items=items,
        subtotal=_num(f.get("SubTotal")),
        discount_total=_num(f.get("TotalDiscount")),
        tax_total=_num(f.get("TotalTax")),
        total=_num(f.get("InvoiceTotal")) or _num(f.get("AmountDue")),
        payment_terms=_val(f.get("PaymentTerm")),
    )
    for party in (doc.vendor, doc.buyer):
        if party.gstin:  # Azure keeps OCR punctuation in tax IDs ("27AAPFU0939F1Z.V")
            party.gstin = re.sub(r"[^0-9A-Z]", "", party.gstin.upper()) or None
    return doc, float(docs[0].get("confidence") or 0.5)


class AzureInvoiceEngine(_AzureBase):
    name = "azure_invoice"
    tier = 4
    features: list[str] = []

    def _price_per_page(self) -> float:
        return get_settings().azure_di_invoice_cost_per_page

    def extract(self, ctx: DocumentContext, doc_type_hint: DocType | None = None) -> EngineResult:
        result = _client_or_unavailable(ctx, self.transport).analyze("prebuilt-invoice", ctx.raw_bytes, self.features)
        doc, confidence = map_invoice(result, doc_type_hint)
        # Azure has no CGST/SGST/IGST split, quotation validity, or GSTIN checks: fill from rules over its OCR text.
        rules = _rules_over_text(ctx, result.row_text(), result.word_confidence(), doc_type_hint).document
        doc = merge(doc, *self._extra_sources(), rules)
        return EngineResult(document=doc, engine_confidence=confidence,
                            cost_usd=round(result.pages * self._price_per_page(), 6), model="prebuilt-invoice")

    def _extra_sources(self) -> list[ExtractedDocument]:
        return []


class AzureHighResFallback(AzureInvoiceEngine):
    """Last automatic resort: high-resolution OCR (small/handwritten text) plus everything earlier tiers found."""

    name = "azure_invoice_highres"
    tier = 5
    features = ["ocrHighResolution"]

    def __init__(self, transport: httpx.BaseTransport | None = None):
        super().__init__(transport)
        self.previous_results: list[ExtractedDocument] = []

    def available(self) -> bool:
        return super().available() and get_settings().azure_di_highres_fallback

    def _price_per_page(self) -> float:
        s = get_settings()
        return s.azure_di_invoice_cost_per_page + s.azure_di_highres_cost_per_page

    def _extra_sources(self) -> list[ExtractedDocument]:
        return self.previous_results
