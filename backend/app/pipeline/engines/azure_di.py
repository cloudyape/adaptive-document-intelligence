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
import threading
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


class _Pacer:
    """One shared clock for every Azure call in the process, so all worker threads together stay under the
    tier's per-minute limit (the free F0 tier allows roughly 20 requests a minute; polls count too)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, per_minute: int) -> None:
        if per_minute <= 0:
            return
        gap = 60.0 / per_minute
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + gap
        if at > now:
            time.sleep(at - now)

    def back_off(self, seconds: float) -> None:
        """Azure said "too many requests": hold every thread, not just the one that was refused."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


PACER = _Pacer()
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class AzureClient:
    def __init__(self, transport: httpx.BaseTransport | None = None, poll_interval: float = 1.5):
        s = get_settings()
        if not (s.azure_di_endpoint and s.azure_di_key):
            raise EngineUnavailable("set ADI_AZURE_DI_ENDPOINT and ADI_AZURE_DI_KEY")
        self.base = s.azure_di_endpoint.rstrip("/")
        self.headers = {"Ocp-Apim-Subscription-Key": s.azure_di_key}
        self.api_version = s.azure_di_api_version
        self.timeout = s.azure_di_timeout_seconds
        self.per_minute = s.azure_di_requests_per_minute
        self.retries = s.azure_di_max_retries
        self.max_bytes = int(s.azure_di_max_file_mb * 1024 * 1024)
        self.poll_interval = poll_interval if poll_interval else 0
        if transport is None:
            self.poll_interval = max(self.poll_interval, s.azure_di_poll_seconds)
        self._transport = transport

    def _send(self, http: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
        """One request with pacing and retries. Retries cover throttling (429), Azure-side errors (5xx) and
        dropped connections (e.g. "SSL: UNEXPECTED_EOF_WHILE_READING"), which Azure does when it sheds load."""
        delay = 2.0
        for attempt in range(self.retries + 1):
            PACER.wait(self.per_minute if self._transport is None else 0)
            try:
                resp = http.request(method, url, headers=self.headers, **kw)
            except httpx.TransportError as exc:  # connection reset / TLS EOF / timeout
                if attempt == self.retries:
                    raise RuntimeError(f"Azure DI connection failed after {attempt + 1} tries: {exc}") from exc
                log.warning("Azure DI %s %s: %s; retrying in %.0fs", method, url.split("?")[0], exc, delay)
                wait = delay
            else:
                if resp.status_code not in RETRY_STATUS:
                    resp.raise_for_status()
                    return resp
                if attempt == self.retries:
                    reason = "too many requests (rate limit)" if resp.status_code == 429 else f"HTTP {resp.status_code}"
                    raise RuntimeError(f"Azure DI {reason} after {attempt + 1} tries")
                try:
                    wait = max(float(resp.headers.get("Retry-After", delay)), 1.0)
                except ValueError:
                    wait = delay
                if resp.status_code == 429:
                    PACER.back_off(wait)
                log.warning("Azure DI HTTP %s; retrying in %.0fs", resp.status_code, wait)
            if self._transport is None:
                time.sleep(wait)
            delay = min(delay * 2, 60)
        raise AssertionError("unreachable")

    def analyze(self, model_id: str, data: bytes, features: list[str] | None = None) -> AnalyzeResult:
        if self.max_bytes and len(data) > self.max_bytes:
            raise EngineUnavailable(f"file is {len(data) / 1048576:.1f} MB, over the {self.max_bytes / 1048576:.0f} MB "
                                    "Azure limit for this tier (ADI_AZURE_DI_MAX_FILE_MB)")
        params = {"api-version": self.api_version}
        if features:
            params["features"] = ",".join(features)
        url = f"{self.base}/documentintelligence/documentModels/{model_id}:analyze"
        with httpx.Client(timeout=httpx.Timeout(120, connect=20), transport=self._transport) as http:
            resp = self._send(http, "POST", url, params=params, json={"base64Source": base64.b64encode(data).decode()})
            op_url = resp.headers["Operation-Location"]
            deadline = time.monotonic() + self.timeout
            while True:
                body = self._send(http, "GET", op_url).json()
                status = body.get("status")
                if status == "succeeded":
                    break
                if status == "failed":
                    raise RuntimeError(f"Azure DI {model_id} failed: {body.get('error')}")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Azure DI {model_id} still '{status}' after {self.timeout}s")
                if self.poll_interval:
                    time.sleep(self.poll_interval)
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
