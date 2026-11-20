"""Page-level PDF helpers used to split a merged PDF (a "packet" of several quotes) into one PDF per quote."""
from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

SPLIT_OCR_DPI = 200  # enough to read headers, quote numbers and totals; full extraction re-OCRs each quote
SPLIT_OCR_WORKERS = 4


def page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    return len(pdfium.PdfDocument(str(path)))


def page_texts(path: Path, ocr: bool = True) -> list[tuple[str, str]]:
    """(text, source) per page: the text layer, or OCR for pages without one. Source is text_layer | ocr | none."""
    import pdfplumber

    from app.pipeline.preprocess import THIN_TEXT_CHARS, _ocr_image, _tesseract_available, form_fields_text

    out: list[tuple[str, str]] = []
    need_ocr: list[int] = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text(layout=False) or ""
            form = form_fields_text(page)
            if form:
                text = f"{text}\n{form}" if text.strip() else form
            area = float(page.width * page.height) or 1.0
            covered = sum(float(im["width"] * im["height"]) for im in page.images) / area
            out.append((text, "text_layer" if len(text.strip()) >= 20 else "none"))
            if len(text.strip()) < 20 or (len(text.strip()) < THIN_TEXT_CHARS and covered > 0.5):
                need_ocr.append(i)
    if not (ocr and need_ocr and _tesseract_available()):
        return out

    def run(i: int, img) -> tuple[int, str]:
        try:
            return i, _ocr_image(img)[0]
        except Exception as exc:  # noqa: BLE001 - one unreadable page must not stop the split
            log.warning("OCR failed on page %s of %s: %s", i + 1, path.name, exc)
            return i, ""

    import pypdfium2 as pdfium

    # pdfium is not thread-safe: render here, one page at a time; Tesseract runs as subprocesses in parallel.
    pdf = pdfium.PdfDocument(str(path))
    with ThreadPoolExecutor(SPLIT_OCR_WORKERS) as ex:
        futures = []
        for i in need_ocr:
            img = pdf[i].render(scale=SPLIT_OCR_DPI / 72).to_pil().convert("RGB")
            futures.append(ex.submit(run, i, img))
            if len(futures) - sum(f.done() for f in futures) > SPLIT_OCR_WORKERS * 2:
                futures[-SPLIT_OCR_WORKERS * 2].result()  # bound the number of rendered pages held in memory
        for f in futures:
            i, text = f.result()
            overlay = out[i][0].strip()
            merged = f"{text}\n{overlay}" if overlay else text
            out[i] = (merged, "ocr" if merged.strip() else "none")
    return out


def extract_pages(path: Path, start: int, end: int) -> bytes:
    """A new PDF holding pages start..end (1-based, inclusive) of `path`."""
    import pypdfium2 as pdfium

    src = pdfium.PdfDocument(str(path))
    dst = pdfium.PdfDocument.new()
    dst.import_pages(src, list(range(start - 1, end)))
    buf = io.BytesIO()
    dst.save(buf)
    return buf.getvalue()
