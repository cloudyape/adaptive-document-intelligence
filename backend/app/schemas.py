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


class OptionItem(BaseModel):
    """An add-on / optional equipment on a quoted unit."""

    name: str
    price: float | None = Field(None, description="Option price, if printed")


class LineItem(BaseModel):
    description: str | None = None
    series: str | None = Field(None, description="Product line / family, e.g. '5E Series'")
    model: str | None = Field(None, description="Specific model or variant, e.g. '5050E 4WD'")
    options: list[OptionItem] = Field(default_factory=list, description="Add-ons chosen on top of the base model")
    base_price: float | None = Field(None, description="Price of the model alone, before options, discounts and tax")
    msrp: float | None = Field(None, description="Manufacturer's suggested retail / list price, if printed")
    margin_amount: float | None = Field(None, description="Margin By: dealer margin as an amount")
    margin_percent: float | None = Field(None, description="Margin By %: dealer margin as a percentage")
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
    other_charges: float | None = Field(None, description="Doc, title, UCC, processing, setup and similar fees")
    trade_in: float | None = Field(None, description="Trade-in allowance credited against the sale (positive number)")
    round_off: float | None = None
    total: float | None = Field(None, description="Grand total payable")
    payment_terms: str | None = None
    margin_amount: float | None = Field(None, description="Margin By: total dealer margin on the quote")
    margin_percent: float | None = Field(None, description="Margin By %: total margin as a percentage")
    notes: str | None = Field(None, description="Comment / Notes: remarks, conditions, special terms")
    margin_source: str | None = Field(None, description="Where the margin came from, e.g. the price file used")
