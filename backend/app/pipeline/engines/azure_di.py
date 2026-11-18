"""Tier 3 (optional) - Azure AI Document Intelligence `prebuilt-invoice` via REST.

Used for DIFFICULT documents when ADI_AZURE_DI_ENDPOINT / ADI_AZURE_DI_KEY are set; otherwise the
orchestrator uses the Claude vision engine at tier 3. Only DIFFICULT docs ever reach this engine.
"""
from __future__ import annotations

import base64
import time

import httpx

from app.config import get_settings
from app.pipeline.engines.base import EngineResult
from app.pipeline.normalize import parse_date
from app.pipeline.preprocess import DocumentContext
from app.schemas import DocType, ExtractedDocument, LineItem, Party

API_VERSION = "2024-11-30"


def _val(field: dict | None):
    if not field:
        return None
    t = field.get("type")
    if t == "currency":
        return (field.get("valueCurrency") or {}).get("amount")
    if t == "number":
        return field.get("valueNumber")
    if t == "date":
        return field.get("valueDate")
    if t == "address":
        return field.get("content")
    return field.get("valueString") or field.get("content")


class AzureDocumentIntelligenceEngine:
    name = "azure_di"
    tier = 3

    def available(self) -> bool:
        s = get_settings()
        return bool(s.azure_di_endpoint and s.azure_di_key)

    def extract(self, ctx: DocumentContext, doc_type_hint: DocType | None = None) -> EngineResult:
        s = get_settings()
        url = f"{s.azure_di_endpoint.rstrip('/')}/documentintelligence/documentModels/prebuilt-invoice:analyze"
        headers = {"Ocp-Apim-Subscription-Key": s.azure_di_key}
        with httpx.Client(timeout=60) as http:
            resp = http.post(url, params={"api-version": API_VERSION}, headers=headers,
                             json={"base64Source": base64.b64encode(ctx.raw_bytes).decode()})
            resp.raise_for_status()
            op_url = resp.headers["Operation-Location"]
            for _ in range(60):
                time.sleep(2)
                poll = http.get(op_url, headers=headers).json()
                if poll.get("status") in ("succeeded", "failed"):
                    break
        if poll.get("status") != "succeeded":
            raise RuntimeError(f"Azure DI analysis {poll.get('status')}: {poll.get('error')}")

        result = poll["analyzeResult"]
        docs = result.get("documents") or [{}]
        f = docs[0].get("fields", {})
        items = []
        for it in (f.get("Items") or {}).get("valueArray", []):
            o = it.get("valueObject", {})
            rate = _val(o.get("TaxRate"))
            items.append(LineItem(
                description=_val(o.get("Description")), hsn_sac=_val(o.get("ProductCode")),
                quantity=_val(o.get("Quantity")), unit=_val(o.get("Unit")), unit_price=_val(o.get("UnitPrice")),
                tax_rate=float(str(rate).rstrip("%")) if rate else None, amount=_val(o.get("Amount")),
            ))
        currency = ((f.get("InvoiceTotal") or {}).get("valueCurrency") or {}).get("currencyCode")
        doc = ExtractedDocument(
            document_type=doc_type_hint or DocType.INVOICE,
            document_number=_val(f.get("InvoiceId")),
            document_date=parse_date(_val(f.get("InvoiceDate"))) or _val(f.get("InvoiceDate")),
            due_date=_val(f.get("DueDate")),
            po_number=_val(f.get("PurchaseOrder")),
            currency=currency,
            vendor=Party(name=_val(f.get("VendorName")), address=_val(f.get("VendorAddress")), gstin=_val(f.get("VendorTaxId"))),
            buyer=Party(name=_val(f.get("CustomerName")), address=_val(f.get("CustomerAddress")), gstin=_val(f.get("CustomerTaxId"))),
            line_items=items,
            subtotal=_val(f.get("SubTotal")),
            discount_total=_val(f.get("TotalDiscount")),
            tax_total=_val(f.get("TotalTax")),
            total=_val(f.get("InvoiceTotal")) or _val(f.get("AmountDue")),
            payment_terms=_val(f.get("PaymentTerm")),
        )
        confidence = float(docs[0].get("confidence") or 0.5)
        pages = len(result.get("pages") or []) or ctx.page_count or 1
        return EngineResult(document=doc, engine_confidence=confidence, cost_usd=round(pages * s.azure_di_cost_per_page, 4), model="prebuilt-invoice")
