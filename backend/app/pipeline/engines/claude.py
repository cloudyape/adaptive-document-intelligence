"""Claude-backed engines.

Tier 2  OcrLlmEngine      - OCR'd/text-layer text -> small model (Haiku). Images only if no text.
Tier 3  VisionEngine      - full document (native PDF or page images) -> mid model (Sonnet).
Tier 4  FallbackEngine    - full document + prior attempt's validation errors -> top model (Opus), high effort.
"""
from __future__ import annotations

import json

from app.config import get_settings
from app.pipeline import llm
from app.pipeline.engines.base import EngineResult, EngineUnavailable
from app.pipeline.preprocess import DocumentContext, image_to_png_bytes
from app.schemas import DocType, ExtractedDocument

SYSTEM = """You extract structured data from financial documents (invoices, quotations, proforma invoices, \
credit/debit notes, purchase orders) for an accounts-payable system. Documents come from many vendors with \
no fixed template; they may be printed, scanned, photographed, or handwritten.

Rules:
- Extract only what the document states. Use null for anything absent or illegible; never invent values.
- The vendor is the party issuing the document (seller/supplier); the buyer is the recipient (bill-to).
- Dates as ISO YYYY-MM-DD. Indian documents use day-first dates (03/04/2025 = 3 April 2025).
- Amounts as plain numbers without currency symbols or thousands separators. Indian grouping (1,23,456.00) = 123456.00.
- Line item `amount` is the line value as printed (normally quantity x unit price less line discount, before tax).
- `subtotal` is the pre-tax total; `tax_total` the sum of all taxes; `total` the grand total payable.
- Copy GSTIN exactly (15 characters). Record CGST/SGST/IGST amounts separately when shown.
- Handwritten corrections or strike-throughs: use the final, corrected value.
- `confidence` is your honest 0-1 estimate that every extracted field is correct."""


class _Extraction(ExtractedDocument):
    confidence: float = 0.5


def _content_for_document(ctx: DocumentContext, include_text: bool) -> list[dict]:
    settings = get_settings()
    blocks: list[dict] = []
    if ctx.is_pdf and len(ctx.raw_bytes) < 30 * 1024 * 1024:
        blocks.append(llm.pdf_block(ctx.raw_bytes))
    elif ctx.is_pdf or ctx.is_image:
        for img in ctx.page_images(max_pages=settings.max_pages_for_vision):
            blocks.append(llm.image_block(image_to_png_bytes(img)))
    if include_text and ctx.has_usable_text:
        blocks.append({"type": "text", "text": f"Locally extracted text (may contain OCR errors):\n{ctx.text[:60000]}"})
    if not blocks:
        raise EngineUnavailable(f"no content to send for {ctx.mime_type}")
    return blocks


def _run(model: str, content: list[dict], effort: str | None = None) -> EngineResult:
    parsed, usage = llm.parse(model=model, system=SYSTEM, content=content, output_model=_Extraction, effort=effort)
    data = parsed.model_dump()
    confidence = float(data.pop("confidence", 0.5) or 0.5)
    return EngineResult(
        document=ExtractedDocument.model_validate(data),
        engine_confidence=max(0.0, min(1.0, confidence)),
        cost_usd=usage.cost_usd,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        model=usage.model,
    )


def _hint(doc_type_hint: DocType | None) -> str:
    return f"The document was pre-identified as: {doc_type_hint.value}." if doc_type_hint else ""


class _ClaudeBase:
    def available(self) -> bool:
        s = get_settings()
        return s.llm_enabled and llm.available()


class OcrLlmEngine(_ClaudeBase):
    name = "ocr_llm"
    tier = 2

    def extract(self, ctx: DocumentContext, doc_type_hint: DocType | None = None) -> EngineResult:
        model = get_settings().model_ocr_llm
        if ctx.has_usable_text:
            content = [{"type": "text", "text": f"{_hint(doc_type_hint)}\nExtract this document. Its text:\n\n{ctx.text[:60000]}"}]
        else:  # no local OCR available: let the small model read page images
            content = _content_for_document(ctx, include_text=False)
            content.append({"type": "text", "text": f"{_hint(doc_type_hint)}\nExtract this document."})
        return _run(model, content)


class VisionEngine(_ClaudeBase):
    name = "claude_vision"
    tier = 3

    def extract(self, ctx: DocumentContext, doc_type_hint: DocType | None = None) -> EngineResult:
        content = _content_for_document(ctx, include_text=True)
        content.append({"type": "text", "text": f"{_hint(doc_type_hint)}\nExtract this document. Read the layout and "
                        "tables visually; use the extracted text only as a hint."})
        return _run(get_settings().model_document_ai, content)


class FallbackEngine(_ClaudeBase):
    name = "claude_fallback"
    tier = 4

    def __init__(self) -> None:
        self.previous_issues: list[str] = []
        self.previous_result: dict | None = None

    def extract(self, ctx: DocumentContext, doc_type_hint: DocType | None = None) -> EngineResult:
        content = _content_for_document(ctx, include_text=True)
        prompt = [_hint(doc_type_hint), "Extract this document with great care."]
        if self.previous_result:
            prompt.append(
                "An earlier extraction failed validation. Re-read the document from scratch and resolve these issues "
                "where the document allows (some may be genuine errors in the document itself; leave those as printed):\n- "
                + "\n- ".join(self.previous_issues)
                + f"\n\nEarlier extraction for reference:\n{json.dumps(self.previous_result)[:20000]}"
            )
        content.append({"type": "text", "text": "\n".join(p for p in prompt if p)})
        return _run(get_settings().model_fallback, content, effort="high")
