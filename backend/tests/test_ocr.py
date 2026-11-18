"""OCR post-processing that doesn't need the Tesseract binary."""
from PIL import Image, ImageDraw

from app.pipeline.engines.rules import RulesEngine, clean_ocr_text
from app.pipeline.preprocess import DocumentContext, PageInfo, _rows_from_words, remove_ruling_lines
from app.pipeline.validation import validate
from app.schemas import DocType


def test_rows_rebuilt_across_columns():
    # Tesseract-style output where label and amount are separate blocks on the same visual line
    data = {"text": ["Grand", "Total", "70,800.00", "Sub", "Total", "60,000.00"],
            "top": [500, 500, 502, 450, 451, 449], "left": [100, 160, 600, 100, 150, 600], "height": [20] * 6}
    assert _rows_from_words(data).splitlines() == ["Sub Total 60,000.00", "Grand Total 70,800.00"]


def test_ruling_lines_removed_but_text_kept():
    img = Image.new("L", (1000, 400), 255)
    d = ImageDraw.Draw(img)
    d.line([(20, 100), (980, 100)], fill=0, width=2)  # horizontal grid line
    d.line([(500, 20), (500, 380)], fill=0, width=2)  # vertical grid line
    d.rectangle([(100, 200), (108, 220)], fill=0)  # a glyph-sized blob
    out = remove_ruling_lines(img)
    assert out.getpixel((300, 100)) == 255 and out.getpixel((500, 300)) == 255
    assert out.getpixel((104, 210)) == 0


def test_clean_ocr_text():
    assert clean_ocr_text("GSTIN: 27AAPFU0939F 1ZV") == "GSTIN: 27AAPFU0939F1ZV"
    assert clean_ocr_text("2 | Whiteboard 6x4 ft 9610 4 3,250.00 | 13,000.00") == "2 Whiteboard 6x4 ft 9610 4 3,250.00 13,000.00"


OCR_TEXT = """Sharma Industrial Supplies Pvt Ltd TAX INVOICE
Plot 14, MIDC Bhosari, Pune 411026 invoice No. SIS/2025-26/0142
GSTIN: 27AAPFU0939F 1ZV Invoice Date: 14/08/2025
Bill To:
Tutorlabs Learning Solutions LLP
GSTIN: 29AABCT1332L1ZA
# | Description HSN Qty _ Rate | Amount
1 Steel Office Chair - Ergonomic 9401 10 4,500.00 | 45,000.00
2 | Whiteboard 6x4 ft 9610 4 {rate} | 13,000.00
3 | Installation Charges | 9987 1 2,000.00 | 2,000.00
Sub Total 60,000.00
IGST @ 18% 10,800.00
Grand Total 70,800.00"""


def _ctx(text):
    return DocumentContext(path=None, mime_type="application/pdf", filename="scan.pdf", pages=[PageInfo(1, text, "ocr", 92.0)])


def test_rules_on_ocr_text_extracts_table():
    d = RulesEngine().extract(_ctx(OCR_TEXT.format(rate="3,250.00")), DocType.INVOICE).document
    assert [li.amount for li in d.line_items] == [45000, 13000, 2000]
    assert [li.hsn_sac for li in d.line_items] == ["9401", "9610", "9987"]
    assert d.vendor.gstin == "27AAPFU0939F1ZV" and d.total == 70800
    assert validate(d, 0.9).passed


def test_ocr_misread_is_not_auto_accepted():
    d = RulesEngine().extract(_ctx(OCR_TEXT.format(rate="3,260.00")), DocType.INVOICE).document
    rep = validate(d, 0.95)
    assert any(i.code == "line_math" for i in rep.issues) and not rep.passed


def test_deskew_recovers_small_rotation():
    from app.pipeline.preprocess import deskew

    img = Image.new("L", (1200, 900), 255)
    d = ImageDraw.Draw(img)
    for y in range(100, 800, 40):  # text-like horizontal bars
        d.rectangle([(100, y), (1100, y + 12)], fill=0)
    _, angle = deskew(img.rotate(1.5, fillcolor=255))
    assert abs(angle + 1.5) <= 0.25


def test_rows_tolerate_drift_across_line():
    # a slightly tilted line: y drifts 14px from left to right; the next line is 45px below
    data = {"text": ["Industrial", "Pvt", "Ltd", "TAX", "INVOICE", "Sharma", "Next", "line"],
            "top": [104, 106, 108, 114, 116, 100, 150, 152], "left": [260, 420, 500, 900, 980, 100, 100, 200], "height": [24] * 8}
    assert _rows_from_words(data).splitlines() == ["Sharma Industrial Pvt Ltd TAX INVOICE", "Next line"]
