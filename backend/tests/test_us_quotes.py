"""US dealer-quote layouts. Fixtures are synthetic (fake dealers/customers/serials) but mirror real layouts."""
import pytest

from app.pipeline.engines.rules import RulesEngine
from app.pipeline.engines.us_quote import dealer_name, money_values, parse_charges, parse_items, parse_totals
from app.pipeline.preprocess import DocumentContext, PageInfo
from app.pipeline.derive import derive
from app.pipeline.validation import validate
from app.schemas import DocType


def run(text: str, filename: str = "quote.pdf", hint=DocType.QUOTATION):
    ctx = DocumentContext(path=None, mime_type="application/pdf", filename=filename, pages=[PageInfo(1, text, "text_layer")])
    r = RulesEngine().extract(ctx, hint)
    derive(r.document)
    return r.document, validate(r.document, r.engine_confidence)


SALES_AGREEMENT = """Pinecrest Kubota
Sales Agreement: #077QA00012XY
Date Issued: Mar 13, 2026, 7:13 PM
Expiration Date: Mar 20, 2026
Purchaser Info Prepared By
JANE Q CUSTOMER Sam Seller
12 ELM ST 400 Hwy 9 East
SPRINGFIELD, MS 38601 Springfield, MS
jane@example.org sseller@pinecrestkubota.com
EQUIPMENT SUMMARY ( 2 )
Equipment Price
KUBOTA SVL75-3HFWVRCC 11111 $82,500.00
2026 ACME HD Drum Mulcher $38,000.00
Total Selling Price: $120,500.00
QUOTE SUMMARY
Total Selling Price $120,500.00
Net Trade Offer $0.00
Net Taxes Due $8,435.00
Finance Subtotal: $128,935.00"""


def test_sales_agreement_layout():
    d, rep = run(SALES_AGREEMENT, "JANE CUSTOMER TARGET FUNDS.pdf")
    li = d.line_items[0]
    assert (d.vendor.name, d.document_number) == ("Pinecrest Kubota", "077QA00012XY")
    assert (d.document_date, d.valid_until, d.currency) == ("2026-03-13", "2026-03-20", "USD")
    assert (li.model, li.series, li.base_price) == ("SVL75-3HFWVRCC", "S Series", 82500)
    assert [(o.name, o.price) for o in li.options] == [("ACME HD Drum Mulcher", 38000)]
    assert (li.amount, d.subtotal, d.tax_total, d.total) == (120500, 120500, 8435, 128935)
    assert rep.passed, rep.messages()


WHOLEGOOD = """Riverbend Tractor & Equipment, LLC
100 Main Hwy
Riverbend, VA 24400
www.riverbendtractor.com
Invoice # RB1-0 PO #: Salesman: PAT SELLER
Date 3/18/2026 Document #: RB1-12345 Ship Via:
SAM BUYER (90001) Ship To:
Item Number Sales Extended Back Drop
Description Qty Price Amount Disc Subtotal Tax Amount Order Ship
(100001)
KUBOTA U17-5 EXCAVATOR
W/VARIABLE
1.00 $35,000.00 $35,000.00 $35,000.00 $1,850.00 $36,850.00
TRACKS/CANOPY
(SN:KBCAZ00000000001)
K7966A COUPLER
K7911 THUMB
(100002)
ACME 6X12 DUMP TRAILER
1.00 $9,000.00 $9,000.00 $9,000.00 $9,000.00
Z-CVR Title Tax
1.00 $475.00 $475.00 $475.00 $475.00
Processing Fee
1.00 $300.00 $300.00 $300.00 $15.90 $315.90
--- page 2 ---
Riverbend Tractor & Equipment, LLC
100 Main Hwy
Summary of Charges
$44,775.00 Subtotal
$1,865.90 Sales Tax
$46,640.90 Total
$2,500.00 Deposit Received
$44,140.90 Net Amount Due"""


def test_wholegood_sales_order_layout():
    d, rep = run(WHOLEGOOD, "SAM BUYER U17.pdf")
    li = d.line_items[0]
    assert d.vendor.name == "Riverbend Tractor & Equipment, LLC"
    assert d.document_number == "RB1-12345"  # "Invoice # RB1-0" is an empty-template placeholder
    assert d.document_date == "2026-03-18"  # month-first
    assert (li.model, li.series, li.base_price) == ("U17-5", "U Series", 35000)
    names = [o.name for o in li.options]
    assert "K7966A COUPLER" in names and "K7911 THUMB" in names and not any("Riverbend" in n for n in names)
    assert ("ACME 6X12 DUMP TRAILER", 9000) in [(o.name, o.price) for o in li.options]
    assert d.other_charges == 775 and (d.subtotal, d.tax_total, d.total) == (44775, 1865.90, 46640.90)
    assert rep.passed, rep.messages()


KUBOTA_TOOL = """QuoteNumber:123456
EffectiveDate:03/13/2026
ValidThrough:03/31/2026
Ship To Kubota Dealer Bill To
JaneBuyer KubotaofSpringfield SamSeller JaneBuyer
Description Manufacturer Model# Qty MSRP PriceEach Total
EXCAVATORW/RUBBERTRACKS,CAB Kubota KX040-4R3T 1 $80,808.00 $67,600.00 $67,600.00
24"QATRENCHINGBUCKET Kubota K7875A 1 $2,087.00 $2,000.00 $2,000.00
HYDTHUMBKIT Kubota K7910A 1 $4,270.00 $4,000.00 $4,000.00
Cash Details
EquipmentTotal** $73,600.00
AdditionalCharges $0.00
CashSub-total $73,600.00
K Series KX040-4R3T"""


def test_kubota_quote_tool_layout():
    d, rep = run(KUBOTA_TOOL)
    li = d.line_items[0]
    assert (d.vendor.name, d.document_number, d.document_date, d.valid_until) == ("Kubota of Springfield", "123456", "2026-03-13", "2026-03-31")
    assert (li.model, li.series, li.msrp, li.base_price, li.amount) == ("KX040-4R3T", "K Series", 80808, 67600, 73600)
    assert [(o.name.split()[-1], o.price) for o in li.options] == [("K7875A", 2000), ("K7910A", 4000)]
    assert (d.subtotal, d.total) == (73600, 73600) and rep.passed, rep.messages()


STOCK_LINES = """SAME AS BELOW 03 - 122 CHARTER
02/23/2026 8:31:27
JOHN AND JANE BUYER
105 LAZY RD
SPRINGFIELD TN 37360
Description ** Q U O T E ** EXPIRY DATE: 03/19/2026 Amount
Stock #: 021922 Serial #: KBCZ053CVS0000001 75095.00
New 2025 KU SVL75
New 2025 KUBOTA SVL75-3HFWC SKID STEER
INCLUDES 2 YEAR EXTENDED WARRANTY
Stock #: 021428 Serial #: 2381247 2200.00
New 2025 LP AP-HD74LT
New 2025 LAND PRIDE AP-HD74LT 74" TOOTH BUCKET
Miscellaneous Charges/Credits
DOCUMENT FEES Qty: 1 Price: 175.00 175.00
Subtotal: 77470.00
Quote Total: 77470.00"""


def test_stock_line_layout_without_dollar_signs_and_logo_only_dealer():
    d, rep = run(STOCK_LINES, "Q99999.pdf")
    li = d.line_items[0]
    assert (li.model, li.base_price) == ("SVL75-3HFWC", 75095)
    assert li.options[0].name == "INCLUDES 2 YEAR EXTENDED WARRANTY"
    assert li.options[-1].price == 2200 and "AP-HD74LT" in li.options[-1].name
    assert d.valid_until == "2026-03-19" and d.document_date != "2026-03-19"  # expiry is not the quote date
    assert (d.subtotal, d.total, d.other_charges) == (77470, 77470, 175)
    # dealer name only in the logo; the customer must not be taken as the dealer
    assert d.vendor.name is None and not rep.passed


def test_money_parsing_edge_cases():
    assert money_values("SUBTOTAL: $ 7 2,683.00") == [72683.0]  # OCR-spaced digits
    assert money_values("TAX: 3.000% $ 2 ,180.49") == [2180.49]
    assert money_values("TradeIn-T770 ($12,038.00)") == [-12038.0]
    assert money_values("Money Down: $33k") == []  # a note, not an amount


def test_charges_trade_in_and_incentive_notes():
    lines = ["Equipment Total $113,000.00", "TradeIn-T770 ($12,038.00)", "Cash Sub-total $100,962.00",
             "Doc Fee $252.50", "UCC $10.00", "Doc Fee $252.50",  # summary repeated on page 2: counted once
             "Cash Incentive $3500.00 not included in price"]
    ch = parse_charges(lines)
    assert ch == {"fees": 262.5, "trade_in": 12038.0, "incentives": 0.0}
    assert parse_totals(lines) == {"total": 100962.0, "subtotal": 113000.0}


def test_dealer_name_rules():
    assert dealer_name(["1287 Hwy 63 S, Lucedale, MS 39452", "QUOTED BY: SAM SELLER", "FOR: JANE BUYER"]) is None
    assert dealer_name(["22595 Highway H", "www.acme-power.com", "SPRINGFIELD RENTAL AND SALES LLC"]) == "Acme Power"
    assert dealer_name(["JANE BUYER LAND MANAGEMENT LLC"], "JANE BUYER.pdf") is None  # customer named in file name
    assert dealer_name(["KUBOTA KX080-5R3A 1 $131,500.00"]) is None  # a price line is not a dealer


@pytest.mark.parametrize("line,expected", [("02/11/2026", "2026-02-11"), ("3/1/2026", "2026-03-01")])
def test_us_dates_are_month_first(line, expected):
    d, _ = run(f"Acme Kubota\nSPRINGFIELD, TN 37064\nDate: {line}\nKubota KX040-5R3AP $72,304.00")
    assert d.document_date == expected
