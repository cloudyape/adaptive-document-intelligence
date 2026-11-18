"""Extraction-complexity classifier: EASY / MEDIUM / DIFFICULT.

A transparent, points-based scorer over features measured locally in `preprocess`. Every point
added comes with a human-readable reason, stored on the document so routing is explainable.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.pipeline.preprocess import DocumentContext, image_quality
from app.schemas import Complexity

MEDIUM_AT = 3.0
DIFFICULT_AT = 6.0


@dataclass
class ComplexityResult:
    level: Complexity
    score: float
    reasons: list[str]


def classify(ctx: DocumentContext) -> ComplexityResult:
    f = ctx.features()
    score = 0.0
    reasons: list[str] = []

    def add(points: float, why: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"+{points:g} {why}")

    # --- Source format ---
    if ctx.is_text:
        reasons.append("+0 plain text / HTML email body")
    elif ctx.is_image:
        add(3, "photo or scanned image (no text layer)")
    elif ctx.is_pdf:
        if f["text_layer_pages"] == f["page_count"]:
            reasons.append("+0 digital PDF with full text layer")
        elif f["text_layer_pages"] == 0:
            add(3, "scanned PDF (no text layer)")
        else:
            add(2, "mixed PDF: some pages scanned")
    else:
        add(6, f"unsupported format {ctx.mime_type}: needs a document-AI engine")

    # --- Text quality ---
    if f["mean_ocr_confidence"] is not None:
        conf = f["mean_ocr_confidence"]
        if conf < 60:
            add(4, f"very low OCR confidence ({conf}) - likely handwritten or poor scan")
        elif conf < 75:
            add(2, f"low OCR confidence ({conf})")
        elif conf < 85:
            add(1, f"moderate OCR confidence ({conf})")
    elif (ctx.is_image or f["text_layer_pages"] < f["page_count"]) and not ctx.ocr_available:
        imgs = ctx.page_images(max_pages=1)
        if imgs:
            q = image_quality(imgs[0])
            if q["contrast"] < 40 or min(q["width"], q["height"]) < 900:
                add(2, f"low-quality image (contrast {q['contrast']}, {q['width']}x{q['height']}) and no local OCR")
            else:
                add(1, "image content and no local OCR to measure quality")

    if f["garbled_ratio"] > 0.25:
        add(2, f"garbled text ({f['garbled_ratio']:.0%} noisy tokens)")
    elif f["garbled_ratio"] > 0.12:
        add(1, f"some noisy text ({f['garbled_ratio']:.0%})")

    if f["empty_pages"] and ctx.ocr_available and f["page_count"] > 0:
        add(1, f"{f['empty_pages']} page(s) with no readable text")

    # --- Layout / size ---
    pages = f["page_count"]
    if pages > 10:
        add(2, f"long document ({pages} pages)")
    elif pages > 3:
        add(1, f"multi-page document ({pages} pages)")

    if ctx.is_pdf and f["text_layer_pages"] and f["table_count"] == 0 and f["avg_chars_per_page"] > 400:
        add(1, "no ruled tables detected: line items need layout understanding")
    if f["table_count"] > 4:
        add(1, f"many tables ({f['table_count']})")
    if ctx.is_pdf and f["text_layer_pages"] and f["max_image_coverage"] > 0.5:
        add(1, "large embedded images on a text page (stamps/handwritten notes possible)")

    level = Complexity.EASY if score < MEDIUM_AT else Complexity.MEDIUM if score < DIFFICULT_AT else Complexity.DIFFICULT
    return ComplexityResult(level, score, reasons)
