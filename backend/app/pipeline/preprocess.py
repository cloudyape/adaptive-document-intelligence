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
EXCEL_MIMES = {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel.sheet.macroenabled.12"}


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
        return self.mime_type in TEXT_MIMES or self.mime_type in EXCEL_MIMES

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
                limit = 50  # rendering is only for quality checks and OCR; cap memory use
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


def remove_ruling_lines(gray: Image.Image) -> Image.Image:
    """Erase long horizontal/vertical dark runs (table grid lines) so Tesseract reads cells as text.

    Without this, ruled invoice tables are mostly dropped or read as noise.
    """
    import numpy as np

    a = np.array(gray)
    dark = a < 128
    h, w = dark.shape

    def long_runs(mask: "np.ndarray", min_len: int) -> "np.ndarray":
        padded = np.pad(mask.astype(np.int8), ((0, 0), (1, 1)))
        diff = np.diff(padded, axis=1)
        res = np.zeros_like(mask)
        for r, c in zip(*np.nonzero(diff == 1)):
            end = c + np.argmax(diff[r, c + 1:] == -1) + 1
            if end - c >= min_len:
                res[r, c:end] = True
        return res

    lines = long_runs(dark, max(60, int(w * 0.08))) | long_runs(dark.T, max(40, int(h * 0.03))).T
    a = a.copy()
    a[lines] = 255
    return Image.fromarray(a)


def _rows_from_words(data: dict) -> str:
    """Rebuild visual text lines by vertical position.

    Tesseract's own block/line grouping reads multi-column layouts column by column, separating
    labels ("Grand Total") from their amounts. Grouping words by y-centre restores label/value rows.
    """
    words = [(data["top"][i] + data["height"][i] / 2, data["left"][i], data["height"][i], w)
             for i, w in enumerate(data["text"]) if w.strip()]
    if not words:
        return ""
    heights = sorted(h for _, _, h, _ in words)
    tol = heights[len(heights) // 2] * 0.6
    words.sort()
    rows: list[list[tuple[int, str]]] = []
    centres: list[float] = []  # running mean y of each row
    for y, x, _h, w in words:
        if centres and abs(y - centres[-1]) <= tol:
            rows[-1].append((x, w))
            centres[-1] += (y - centres[-1]) / len(rows[-1])
        else:
            rows.append([(x, w)])
            centres.append(y)
    return "\n".join(" ".join(w for _, w in sorted(r)) for r in rows)


def deskew(gray: Image.Image, max_angle: float = 3.0, step: float = 0.25) -> tuple[Image.Image, float]:
    """Rotate so text lines are horizontal: pick the angle that maximises row-profile variance."""
    import numpy as np

    small = gray.copy()
    small.thumbnail((900, 900))
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-max_angle, max_angle + step / 2, step):
        rotated = np.array(small.rotate(float(angle), fillcolor=255, resample=Image.Resampling.NEAREST))
        profile = (rotated < 128).sum(axis=1).astype(float)
        score = profile.var()
        if score > best_score:
            best_angle, best_score = float(angle), score
    if abs(best_angle) < step / 2:
        return gray, 0.0
    return gray.rotate(best_angle, fillcolor=255, resample=Image.Resampling.BICUBIC, expand=False), best_angle


def binarize_and_strip_lines(gray: Image.Image, threshold: int = 170, widen: int = 2) -> Image.Image:
    """Hard black/white, then erase grid lines plus a margin (removes anti-aliased line ghosts)."""
    import numpy as np

    bw = gray.point(lambda p: 255 if p > threshold else 0)
    a = np.array(bw)
    lines = (a < 128) & (np.array(remove_ruling_lines(bw)) == 255)
    grown = lines.copy()
    for d in range(1, widen + 1):
        for axis in (0, 1):
            grown |= np.roll(lines, d, axis=axis) | np.roll(lines, -d, axis=axis)
    a = a.copy()
    a[grown] = 255
    return Image.fromarray(a)


# Preprocessing variants for local OCR. Which one reads a given scan best varies, so the local OCR
# engine tries them in order and keeps the result that validates best (CPU only, still free).
OCR_VARIANTS = {
    "lines_removed": lambda g: remove_ruling_lines(g),
    "binarized": lambda g: binarize_and_strip_lines(g),
    "deskewed": lambda g: remove_ruling_lines(deskew(g)[0]),
    "deskewed_binarized": lambda g: binarize_and_strip_lines(deskew(g)[0]),
}


def _ocr_image(img: Image.Image, variant: str = "lines_removed") -> tuple[str, float | None]:
    import pytesseract

    clean = OCR_VARIANTS[variant](img.convert("L"))
    data = pytesseract.image_to_data(clean, config="--psm 3", output_type=pytesseract.Output.DICT)
    confs = [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
    return _rows_from_words(data), (sum(confs) / len(confs) if confs else None)


def ocr_variant_pages(ctx: "DocumentContext", variant: str) -> list[PageInfo]:
    """Re-OCR the pages that needed OCR with another preprocessing variant; text-layer pages are kept."""
    images = ctx.page_images()
    pages = []
    for p in ctx.pages:
        if p.text_source == "ocr" and p.number - 1 < len(images):
            text, conf = _ocr_image(images[p.number - 1], variant)
            pages.append(PageInfo(p.number, text, "ocr", conf, p.image_coverage, p.table_count, p.tables))
        else:
            pages.append(p)
    return pages


THIN_TEXT_CHARS = 600


def form_fields_text(page) -> str:
    """Values typed into a fillable PDF form, as text lines.

    Row-indexed fields (e.g. QUANTITYRow3, DESCRIPTIONRow3, AMOUNTRow3) become one line per row,
    in column order; other fields become "NAME: value" lines.
    """
    def dec(x) -> str:
        # PDF text strings are UTF-16 when they start with a byte-order mark, else PDFDocEncoding (~latin-1)
        if isinstance(x, bytes):
            return x.decode("utf-16") if x.startswith((b"\xfe\xff", b"\xff\xfe")) else x.decode("latin-1")
        return "" if x is None else str(x).strip("/")

    rows: dict[int, list[tuple[float, str]]] = {}
    singles: list[tuple[float, str]] = []
    try:
        annots = page.annots or []
    except Exception as exc:  # noqa: BLE001 - malformed/recursive annotation trees: skip form fields, keep the page
        log.warning("could not read form fields on page %s: %s", page.page_number, type(exc).__name__)
        return ""
    for a in annots:
        d = a.get("data") or {}
        name, value = dec(d.get("T")), dec(d.get("V")).strip()
        if not value or value.lower() in ("off", "no"):
            continue
        x = float(a.get("x0") or 0)
        m = re.match(r"^(.*?)[\s_]*row[\s_]*(\d+)$", name, re.I)
        if m:
            rows.setdefault(int(m.group(2)), []).append((x, value))
        else:
            singles.append((float(a.get("top") or 0), f"{name}: {value}"))
    lines = [t for _, t in sorted(singles)]
    lines += [" ".join(v for _, v in sorted(cells)) for _, cells in sorted(rows.items())]
    return "\n".join(lines)


def excel_text(path: Path) -> list[str]:
    """One text block per worksheet; each row's non-empty cells joined in order, numbers as money."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    sheets = []
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue  # hidden helper / lookup sheets
        rows = []
        for row in ws.iter_rows():
            cells = []
            for cell in row:
                c = getattr(cell, "value", None)
                if c is None or isinstance(c, bool) or (isinstance(c, str) and not c.strip()):
                    continue
                if isinstance(c, (int, float)):
                    fmt = (getattr(cell, "number_format", "") or "").lower()
                    money_fmt = any(t in fmt for t in ("$", "0.00", "#,##0", "accounting", "currency"))
                    # phone numbers / ids / horsepower are numeric cells too: only currency-formatted cells are money
                    cells.append(f"${c:,.2f}" if money_fmt and abs(c) < 1e8 else (f"{c:g}" if isinstance(c, float) else str(c)))
                elif hasattr(c, "strftime"):
                    cells.append(c.strftime("%m/%d/%Y"))
                else:
                    cells.append(str(c).strip())
            if cells:
                rows.append(" ".join(cells))
        text = "\n".join(rows)
        if rows and "formula=" not in text:
            sheets.append(text)
    return sheets


def analyze(path: Path, mime_type: str, filename: str) -> DocumentContext:
    ctx = DocumentContext(path=path, mime_type=mime_type, filename=filename, ocr_available=_tesseract_available())

    if mime_type in EXCEL_MIMES:
        ctx.pages = [PageInfo(i, t, "text_layer") for i, t in enumerate(excel_text(path), start=1)] or [PageInfo(1)]
        return ctx

    if ctx.is_text:
        raw = path.read_text(errors="replace")
        ctx.pages = [PageInfo(1, html_to_text(raw) if mime_type == "text/html" else raw, "text_layer")]
        return ctx

    if ctx.is_pdf:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text(layout=False) or ""
                form = form_fields_text(page)
                if form:  # fillable PDF: typed values live in form fields, not the page text
                    text = f"{text}\n{form}" if text.strip() else form
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
        # OCR pages with no text layer (scans), and image-covered pages with only a sliver of text
        # (a scan with a typed header or stamp overlaid) - their real content is in the image.
        for p in ctx.pages:
            if p.text_source == "text_layer" and len(p.text.strip()) < THIN_TEXT_CHARS and p.image_coverage > 0.5:
                p.text_source = "none"
        if ctx.ocr_available and any(p.text_source == "none" for p in ctx.pages):
            images = ctx.page_images()
            for p in ctx.pages:
                if p.text_source == "none" and p.number - 1 < len(images):
                    overlay = p.text.strip()
                    ocr_text, p.ocr_confidence = _ocr_image(images[p.number - 1])
                    p.text = ocr_text if not overlay else f"{ocr_text}\n{overlay}"
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
