"""Tier 1 - lightweight extraction. Zero API cost.

Template-free heuristics over the text layer: label/value patterns for header fields and
pdfplumber's ruled tables (or numeric line patterns) for line items. Works well on clean digital
PDFs and HTML emails; anything it can't fully explain fails validation and escalates.
"""
from __future__ import annotations

import re

from app.pipeline.engines import us_quote
from app.pipeline.engines.base import EngineResult
from app.pipeline.kubota import enrich_models, normalize as normalize_dashes
from app.pipeline.normalize import DATE_TOKEN, GSTIN_RE, amounts_in, parse_amount, parse_date
from app.pipeline.preprocess import DocumentContext
from app.schemas import DocType, ExtractedDocument, LineItem, OptionItem, Party

TITLE_RE = re.compile(r"^(tax\s+invoice|invoice|quotation|quote|estimate|proforma.*|pro-forma.*|credit note|debit note|purchase order|bill of supply|original.*|duplicate.*)$", re.I)
BUYER_LABEL = re.compile(r"^\s*(?:bill(?:ed)?\s+to|buyer|customer|sold\s+to|ship(?:ped)?\s+to|to)\s*[:\-]\s*(?P<rest>.*)$|^\s*(?:bill(?:ed)?\s+to|buyer|customer|sold\s+to)\s*(?P<rest2>)$", re.I)

NUMBER_LABELS = {
    DocType.QUOTATION: r"(?:(?:quotation|quote|estimate)\s*(?:no\.?|number|#|ref\.?)|(?:quotation|quote)\s*(?=:)|sales\s+agreement|document\s*#)",
    DocType.INVOICE: r"(?:invoice|inv|bill)\s*(?:no\.?|number|#)",
    DocType.PROFORMA: r"(?:pro[\s-]?forma\s*(?:invoice)?|pi)\s*(?:no\.?|number|#)",
    DocType.CREDIT_NOTE: r"(?:credit|debit)\s*note\s*(?:no\.?|number|#)",
    DocType.PURCHASE_ORDER: r"(?:purchase\s*order|p\.?o\.?)\s*(?:no\.?|number|#)",
}
DOC_NO_VALUE = r"\s*[:\-.]?\s*#?\s*([A-Z0-9][A-Z0-9/\-_.]{1,40})"

TOTAL_PATTERNS = [
    ("total", r"(grand\s+total|total\s+amount\s+(payable|due)|amount\s+payable|net\s+payable|total\s+payable|invoice\s+total|balance\s+due|total\s+due)"),
    ("subtotal", r"(sub[\s-]?total|taxable\s+(value|amount)|total\s+before\s+tax|net\s+amount|total\s+taxable)"),
    ("cgst", r"\bcgst\b"),
    ("sgst", r"\b(sgst|utgst)\b"),
    ("igst", r"\bigst\b"),
    ("cess", r"\bcess\b"),
    ("tax_total", r"(total\s+tax|total\s+gst|tax\s+amount|\bvat\b|total\s+vat)"),
    ("discount_total", r"\b(discount|less)\b"),
    ("shipping", r"\b(shipping|freight|delivery\s+charges?|packing\s*(&|and)?\s*forwarding)\b"),
    ("round_off", r"\bround(ed)?[\s-]*off\b|\brounding\b"),
]
HEADER_SYNONYMS = {
    # Dealer-quote columns first: "Base Price" must not be taken as unit price, "Margin %" not as margin amount.
    "series": ["series", "product line", "range"],
    "model": ["model", "variant"],
    "options": ["options", "option", "accessories", "add-ons", "addons", "optional"],
    "base_price": ["base price", "basic price", "ex-showroom", "ex showroom", "list price"],
    "margin_percent": ["margin %", "margin%", "margin by %", "margin (%)", "margin pct", "margin percent"],
    "margin_amount": ["margin by", "margin amount", "margin amt", "dealer margin", "margin"],
    "description": ["description", "item", "particulars", "product", "service", "details", "goods"],
    "hsn_sac": ["hsn", "sac"],
    "quantity": ["qty", "quantity", "nos", "units"],
    "unit_price": ["rate", "unit price", "price", "unit cost", "mrp"],
    "unit": ["uom", "unit"],
    "discount": ["disc", "discount"],
    "tax_rate": ["gst %", "gst%", "tax %", "tax%", "gst rate", "tax rate", "vat %"],
    "amount": ["amount", "total", "value", "line total", "taxable"],
}


# ---------- dealer-quote fields: series, model, options, base price, margin, notes ----------
# Where a merged line's next label starts: only a keyword followed by its separator counts,
# so values like "5E Series" or "Valid for 30 days" aren't cut.
_LABEL_CUT = re.compile(
    r"\s{2,}|\s+(?=(?:quotation|quote|invoice|ref)\s*(?:no\.?|#|number)\b"
    r"|(?:date|dated|valid\s+(?:until|till|upto)|gstin|series|model|base\s+price|(?:dealer\s+)?margin)\s*[:\-])",
    re.I,
)
_STOP_LINE = re.compile(
    r"^\s*(sub[\s-]?total|total|grand|cgst|sgst|igst|tax|gst|discount|round|amount|payment|terms|remarks?|notes?|comments?|"
    r"series|model|base\s+price|basic\s+price|(dealer\s+)?margin|for\s+|authori[sz]ed|bank|delivery|warranty|"
    r"bill\s+to|ship\s+to|qty|description|#)\b",
    re.I,
)
_OPTIONS_HEADER = re.compile(
    r"^\s*(optional\s+(?:equipment|accessories|extras|fitments)|options?|accessories|add[\s-]?ons|extras)\b\s*(?:included|selected)?\s*[:\-]?\s*(.*)$",
    re.I,
)
_NOTES_HEADER = re.compile(r"^\s*(remarks?|notes?|comments?|special\s+(?:instructions|notes))\s*[:\-]\s*(.*)$", re.I)


def _cut(value: str) -> str:
    return _LABEL_CUT.split(value, maxsplit=1)[0].strip(" :-|,")


def _option_from(text: str) -> OptionItem | None:
    text = re.sub(r"^\s*(?:[-•*·]|\d{1,2}[.)])\s*", "", text).strip(" ,;")
    if not re.search(r"[A-Za-z]", text):
        return None
    m = re.match(r"^(.*?[A-Za-z].*?)[\s:@-]*(?:₹|rs\.?|inr)?\s*((?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)\s*(?:/-)?$", text, re.I)
    if m and not re.search(r"\d\s*%$", text):
        return OptionItem(name=m.group(1).strip(" :-"), price=parse_amount(m.group(2)))
    return OptionItem(name=text)


def parse_options_text(text: str) -> list[OptionItem]:
    """'Loader 1,20,000; Rotavator 65,000' -> named options with prices. Commas inside amounts are kept."""
    parts = re.split(r";|\n|\s\|\s|\s\+\s|,(?!\d)", text or "")
    return [o for p in parts if p.strip() and (o := _option_from(p))]


def dealer_fields(lines: list[str]) -> dict:
    """Labelled dealer-quote values from anywhere in the text."""
    out: dict = {"options": []}
    for i, ln in enumerate(lines):
        if out.get("series") is None and (m := re.search(r"\bseries\s*[:\-]\s*(.+)", ln, re.I)):
            out["series"] = _cut(m.group(1)) or None
        if out.get("model") is None and (m := re.search(r"\bmodel(?:\s*(?:no\.?|name|number|code))?\s*[:\-]\s*(.+)", ln, re.I)):
            out["model"] = _cut(m.group(1)) or None
        if out.get("base_price") is None and (m := re.search(
                r"\b(?:base|basic|ex[\s-]?showroom|list)\s+price\b[^\d]*((?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)", ln, re.I)):
            out["base_price"] = parse_amount(m.group(1))
        if re.search(r"\bmargin\b", ln, re.I) and not re.search(r"\bcost\s+margin\b|approximate|keep all", ln, re.I):
            pct_m = re.search(r"(\d+(?:\.\d+)?)\s*%", ln)
            if out.get("margin_percent") is None and pct_m and float(pct_m.group(1)) > 0:
                out["margin_percent"] = float(pct_m.group(1))
            elif out.get("margin_percent") is None and re.search(r"margin(?:\s+by)?\s*(?:%|percent|pct|percentage|\(%\))", ln, re.I):
                nums = [n for n in amounts_in(ln) if n > 0]
                out["margin_percent"] = nums[-1] if nums else None
            elif out.get("margin_amount") is None:
                nums = [n for n in amounts_in(ln) if n > 0]  # "Calculated Margin $0.00" is an empty field, not a margin
                if nums:
                    out["margin_amount"] = nums[-1]
        if not out["options"] and (m := _OPTIONS_HEADER.match(ln)):
            inline = m.group(2).strip()
            if inline:
                out["options"] = parse_options_text(inline)
            else:
                for nxt in lines[i + 1 : i + 16]:
                    if _STOP_LINE.match(nxt) or any(re.search(p, nxt.lower()) for _, p in TOTAL_PATTERNS[:2]):
                        break
                    if (opt := _option_from(nxt)) is not None:
                        out["options"].append(opt)
        if out.get("notes") is None and (m := _NOTES_HEADER.match(ln)):
            parts = [m.group(2).strip()] if m.group(2).strip() else []
            for nxt in lines[i + 1 : i + 5]:
                if _STOP_LINE.match(nxt) or re.match(r"^[A-Z][\w /&]{2,30}:\s", nxt):
                    break
                parts.append(nxt.strip())
            out["notes"] = " ".join(parts)[:1000] or None
    return out


def apply_dealer_fields(doc: ExtractedDocument, f: dict, warnings: list[str]) -> None:
    """Attach header-level dealer fields to the quote and, for single-unit quotes, to its line item."""
    doc.notes = doc.notes or f.get("notes")
    if f.get("margin_amount") is not None:
        doc.margin_amount = doc.margin_amount if doc.margin_amount is not None else f["margin_amount"]
    if f.get("margin_percent") is not None:
        doc.margin_percent = doc.margin_percent if doc.margin_percent is not None else f["margin_percent"]
    unit_fields = {k: f.get(k) for k in ("series", "model", "base_price", "margin_amount", "margin_percent")}
    has_unit = f.get("model") or f.get("series") or f.get("base_price") is not None or f["options"]
    if not has_unit:
        return
    if not doc.line_items:
        # Single-unit quote laid out as labels rather than a table: build its line from the labels.
        base = f.get("base_price")
        opts = f["options"]
        priced = base is not None and all(o.price is not None for o in opts)
        unit_price = round(base + sum(o.price for o in opts), 2) if priced else None
        doc.line_items = [LineItem(
            description=" ".join(filter(None, [f.get("series"), f.get("model")])) or "Quoted unit",
            quantity=1 if unit_price is not None else None, unit_price=unit_price, amount=unit_price,
            options=opts, **unit_fields,
        )]
        if unit_price is not None:
            warnings.append("line amount derived from base price + options")
    elif len(doc.line_items) == 1:
        li = doc.line_items[0]
        for k, v in unit_fields.items():
            if getattr(li, k) is None and v is not None:
                setattr(li, k, v)
        if not li.options and f["options"]:
            li.options = f["options"]


def clean_ocr_text(text: str) -> str:
    """Undo common OCR artefacts: table-border pipes/underscores and GSTINs split by a space."""
    text = re.sub(r"\s*[|¦]\s*", " ", text)
    text = re.sub(r"(?<=\s)_+(?=\s)", " ", text)
    text = re.sub(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z])\s?([1-9A-Z])\s?(Z)\s?([0-9A-Z])\b", r"\1\2\3\4", text)
    return re.sub(r"[ \t]{2,}", " ", text)


class RulesEngine:
    name = "rules"
    tier = 1

    def available(self) -> bool:
        return True

    def extract(self, ctx: DocumentContext, doc_type_hint: DocType | None = None) -> EngineResult:
        text = clean_ocr_text(normalize_dashes(ctx.text))
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("--- page")]
        doc = ExtractedDocument(document_type=doc_type_hint or DocType.OTHER)
        warnings: list[str] = []
        us = us_quote.is_us(text)
        dayfirst = not us  # US documents write dates month-first (02/11/2026 = Feb 11)

        doc.document_number = self._doc_number(text, doc.document_type)
        date_text = re.sub(r"(expir\w*|valid\w*|due|delivery|inquiry|ship(?:ping)?|order|printed|birth)\s*date", "_", text, flags=re.I)
        doc.document_date = self._labeled_date(
            date_text, r"(?:invoice|quotation|quote|bill|document|issue|effective)?\s*(?:date(?:d)?(?:\s+issued)?|issued)", dayfirst)
        doc.due_date = self._labeled_date(text, r"(?:due\s+date|payment\s+due|due\s+on|due\s+by)", dayfirst)
        doc.valid_until = self._labeled_date(
            text, r"(?:(?:quote\s+)?valid\s*(?:until|till|upto|up\s+to|through|thru)|validity|expiry\s+date|expiration\s+date|"
                  r"expires(?:\s+on)?|good\s+(?:through|until))", dayfirst)
        if doc.due_date == doc.document_date and not re.search(r"due", text, re.I):
            doc.due_date = None
        m = re.search(r"(?:p\.?o\.?|purchase\s+order)\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9/\-]{2,30})", text, re.I)
        doc.po_number = m.group(1) if m and doc.document_type != DocType.PURCHASE_ORDER else None
        doc.currency = self._currency(text)

        gstins = list(dict.fromkeys(GSTIN_RE.findall(text.upper())))
        doc.vendor = Party(name=self._vendor_name(lines), gstin=gstins[0] if gstins else None)
        doc.buyer = Party(name=self._buyer_name(lines), gstin=gstins[1] if len(gstins) > 1 else None)
        if gstins:
            # Assign GSTIN by proximity to the buyer label when possible
            buyer_idx = next((i for i, ln in enumerate(lines) if BUYER_LABEL.match(ln)), None)
            if buyer_idx is not None:
                before = [g for g in gstins if any(g in ln.upper() for ln in lines[:buyer_idx])]
                after = [g for g in gstins if any(g in ln.upper() for ln in lines[buyer_idx:])]
                if before:
                    doc.vendor.gstin = before[0]
                if after and after[0] != doc.vendor.gstin:
                    doc.buyer.gstin = after[0]
                elif not before and after:
                    doc.vendor.gstin, doc.buyer.gstin = None, after[0]
        m = re.search(r"\bPAN\s*(?:no\.?)?\s*[:\-]?\s*([A-Z]{5}\d{4}[A-Z])\b", text)
        doc.vendor.pan = m.group(1) if m else (doc.vendor.gstin[2:12] if doc.vendor.gstin else None)
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
        doc.vendor.email = m.group(0) if m else None

        self._totals(lines, doc)
        doc.payment_terms = self._payment_terms(lines)
        doc.line_items = self._items_from_tables(ctx) or self._items_from_lines(lines)
        if us:
            self._apply_us(doc, [ln.strip() for ln in text.splitlines() if ln.strip()], warnings, ctx.filename or "")
        apply_dealer_fields(doc, dealer_fields(lines), warnings)
        enrich_models(doc, text)
        if not doc.line_items:
            warnings.append("no line items found")

        found = sum(
            1 for v in [doc.document_number, doc.document_date, doc.vendor.name, doc.total, doc.line_items or None] if v
        )
        return EngineResult(document=doc, engine_confidence=round(found / 5 * 0.9, 2), warnings=warnings)

    @staticmethod
    def _apply_us(doc: ExtractedDocument, raw_lines: list[str], warnings: list[str], filename: str = "") -> None:
        parsed = us_quote.parse_items(raw_lines)
        if parsed.items and (not doc.line_items or not any(li.amount for li in doc.line_items)
                             or len([li for li in parsed.items if li.model]) >= len([li for li in doc.line_items if li.model])):
            doc.line_items = parsed.items
        totals = us_quote.parse_totals([ln for ln in raw_lines if not ln.startswith("--- page")])
        for fld in ("total", "subtotal", "tax_total"):
            if fld in totals:
                setattr(doc, fld, totals[fld])
        charges = us_quote.parse_charges(raw_lines)
        doc.other_charges = charges["fees"] or None
        doc.trade_in = charges["trade_in"] or None
        if charges["incentives"]:
            doc.discount_total = charges["incentives"]
        if parsed.notes:
            doc.notes = "; ".join(filter(None, [doc.notes, *parsed.notes]))
        doc.currency = doc.currency or "USD"
        dealer = us_quote.dealer_name(raw_lines, filename)
        doc.vendor.name = dealer  # None when the dealer name is only in a logo: escalate / review instead of guessing
        if doc.buyer.name and dealer and doc.buyer.name.lower() == dealer.lower():
            doc.buyer.name = None
        if not doc.vendor.gstin:  # US documents carry no GST; any CGST/SGST/IGST hits are stray matches
            doc.cgst = doc.sgst = doc.igst = doc.cess = None

    # ---------- header fields ----------
    @staticmethod
    def _doc_number(text: str, doc_type: DocType) -> str | None:
        order = [NUMBER_LABELS.get(doc_type)] if doc_type in NUMBER_LABELS else []
        order += [p for t, p in NUMBER_LABELS.items() if t != doc_type]
        order.append(r"(?:quote\s*id|document\s*(?:no\.?|number|#))")
        for label in filter(None, order):
            for m in re.finditer(label + DOC_NO_VALUE, text, re.I):
                value = m.group(1).rstrip(".-/")
                if not re.search(r"\d", value) or re.fullmatch(DATE_TOKEN, value) or re.fullmatch(r"[A-Z0-9]{1,4}-0", value, re.I):
                    continue  # no digits, a date, or an empty-template placeholder like "100-0"
                return value
        return None

    @staticmethod
    def _labeled_date(text: str, label: str, dayfirst: bool = True) -> str | None:
        weekday = r"(?:(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?,?\s+)?"
        for m in re.finditer(r"\b" + label + r"\s*[:\-]?\s*" + weekday + DATE_TOKEN, text, re.I):
            if (d := parse_date(m.group(1), dayfirst=dayfirst)) is not None:
                return d
        return None

    @staticmethod
    def _currency(text: str) -> str | None:
        if re.search(r"₹|\bINR\b|\bRs\.?\s?\d|\brupees\b", text, re.I):
            return "INR"
        for code, pat in [("USD", r"\$|\bUSD\b"), ("EUR", r"€|\bEUR\b"), ("GBP", r"£|\bGBP\b"), ("AED", r"\bAED\b")]:
            if re.search(pat, text):
                return code
        return "INR" if re.search(r"\bGSTIN\b", text, re.I) else None

    @staticmethod
    def _vendor_name(lines: list[str]) -> str | None:
        """Issuer name: first name-like line in the header block, above the buyer section."""
        cut = re.compile(r"\s*\b(tax\s+invoice|invoice|quotation|quote|estimate|pro[\s-]?forma|credit\s+note|debit\s+note|"
                         r"purchase\s+order|bill\s+of\s+supply|gstin|gst\s*no|pan|date|no\.|phone|tel|mob|email|original|duplicate)\b.*$", re.I)
        for ln in lines[:12]:
            if BUYER_LABEL.match(ln):
                break
            clean = cut.sub("", ln).strip(" :*-|,")
            if not clean or TITLE_RE.match(clean) or re.search(r"\d{3,}|@|^(hi|hello|dear)\b|please|page\s+\d", clean, re.I):
                continue
            if sum(1 for h in ("description", "qty", "rate", "amount", "item", "price") if h in clean.lower()) >= 2:
                continue
            if 2 < len(clean) < 80 and re.search(r"[A-Za-z]{2}", clean):
                return clean
        return None

    @staticmethod
    def _buyer_name(lines: list[str]) -> str | None:
        for i, ln in enumerate(lines):
            m = BUYER_LABEL.match(ln)
            if m and not re.match(r"^to(tal)?\b.*\d", ln, re.I):
                rest = (m.group("rest") or "").strip()
                if rest and not re.search(r"\d{4,}", rest):
                    return rest
                if i + 1 < len(lines):
                    return lines[i + 1].strip()
        return None

    @staticmethod
    def _payment_terms(lines: list[str]) -> str | None:
        for i, ln in enumerate(lines):
            m = re.search(r"(payment\s+terms?|terms\s+of\s+payment)\s*[:\-]?\s*(.*)", ln, re.I)
            if m:
                val = m.group(2).strip() or (lines[i + 1] if i + 1 < len(lines) else "")
                return val[:300] or None
            m = re.search(r"\b(net\s*\d{1,3}(\s*days)?|\d{1,3}\s*days\s+(credit|from\s+invoice)|advance\s+payment|due\s+on\s+receipt)\b", ln, re.I)
            if m:
                return m.group(0)
        return None

    @staticmethod
    def _totals(lines: list[str], doc: ExtractedDocument) -> None:
        for ln in reversed(lines):  # totals live at the bottom; bottom-most wins
            low = ln.lower()
            nums = amounts_in(re.sub(r"\d+(\.\d+)?\s*%", " ", ln))  # drop rate percentages
            if not nums:
                continue
            for field_name, pat in TOTAL_PATTERNS:
                if re.search(pat, low):
                    if field_name == "total" or getattr(doc, field_name) is None:
                        if field_name == "total" and doc.total is not None:
                            break
                        val = nums[-1]
                        if field_name == "discount_total":
                            val = abs(val)
                        setattr(doc, field_name, val)
                    break
            else:
                if doc.total is None and re.match(r"^\s*total\b", low) and not re.search(r"qty|quantity", low):
                    doc.total = nums[-1]
        if doc.subtotal is not None and doc.total is not None and doc.subtotal > doc.total and doc.discount_total is None:
            doc.subtotal, doc.total = doc.total, doc.subtotal
        if doc.tax_total is None and any(v is not None for v in (doc.cgst, doc.sgst, doc.igst)):
            doc.tax_total = round(sum(v or 0 for v in (doc.cgst, doc.sgst, doc.igst, doc.cess)), 2)

    # ---------- line items ----------
    def _items_from_tables(self, ctx: DocumentContext) -> list[LineItem]:
        items: list[LineItem] = []
        for page in ctx.pages:
            for table in page.tables:
                rows = [[(c or "").replace("\n", " ").strip() for c in row] for row in table if row]
                header_idx, cols = self._find_header(rows)
                if header_idx is None:
                    continue
                for row in rows[header_idx + 1 :]:
                    item = self._row_to_item(row, cols)
                    if item:
                        items.append(item)
        return items

    @staticmethod
    def _find_header(rows: list[list[str]]) -> tuple[int | None, dict[str, int]]:
        for idx, row in enumerate(rows[:6]):
            cols: dict[str, int] = {}
            for ci, cell in enumerate(row):
                low = cell.lower()
                for field_name, syns in HEADER_SYNONYMS.items():
                    if field_name not in cols and any(s in low for s in syns):
                        # "Total"/"Taxable" header only means amount if we don't already have it
                        cols[field_name] = ci
                        break
            has_item = "description" in cols or "model" in cols
            has_price = "amount" in cols or "base_price" in cols
            if has_item and has_price and ("quantity" in cols or "unit_price" in cols or "base_price" in cols):
                return idx, cols
        return None, {}

    @staticmethod
    def _row_to_item(row: list[str], cols: dict[str, int]) -> LineItem | None:
        def cell(name: str) -> str | None:
            i = cols.get(name)
            return row[i] if i is not None and i < len(row) and row[i] else None

        desc = cell("description") or " ".join(filter(None, [cell("series"), cell("model")])) or None
        amount = parse_amount(cell("amount"))
        base = parse_amount(cell("base_price"))
        if not desc or (amount is None and base is None) or re.match(r"^(sub\s*total|total|grand|cgst|sgst|igst|tax|discount|round)", desc, re.I):
            return None
        rate = cell("tax_rate")
        pct = cell("margin_percent")
        return LineItem(
            description=desc,
            series=cell("series"),
            model=cell("model"),
            options=parse_options_text(cell("options") or ""),
            base_price=base,
            margin_amount=parse_amount(cell("margin_amount")),
            margin_percent=parse_amount(pct.replace("%", "")) if pct else None,
            hsn_sac=cell("hsn_sac"),
            quantity=parse_amount(cell("quantity")),
            unit=cell("unit"),
            unit_price=parse_amount(cell("unit_price")),
            discount=parse_amount(cell("discount")),
            tax_rate=parse_amount(rate.replace("%", "")) if rate else None,
            amount=amount,
        )

    @staticmethod
    def _items_from_lines(lines: list[str]) -> list[LineItem]:
        """Untabled text: `<desc> ... <qty> <rate> <amount>` where qty * rate ~= amount."""
        items = []
        for ln in lines:
            if re.match(r"^\s*(sub\s*total|total|grand|cgst|sgst|igst|tax|discount|round|amount|balance)", ln, re.I):
                continue
            m = re.match(r"^\s*(?:\d{1,3}[.)]?\s+)?(.*?[A-Za-z].*?)\s+((?:[\d,]+(?:\.\d+)?%?\s+){1,4}[\d,]+(?:\.\d{1,2})?)\s*$", ln)
            if not m:
                continue
            nums = [parse_amount(x) for x in m.group(2).split() if not x.endswith("%")]
            nums = [n for n in nums if n is not None]
            if len(nums) < 3:
                continue
            qty, price, amount = nums[-3], nums[-2], nums[-1]
            # Accept the row when qty x price ~= amount; OCR misreads that break the arithmetic are kept
            # (if the row is otherwise well-formed) so validation flags them and the doc escalates.
            plausible = qty > 0 and (abs(qty * price - amount) <= max(1.0, 0.01 * amount) or (qty == int(qty) and qty < 10000 and price > 0))
            if plausible:
                desc = m.group(1).strip()
                raw_nums = [x for x in m.group(2).split() if not x.endswith("%")]
                hsn = re.search(r"\b(\d{4,8})\b", desc)
                hsn_code = hsn.group(1) if hsn else (raw_nums[0] if len(nums) >= 4 and re.fullmatch(r"\d{4,8}", raw_nums[0]) else None)
                items.append(LineItem(description=desc, quantity=qty, unit_price=price, amount=amount, hsn_sac=hsn_code))
        return items


MAX_VARIANT_PAGES = 3
OCR_VARIANT_BUDGET_S = 45.0


class LocalOcrRulesEngine(RulesEngine):
    """Tier 2 - rules over locally OCR'd text (Tesseract). Free; used for scans and photos.

    `preprocess.analyze` already OCR'd pages without a text layer. If that reading doesn't validate,
    this engine re-OCRs with other preprocessing variants (deskew, binarization) and keeps the
    candidate that validates best, filling its gaps from the others. CPU cost only.
    """

    name = "local_ocr"
    tier = 2

    def available(self) -> bool:
        from app.pipeline.preprocess import _tesseract_available

        return _tesseract_available()

    def extract(self, ctx, doc_type_hint=None):
        from dataclasses import replace

        from app.pipeline.engines.base import EngineUnavailable
        from app.pipeline.merge import merge
        from app.pipeline.preprocess import OCR_VARIANTS, ocr_variant_pages
        from app.pipeline.validation import validate

        if not ctx.has_usable_text:
            raise EngineUnavailable("local OCR found no readable text")

        def run(c, label):
            r = RulesEngine.extract(self, c, doc_type_hint)
            confs = [p.ocr_confidence for p in c.pages if p.ocr_confidence is not None]
            if confs:  # weight the rules engine's confidence by how well the page was read
                r.engine_confidence = round(r.engine_confidence * min(1.0, sum(confs) / len(confs) / 90), 2)
            r.warnings.append(f"ocr variant: {label}")
            return r, validate(r.document, r.engine_confidence)

        import time

        started = time.monotonic()
        candidates = [run(ctx, "lines_removed")]
        ocr_pages = sum(1 for p in ctx.pages if p.text_source == "ocr")
        # Extra variants cost one OCR pass per page each; long scans go to Azure instead of burning minutes here
        extra = list(OCR_VARIANTS)[1:] if 0 < ocr_pages <= MAX_VARIANT_PAGES else []
        for variant in extra:
            if any(v.passed for _, v in candidates):
                break  # stop at the first variant that validates
            if time.monotonic() - started > OCR_VARIANT_BUDGET_S:
                candidates[0][0].warnings.append("ocr variant time budget reached")
                break
            candidates.append(run(replace(ctx, pages=ocr_variant_pages(ctx, variant)), variant))

        candidates.sort(key=lambda c: c[1].score, reverse=True)
        best, best_v = candidates[0]
        if not best_v.passed and len(candidates) > 1:
            merged = merge(best.document, *(c[0].document for c in candidates[1:]))
            mv = validate(merged, best.engine_confidence)
            if mv.score > best_v.score:
                best.document = merged
                best.warnings.append("merged fields from other OCR variants")
        return best
