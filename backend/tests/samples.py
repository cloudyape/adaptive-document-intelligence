"""Generate realistic sample documents for tests and demos (python -m tests.samples <outdir>)."""
from __future__ import annotations

import io
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

VENDOR_GSTIN = "27AAPFU0939F1ZV"  # valid checksum
BUYER_GSTIN = "29AABCT1332L1ZA"  # valid checksum (different state -> IGST)


def _table(c: canvas.Canvas, data: list[list[str]], x: float, y: float, widths: list[float]) -> float:
    t = Table(data, colWidths=widths)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9), ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
    ]))
    _w, h = t.wrapOn(c, 0, 0)
    t.drawOn(c, x, y - h)
    return y - h


def invoice_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 20 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, y, "Sharma Industrial Supplies Pvt Ltd")
    c.setFont("Helvetica", 9)
    for line in ["Plot 14, MIDC Bhosari, Pune 411026", f"GSTIN: {VENDOR_GSTIN}", "Email: accounts@sharmasupplies.in"]:
        y -= 12
        c.drawString(20 * mm, y, line)
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(w - 20 * mm, h - 20 * mm, "TAX INVOICE")
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 20 * mm, h - 20 * mm - 14, "Invoice No: SIS/2025-26/0142")
    c.drawRightString(w - 20 * mm, h - 20 * mm - 26, "Invoice Date: 14/08/2025")
    c.drawRightString(w - 20 * mm, h - 20 * mm - 38, "Due Date: 13/09/2025")
    y -= 24
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y, "Bill To:")
    c.setFont("Helvetica", 9)
    for line in ["Tutorlabs Learning Solutions LLP", "45 Residency Road, Bengaluru 560025", f"GSTIN: {BUYER_GSTIN}"]:
        y -= 12
        c.drawString(20 * mm, y, line)
    y -= 20
    rows = [
        ["#", "Description", "HSN", "Qty", "Rate", "Amount"],
        ["1", "Steel Office Chair - Ergonomic", "9401", "10", "4,500.00", "45,000.00"],
        ["2", "Whiteboard 6x4 ft", "9610", "4", "3,250.00", "13,000.00"],
        ["3", "Installation Charges", "9987", "1", "2,000.00", "2,000.00"],
    ]
    y = _table(c, rows, 20 * mm, y, [10 * mm, 75 * mm, 18 * mm, 15 * mm, 25 * mm, 27 * mm])
    y -= 18
    for label, val in [("Sub Total", "60,000.00"), ("IGST @ 18%", "10,800.00"), ("Grand Total", "70,800.00")]:
        c.setFont("Helvetica-Bold" if label == "Grand Total" else "Helvetica", 9)
        c.drawString(120 * mm, y, label)
        c.drawRightString(w - 20 * mm, y, val)
        y -= 13
    y -= 10
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, y, "Payment Terms: Net 30 days from invoice date")
    c.showPage()
    c.save()
    return buf.getvalue()


def quotation_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, h - 20 * mm, "Brightline IT Services")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, h - 20 * mm - 12, f"GSTIN: {VENDOR_GSTIN}")
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(w - 20 * mm, h - 20 * mm, "QUOTATION")
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 20 * mm, h - 20 * mm - 14, "Quotation No: BIT-Q-2291")
    c.drawRightString(w - 20 * mm, h - 20 * mm - 26, "Date: 02/09/2025")
    c.drawRightString(w - 20 * mm, h - 20 * mm - 38, "Valid Until: 02/10/2025")
    c.drawString(20 * mm, h - 50 * mm, "To: Tutorlabs Learning Solutions LLP")
    rows = [
        ["Item", "Qty", "Unit Price", "Amount"],
        ["Laptop i7 16GB", "5", "62,000.00", "3,10,000.00"],
        ["Annual Support Plan", "5", "4,000.00", "20,000.00"],
    ]
    y = _table(c, rows, 20 * mm, h - 60 * mm, [80 * mm, 20 * mm, 35 * mm, 35 * mm])
    y -= 18
    for label, val in [("Subtotal", "3,30,000.00"), ("CGST @ 9%", "29,700.00"), ("SGST @ 9%", "29,700.00"), ("Total Amount Payable", "3,89,400.00")]:
        c.drawString(110 * mm, y, label)
        c.drawRightString(w - 20 * mm, y, val)
        y -= 13
    c.drawString(20 * mm, y - 10, "Payment Terms: 50% advance, balance on delivery")
    c.showPage()
    c.save()
    return buf.getvalue()


def dealer_quote_pdf(with_margin: bool = True) -> bytes:
    """Single-unit dealer quotation laid out as labels (no line-item table), with options and margin."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 20 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, y, "Green Valley Tractors")
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(w - 20 * mm, y, "QUOTATION")
    c.setFont("Helvetica", 9)
    rows = [
        ("Authorised Dealer, NH-48, Nashik 422010", "Quotation No: GVT-Q-0915"),
        (f"GSTIN: {VENDOR_GSTIN}", "Date: 10/11/2026"),
        ("", "Valid Until: 10/12/2026"),
    ]
    for left, right in rows:
        y -= 13
        c.drawString(20 * mm, y, left)
        c.drawRightString(w - 20 * mm, y, right)
    y -= 22
    c.drawString(20 * mm, y, "To: Tutorlabs Learning Solutions LLP")
    y -= 24
    for label, val in [("Series:", "5E Series"), ("Model:", "5050E 4WD"), ("Base Price:", "8,50,000.00")]:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20 * mm, y, label)
        c.setFont("Helvetica", 10)
        c.drawString(55 * mm, y, val)
        y -= 15
    y -= 6
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, y, "Optional Equipment:")
    c.setFont("Helvetica", 10)
    for name, price in [("Front End Loader", "1,20,000.00"), ("Rotavator 7 ft", "65,000.00")]:
        y -= 14
        c.drawString(25 * mm, y, f"- {name}")
        c.drawRightString(w - 20 * mm, y, price)
    y -= 24
    for label, val in [("Sub Total", "10,35,000.00"), ("CGST @ 6%", "62,100.00"), ("SGST @ 6%", "62,100.00"),
                       ("Total Amount Payable", "11,59,200.00")]:
        c.drawString(110 * mm, y, label)
        c.drawRightString(w - 20 * mm, y, val)
        y -= 14
    y -= 10
    if with_margin:
        c.drawString(20 * mm, y, "Dealer Margin: 51,750.00")
        y -= 14
        c.drawString(20 * mm, y, "Margin %: 5.00")
    y -= 20
    c.drawString(20 * mm, y, "Payment Terms: 25% advance, balance before delivery")
    y -= 14
    c.drawString(20 * mm, y, "Remarks: Delivery within 3 weeks of order. Price includes registration")
    y -= 12
    c.drawString(20 * mm, y, "and first two free services.")
    c.showPage()
    c.save()
    return buf.getvalue()


def scanned_invoice_pdf() -> bytes:
    """Rasterize the digital invoice and wrap it in an image-only PDF (what a scanner produces)."""
    import pypdfium2 as pdfium
    from reportlab.lib.utils import ImageReader

    img = pdfium.PdfDocument(invoice_pdf())[0].render(scale=1.5).to_pil().convert("L").rotate(0.7, fillcolor=255)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawImage(ImageReader(img), 0, 0, *A4)
    c.showPage()
    c.save()
    return buf.getvalue()


def letter_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 11)
    y = A4[1] - 30 * mm
    for line in ["Dear team,", "", "Please find our updated holiday schedule for the coming quarter.",
                 "The office will remain closed on public holidays as per the calendar.", "", "Regards, HR"]:
        c.drawString(20 * mm, y, line)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


def _lines_page(c: canvas.Canvas, lines: list[str]) -> None:
    y = A4[1] - 20 * mm
    c.setFont("Helvetica", 10)
    for line in lines:
        c.drawString(20 * mm, y, line)
        y -= 14
    c.showPage()


def _us_quote(c: canvas.Canvas, dealer: str, city: str, number: str, customer: str, model: str, price: str,
              tax: str, total: str, *, two_pages: bool = False) -> None:
    head = [dealer, f"{city}", f"Quote #: {number}", "Date: 03/13/2026", f"Customer: {customer}"]
    body = ["Description Qty Price Amount", f"KUBOTA {model} 1 ${price} ${price}"]
    sums = [f"Subtotal: ${price}", f"Sales Tax: ${tax}", f"Quote Total: ${total}"]
    if two_pages:
        _lines_page(c, ["Page 1 of 2"] + head + body)
        _lines_page(c, ["Page 2 of 2", dealer, f"Quote #: {number}"] + sums + ["Customer signature: ________"])
    else:
        _lines_page(c, head + body + sums)


TERMS = ["Terms and Conditions:", "1. The customer named in the sales order on the first page agrees to the terms below."] + [
    f"{i}. Warranty, arbitration and limitation of liability provisions apply to all equipment sold hereunder and "
    "the purchaser acknowledges receipt of the manufacturer limited warranty statement." for i in range(2, 14)]


def merged_quotes_pdf() -> bytes:
    """Several dealer quotes merged into one PDF, as dealers send them: a 2-page quote, a quote with a terms
    page, the first quote again (duplicate), a blank separator, and an empty quote form (fails extraction)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _us_quote(c, "Pinecrest Kubota", "Springfield, MS 38601", "PK-1001", "Jane Buyer", "SVL75-3HFWC",
              "82,500.00", "5,775.00", "88,275.00", two_pages=True)
    _us_quote(c, "Riverbend Tractor & Equipment", "Riverbend, VA 24400", "RB-2002", "Sam Buyer", "KX040-4R3T",
              "67,600.00", "3,042.00", "70,642.00")
    _lines_page(c, TERMS)
    _us_quote(c, "Pinecrest Kubota", "Springfield, MS 38601", "PK-1001", "Jane Buyer", "SVL75-3HFWC",
              "82,500.00", "5,775.00", "88,275.00", two_pages=True)
    _lines_page(c, [])
    _lines_page(c, ["Acme Equipment Sales", "Tulsa, OK 74101", "SALES QUOTATION", "Customer:", "Date:",
                    "Description Qty Price Amount", "Subtotal:", "Total: $0.00"])
    c.save()
    return buf.getvalue()


INVOICE_EMAIL_HTML = f"""<html><body><p>Hi, please find the invoice details below.</p>
<h3>Quick Prints &amp; Co</h3><p>GSTIN: {VENDOR_GSTIN}</p>
<p>Tax Invoice No: QP-7781<br>Invoice Date: 05/09/2025</p>
<p>Bill To: Tutorlabs Learning Solutions LLP</p>
<table><tr><th>Description</th><th>Qty</th><th>Rate</th><th>Amount</th></tr>
<tr><td>Brochure printing A4</td><td>500</td><td>12.00</td><td>6,000.00</td></tr>
<tr><td>Visiting cards</td><td>10</td><td>250.00</td><td>2,500.00</td></tr></table>
<p>Sub Total: 8,500.00<br>IGST @ 18%: 1,530.00<br>Grand Total: Rs. 10,030.00</p>
<p>Payment terms: Due on receipt</p></body></html>"""


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    out.mkdir(parents=True, exist_ok=True)
    (out / "invoice_digital.pdf").write_bytes(invoice_pdf())
    (out / "quotation_digital.pdf").write_bytes(quotation_pdf())
    (out / "invoice_scanned.pdf").write_bytes(scanned_invoice_pdf())
    (out / "letter.pdf").write_bytes(letter_pdf())
    (out / "invoice_email.html").write_text(INVOICE_EMAIL_HTML)
    (out / "dealer_quote.pdf").write_bytes(dealer_quote_pdf())
    (out / "merged_quotes.pdf").write_bytes(merged_quotes_pdf())
    print(f"wrote samples to {out}")
