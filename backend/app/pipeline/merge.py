"""Combine extractions: keep the primary's values, fill its gaps from secondaries.

Used so that (a) Azure's invoice model gets GST splits / GSTINs / validity dates that only our
India-aware rules find, and (b) the fallback reuses whatever earlier tiers got right.
"""
from __future__ import annotations

from app.schemas import DocType, ExtractedDocument, Party

SCALARS = [
    "document_number", "document_date", "due_date", "valid_until", "po_number", "currency", "subtotal",
    "discount_total", "cgst", "sgst", "igst", "cess", "tax_total", "shipping", "round_off", "total",
    "payment_terms", "notes",
]


def _merge_party(primary: Party, other: Party) -> Party:
    data = primary.model_dump()
    for k, v in other.model_dump().items():
        if data.get(k) in (None, "") and v not in (None, ""):
            data[k] = v
    return Party(**data)


def merge(primary: ExtractedDocument, *others: ExtractedDocument | None) -> ExtractedDocument:
    out = primary.model_copy(deep=True)
    for other in filter(None, others):
        if out.document_type == DocType.OTHER and other.document_type != DocType.OTHER:
            out.document_type = other.document_type
        for f in SCALARS:
            if getattr(out, f) in (None, "") and getattr(other, f) not in (None, ""):
                setattr(out, f, getattr(other, f))
        out.vendor = _merge_party(out.vendor, other.vendor)
        out.buyer = _merge_party(out.buyer, other.buyer)
        if not out.line_items and other.line_items:
            out.line_items = [li.model_copy() for li in other.line_items]
        elif len(out.line_items) == len(other.line_items):
            for li, oli in zip(out.line_items, other.line_items):
                for k, v in oli.model_dump().items():
                    if getattr(li, k) is None and v is not None:
                        setattr(li, k, v)
    # Tax total from components when only components were found
    if out.tax_total is None and any(v is not None for v in (out.cgst, out.sgst, out.igst)):
        out.tax_total = round(sum(v or 0 for v in (out.cgst, out.sgst, out.igst, out.cess)), 2)
    return out
