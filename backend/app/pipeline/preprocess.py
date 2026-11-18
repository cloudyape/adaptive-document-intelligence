"""Cheap, local document analysis: text layer, OCR, page images, and complexity features.

Everything here runs without any paid API. The resulting `DocumentContext` is shared by the
identifier, the complexity classifier, and every extraction engine, so work is done once.
"""
from __future__ import annotations

import html
import io
import logging
import re
import shutil
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from PIL import Image, ImageStat

from app.config import get_settings

log = logging.getLogger(__name__)

PDF_MIME = "application/pdf"
IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/tiff", "image/webp", "image/bmp", "image/gif"}
TEXT_MIMES = {"text/plain", "text/html", "text/csv"}


def _tesseract_available() -> bool:
    settings = get_settings()
    try:
        import pytesseract

        if settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
            return Path(settings.tesseract_cmd).exists()
        return shutil.which("tesseract") is not None
    except ImportError:
        return False


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>|</li>|</h\d>|</table>", "\n", raw)
    raw = re.sub(r"(?i)</td>|</th>", "\t", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(raw)
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines() if line.strip())


@dataclass
class PageInfo:
    number: int
    text: str = ""
    text_source: str = "none"  # text_layer | ocr | none
    ocr_confidence: float | None = None  # 0-100 mean word confidence
    image_coverage: float = 0.0  # fraction of page area covered by embedded images
    table_count: int = 0
    tables: list[list[list[str | None]]] = field(default_factory=list)


@dataclass
class DocumentContext:
    path: Path
    mime_type: str
    filename: str
    pages: list[PageInfo] = field(default_factory=list)
    ocr_available: bool = False
    _page_images: list[Image.Image] | None = None

    # ---------- derived ----------
    @property
    def is_pdf(self) -> bool:
        return self.mime_type == PDF_MIME

    @property
    def is_image(self) -> bool:
        return self.mime_type in IMAGE_MIMES

    @property
    def is_text(self) -> bool:
        return self.mime_type in TEXT_MIMES

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text(self) -> str:
        return "\n\n".join(f"--- page {p.number} ---\n{p.text}" if self.page_count > 1 else p.text for p in self.pages)

    @property
    def has_usable_text(self) -> bool:
        return sum(len(p.text.strip()) for p in self.pages) >= 80

    @cached_property
    def raw_bytes(self) -> bytes:
        return self.path.read_bytes()

    def page_images(self, max_pages: int | None = None) -> list[Image.Image]:
        """Rendered page images (PDF) or the image itself. Rendered lazily and cached."""
        if self._page_images is None:
            dpi = get_settings().render_dpi
            if self.is_pdf:
                import pypdfium2 as pdfium

                pdf = pdfium.PdfDocument(str(self.path))
                limit = get_settings().max_pages_for_vision
                self._page_images = [
                    pdf[i].render(scale=dpi / 72).to_pil().convert("RGB") for i in range(min(len(pdf), limit))
                ]
            elif self.is_image:
                img = Image.open(self.path)
                frames = []
                try:
                    for i in range(getattr(img, "n_frames", 1)):
                        img.seek(i)
                        frames.append(img.convert("RGB"))
                except EOFError:
                    pass
                self._page_images = frames or [img.convert("RGB")]
            else:
                self._page_images = []
        return self._page_images[:max_pages] if max_pages else self._page_images

    def features(self) -> dict:
        ocr_confs = [p.ocr_confidence for p in self.pages if p.ocr_confidence is not None]
        text_pages = [p for p in self.pages if p.text_source == "text_layer"]
        chars = [len(p.text.strip()) for p in self.pages]
        return {
            "mime_type": self.mime_type,
            "page_count": self.page_count,
            "text_layer_pages": len(text_pages),
            "ocr_pages": sum(1 for p in self.pages if p.text_source == "ocr"),
            "empty_pages": sum(1 for c in chars if c < 20),
            "avg_chars_per_page": round(sum(chars) / max(1, len(chars)), 1),
            "mean_ocr_confidence": round(sum(ocr_confs) / len(ocr_confs), 1) if ocr_confs else None,
            "min_ocr_confidence": round(min(ocr_confs), 1) if ocr_confs else None,
            "max_image_coverage": round(max((p.image_coverage for p in self.pages), default=0.0), 2),
            "table_count": sum(p.table_count for p in self.pages),
            "ocr_available": self.ocr_available,
            "garbled_ratio": round(_garbled_ratio(self.text), 3),
        }


def _garbled_ratio(text: str) -> float:
    """Share of tokens that look like OCR noise (no vowels/digits, odd symbol mixes)."""
    tokens = [t for t in re.split(r"\s+", text) if len(t) >= 3]
    if not tokens:
        return 0.0
    bad = sum(1 for t in tokens if not re.search(r"[aeiouAEIOU0-9]", t) or len(re.findall(r"[^\w.,:/\-₹$%()#@&]", t)) >= 2)
    return bad / len(tokens)


def _ocr_image(img: Image.Image) -> tuple[str, float | None]:
    import pytesseract

    gray = img.convert("L")
    data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT)
    words, confs = [], []
    lines: dict[tuple, list[str]] = {}
    for i, word in enumerate(data["text"]):
        if not word.strip():
            continue
        conf = float(data["conf"][i])
        if conf >= 0:
            confs.append(conf)
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        words.append(word)
    text = "\n".join(" ".join(ws) for ws in lines.values())
    return text, (sum(confs) / len(confs) if confs else None)


def analyze(path: Path, mime_type: str, filename: str) -> DocumentContext:
    ctx = DocumentContext(path=path, mime_type=mime_type, filename=filename, ocr_available=_tesseract_available())

    if ctx.is_text:
        raw = path.read_text(errors="replace")
        ctx.pages = [PageInfo(1, html_to_text(raw) if mime_type == "text/html" else raw, "text_layer")]
        return ctx

    if ctx.is_pdf:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text(layout=False) or ""
                area = float(page.width * page.height) or 1.0
                img_area = sum(float(im["width"] * im["height"]) for im in page.images)
                try:
                    tables = page.extract_tables() or []
                except Exception:  # noqa: BLE001 - table detection is best-effort
                    tables = []
                ctx.pages.append(
                    PageInfo(
                        i, text, "text_layer" if len(text.strip()) >= 20 else "none", None,
                        min(1.0, img_area / area), len(tables), tables,
                    )
                )
        # OCR pages that have no text layer (scanned PDFs)
        if ctx.ocr_available and any(p.text_source == "none" for p in ctx.pages):
            images = ctx.page_images()
            for p in ctx.pages:
                if p.text_source == "none" and p.number - 1 < len(images):
                    p.text, p.ocr_confidence = _ocr_image(images[p.number - 1])
                    p.text_source = "ocr" if p.text.strip() else "none"
        return ctx

    if ctx.is_image:
        images = ctx.page_images()
        for i, img in enumerate(images, start=1):
            page = PageInfo(i, image_coverage=1.0)
            if ctx.ocr_available:
                page.text, page.ocr_confidence = _ocr_image(img)
                page.text_source = "ocr" if page.text.strip() else "none"
            ctx.pages.append(page)
        return ctx

    # Unknown binary (docx/xlsx etc. are not handled natively yet): treat as opaque
    ctx.pages = [PageInfo(1)]
    return ctx


def image_quality(img: Image.Image) -> dict:
    """Very rough scan-quality signals used when OCR is unavailable."""
    gray = img.convert("L")
    stat = ImageStat.Stat(gray)
    return {"width": img.width, "height": img.height, "contrast": round(stat.stddev[0], 1)}


def image_to_png_bytes(img: Image.Image, max_side: int = 1600) -> bytes:
    img = img.copy()
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
