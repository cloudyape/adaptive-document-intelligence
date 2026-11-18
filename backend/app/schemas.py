"""The standardized financial-document schema every engine must produce."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class DocType(StrEnum):
    INVOICE = "invoice"
    QUOTATION = "quotation"
    PROFORMA = "proforma_invoice"
    CREDIT_NOTE = "credit_note"
    PURCHASE_ORDER = "purchase_order"
    OTHER = "other"


FINANCIAL_TYPES = {DocType.INVOICE, DocType.QUOTATION, DocType.PROFORMA, DocType.CREDIT_NOTE, DocType.PURCHASE_ORDER}


class Complexity(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    DIFFICULT = "difficult"


class DocStatus(StrEnum):
    RECEIVED = "received"
    IGNORED = "ignored"  # identified as non-financial
    PROCESSING = "processing"
    EXTRACTED = "extracted"  # auto-accepted after validation
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class Party(BaseModel):
    name: str | None = None
    address: str | None = None
    gstin: str | None = Field(None, description="15-character Indian GSTIN, if present")
    pan: str | None = None
    email: str | None = None
    phone: str | None = None


class LineItem(BaseModel):
    description: str | None = None
    hsn_sac: str | None = Field(None, description="HSN or SAC code")
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    discount: float | None = Field(None, description="Discount amount for this line, in currency")
    tax_rate: float | None = Field(None, description="Total GST/VAT rate in percent, e.g. 18")
    amount: float | None = Field(None, description="Line amount as printed, before tax")


class ExtractedDocument(BaseModel):
    document_type: DocType = DocType.OTHER
    document_number: str | None = Field(None, description="Invoice / quotation number")
    document_date: str | None = Field(None, description="ISO date YYYY-MM-DD")
    due_date: str | None = Field(None, description="ISO date YYYY-MM-DD")
    valid_until: str | None = Field(None, description="Quotation validity date, ISO YYYY-MM-DD")
    po_number: str | None = None
    currency: str | None = Field(None, description="ISO 4217 code, e.g. INR")
    vendor: Party = Field(default_factory=Party)
    buyer: Party = Field(default_factory=Party)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = Field(None, description="Sum of line amounts before tax")
    discount_total: float | None = None
    cgst: float | None = None
    sgst: float | None = None
    igst: float | None = None
    cess: float | None = None
    tax_total: float | None = Field(None, description="Total tax amount")
    shipping: float | None = None
    round_off: float | None = None
    total: float | None = Field(None, description="Grand total payable")
    payment_terms: str | None = None
    notes: str | None = None
