"""US equipment-dealer quotes (Kubota and allied): money lines, machines with options, US totals.

Dealer systems differ (Kubota quote tool, sales agreements, wholegood sales orders, Word/Excel forms),
but share a vocabulary this parser relies on rather than any one template:

* a priced line is text plus one or more money amounts ($82,500.00); quantity sits just before the
  first amount ("1", "1.00", "1.0000 Y");
* a machine line carries a Kubota model number; unpriced lines under it are its options/features;
  separately priced attachments (buckets, forks, mulchers) become priced options of that machine;
* fees, trade-ins, incentives and finance tables are separated from equipment;
* totals use US labels (Total Selling Price, Invoice Total, Sales Tax, Finance Subtotal ...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.pipeline.kubota import machine_models, normalize as normalize_dashes
from app.pipeline.normalize import parse_amount
from app.schemas import LineItem, OptionItem

US_STATE_ZIP = re.compile(r"\b(A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b,?\s+\d{5}\b")

# $82,500.00  ($12,038.00)  $ 7 2,683.00 (OCR spacing)  $33k is a note, not an amount
MONEY = re.compile(r"\(?-?\$\s?(\d(?:[\d,]|\s(?=[\d,]))*(?:\.\d{2})?)\)?(?![\dkK%])")
PLAIN_TRAILING = re.compile(r"(?<![\d.,/])(\d{1,3}(?:,\d{3})+|\d{3,})\.(\d{2})\s*$")
QTY_BEFORE_MONEY = re.compile(r"(?<![\d$.,])(\d{1,3})(?:\.\d{1,4})?\s+(?:[YN]\s+)?\(?\$")

FEE = re.compile(r"\b(doc(?:ument(?:ation)?)?\.?\s*fee|doc/setup|title\s*(?:fee|tax)?|ucc\b|cit\b|processing\s+fee|temp\s+tag|"
                 r"filing\s+fee|setup\s+fee|delivery(?:\s+charge)?|freight|registration|admin(?:istrative)?\s+fee|credit\s+card\s+fee|[a-z]+\s+fee)", re.I)
TRADE = re.compile(r"(?<!/)\b(trade[\s-]?in(?!\s+totals?:\s*\$0)|trade\s+(?:allowance|offer|value)|net\s+trade|less\s+lien)\b|^\s*used\b", re.I)
INCENTIVE = re.compile(r"\b(incentive|rebate|[a-z]+\s+discount|discount\b(?!\s+net)|manufacturer\s+credit|mfg\s+credit)", re.I)
FINANCE = re.compile(r"\b(months?|apr|rate|payment|financ|down\s+payment|deposit|money\s+down|term|insurance|ktac|blended|monthly)\b|%,", re.I)
TOTALISH = re.compile(r"(sub[\s-]?total|total|amount\s*due|net\s*due|balance\s*due|\btax|summary\s*of\s*charges)", re.I)
SECTION_END = re.compile(r"^\s*(cash\s*details|purchase\s*options|quote\s*summary|finance\s*pricing|summary\s*of\s*charges|"
                         r"payment\s*options|trade\s*in\s*totals|totals?\s*$)", re.I)
ITEM_BOUNDARY = re.compile(r"^\s*\(\d{4,}\)\s*(\d{6,})?\s*$")  # wholegood item-number line "(105572)"
STOCK_LINE = re.compile(r"\bstock\s*#", re.I)
SERIAL = re.compile(r"\b[A-Z0-9]{15,}\b|\(?serial\s*number:|\bS/N\b", re.I)
HEADER = re.compile(r"\b(description|qty|quantity|price\s*each|sale\s+price|msrp|amount|stock\s*#|serial\s*#?)\b", re.I)
NOISE_OPTION = re.compile(r"^(s/n|serial|engine:|stock|vin\b|\(sn:|sn:|hours?:|yr:|reprint|page\s+\d|printed|signature|"
                          r"customer|dealer|thank|subject|price|pricing|this\s+quote|quote\s+valid|all\s+prices|note|remark|"
                          r"plus\s+applicable|miscellaneous|tax|---|=)", re.I)
LEGAL = re.compile(r"\b(subject to|warrant(y|ies) (is|are)|liabilit|hereby|agreement|acknowledge|terms and conditions)\b", re.I)


def is_us(text: str) -> bool:
    return bool(US_STATE_ZIP.search(text)) or ("$" in text and ("USD" in text or re.search(r"kubota", text, re.I) is not None))


def money_values(line: str) -> list[float]:
    vals = []
    for m in MONEY.finditer(line):
        raw = m.group(0)
        v = parse_amount(re.sub(r"\s", "", m.group(1)))
        if v is None:
            continue
        vals.append(-v if raw.strip().startswith("(") or "-$" in raw else v)
    if not vals and not re.search(r"%\s*$|\d{1,2}/\d{1,2}/\d{2,4}\s*$", line):
        m = PLAIN_TRAILING.search(line)
        if m and re.search(r"[A-Za-z]", line):
            vals.append(float(m.group(1).replace(",", "") + "." + m.group(2)))
    return vals


def _clean_desc(text: str) -> str:
    t = MONEY.sub(" ", text)
    t = PLAIN_TRAILING.sub(" ", t)
    t = re.sub(r"\b(?:stock\s*#?:?\s*\w+|serial\s*#?:?\s*\w+|s/n:?\s*\w+|\(sn:[^)]*\)|yr:\s*\d{4}|\bun\b|\(\d{4,}\))", " ", t, flags=re.I)
    t = re.sub(r"^\s*(?:\d{3,}\s+)+", "", t)  # leading stock / item numbers
    t = re.sub(r"^\s*\d{1,2}\s+(?=[A-Za-z])", "", t)  # leading quantity
    t = re.sub(r"\s(\d{1,3})(?:\.\d{1,4})?\s+(?:[YN]\s*)?$", " ", t)  # trailing quantity / taxable flag
    return re.sub(r"\s{2,}", " ", t).strip(" -:|•*")


def _option_names(line: str) -> list[str]:
    parts = re.split(r"\s*•\s*", line.strip(" •"))
    out = []
    for p in parts:
        p = re.sub(r";\s*serial no\..*$", "", p, flags=re.I).strip(" -:,")
        p = re.sub(r"^([A-Z]{1,4}\d{3,5}[A-Z]?):\s*", r"\1 ", p)  # "S6606: 80in Bucket" -> "S6606 80in Bucket"
        p = SERIAL.sub("", p).strip(" -:,()")
        if len(p) >= 3 and re.search(r"[A-Za-z]{2}", p) and not NOISE_OPTION.match(p) and not LEGAL.search(p):
            out.append(p[:120])
    return out


@dataclass
class USParse:
    items: list[LineItem] = field(default_factory=list)
    fees: float = 0.0
    trade_in: float = 0.0
    incentives: float = 0.0
    notes: list[str] = field(default_factory=list)


def parse_items(lines: list[str]) -> USParse:
    out = USParse()
    has_msrp = any(re.search(r"\bmsrp\b", ln, re.I) for ln in lines)
    current: LineItem | None = None
    pending: list[str] = []  # unpriced text since the last priced line
    needs_desc: LineItem | OptionItem | None = None
    options_budget = 0
    started = False
    skip_next_amounts = False
    extend_option = 0  # unpriced lines right after a priced attachment continue its name
    for raw in lines:
        line = normalize_dashes(raw).strip()
        if not line:
            continue
        if line.startswith("--- page") or line.startswith("=== page"):
            options_budget, extend_option, needs_desc, pending = 0, 0, None, []  # next page repeats its header
            continue
        vals = money_values(line)
        if re.match(r"^\s*remarks?\b", line, re.I):
            out.notes.append(re.sub(r"^\s*remarks?\s*(?:re\s+)?[:\-]?\s*", "", line, flags=re.I))
            continue
        if started and (SECTION_END.match(line) or (TOTALISH.search(line) and vals and not machine_models(line) and not FEE.search(line))):
            if SECTION_END.match(line) or re.search(r"(sub[\s-]?total|total)", line, re.I):
                break  # equipment section ends at the first total / summary block
            continue
        if ITEM_BOUNDARY.match(line):
            options_budget, needs_desc, pending = 0, None, []
            continue
        if FEE.search(line) or TRADE.search(line) or (INCENTIVE.search(line) and vals):
            skip_next_amounts = not vals
            continue  # counted by parse_charges over the whole document
        if skip_next_amounts and vals and not re.search(r"[A-Za-z]{3}", _clean_desc(line)):
            skip_next_amounts = False
            continue
        skip_next_amounts = False
        if FINANCE.search(line) and not machine_models(line):
            continue
        if len({m.group(0).lower() for m in HEADER.finditer(line)}) >= 2 and not STOCK_LINE.search(line) and not machine_models(line):
            continue
        if vals:
            started = True
            desc = _clean_desc(line)
            borrowed = False
            if not re.search(r"[A-Za-z]{3}", desc) and not STOCK_LINE.search(line):
                desc = " ".join(_clean_desc(p) for p in pending[-2:]).strip()
                borrowed = True
                if FEE.search(desc) or TRADE.search(desc):
                    pending = []
                    continue  # a fee/trade whose label was on the previous line
            if not re.search(r"[A-Za-z]{3}", desc):
                desc = ""
            qty_m = QTY_BEFORE_MONEY.search(line)
            qty = float(qty_m.group(1)) if qty_m and 0 < int(qty_m.group(1)) <= 50 else 1.0
            pos = [v for v in vals if v > 0] or vals
            msrp = None
            if has_msrp and len(pos) >= 3:
                msrp, unit, amount = pos[0], pos[1], pos[2]
            else:
                unit = pos[0]
                amount = pos[1] if len(pos) >= 2 and abs(pos[1] - qty * unit) <= 1 else round(unit * qty, 2)
            models = machine_models(" ".join([desc] + ([] if borrowed else pending[-4:])))
            if models and not borrowed and models[0] not in desc.upper():
                model_line = next((p for p in reversed(pending) if models[0] in p.upper()), "")
                desc = f"{_clean_desc(model_line)} {desc}".strip()
            if models:  # a machine: new line item
                current = LineItem(description=desc or models[0], model=models[0], quantity=qty, unit_price=unit,
                                   base_price=unit, msrp=msrp, amount=amount)
                out.items.append(current)
                options_budget = 14
                needs_desc = current if not desc else None
            elif amount and abs(amount) > 0:  # priced attachment / accessory
                if current is not None:
                    opt = OptionItem(name=desc or "Attachment", price=amount)
                    current.options.append(opt)
                    needs_desc = opt if not desc else None
                    extend_option = 2 if desc else 0
                    current.amount = round((current.amount or 0) + amount, 2)
                    current.unit_price = round((current.base_price or 0) + sum(o.price or 0 for o in current.options), 2)
                    options_budget = max(options_budget, 6)
                else:
                    item = LineItem(description=desc or None, quantity=qty, unit_price=unit, msrp=msrp, amount=amount)
                    out.items.append(item)
                    needs_desc = item if not desc else None
            pending = []
            continue
        # unpriced line
        if FINANCE.search(line) and not machine_models(line):
            continue
        if needs_desc is not None and re.search(r"[A-Za-z]{3}", line) and not NOISE_OPTION.match(line):
            text = _clean_desc(line)
            if isinstance(needs_desc, OptionItem):
                needs_desc.name = text
                extend_option = 1
            else:
                needs_desc.description = text
                if (mm := machine_models(line)) and not needs_desc.model:
                    needs_desc.model = mm[0]
                    if needs_desc.base_price is None:
                        needs_desc.base_price = needs_desc.unit_price
                    current, options_budget = needs_desc, 14  # it was a machine after all
            needs_desc = None
            continue
        if current is not None and (mm := machine_models(line)) and current.model and mm[0].startswith(current.model) and len(mm[0]) > len(current.model):
            current.model = mm[0]  # a fuller spelling of the same model on the next line
            current.description = _clean_desc(line)
            continue
        if current is not None and current.options and current.options[-1].price is not None and re.match(r"^[a-z]{2,4} - ", line):
            current.options[-1].name += " " + line.split(" - ", 1)[1][:80]  # description of the attachment just above
            continue
        if extend_option and current is not None and current.options and not NOISE_OPTION.match(line) and not SERIAL.search(line) \
                and len(line) < 60 and re.search(r"[A-Za-z]{3}", line):
            current.options[-1].name = (current.options[-1].name + " " + _clean_desc(line))[:160]
            extend_option -= 1
            continue
        extend_option = 0
        if current is not None and options_budget > 0 and not LEGAL.search(line) and not TOTALISH.search(line):
            names = _option_names(line)
            if names and not (current.model and all(current.model in n for n in names)):
                current.options.extend(OptionItem(name=n) for n in names if n.upper() != (current.description or "").upper())
                options_budget -= 1
                continue
        if current is not None:
            options_budget = max(0, options_budget - 1)
        pending.append(line)
        pending = pending[-4:]
    return out


def parse_charges(lines: list[str]) -> dict:
    """Fees, trade-in and incentives anywhere in the document; one value per distinct label."""
    seen: dict[tuple[str, str, float], float] = {}
    norm = [normalize_dashes(raw) for raw in lines]
    for i, line in enumerate(norm):
        for kind, rx in (("fees", FEE), ("trade_in", TRADE), ("incentives", INCENTIVE)):
            m = rx.search(line)
            if not m:
                continue
            if kind == "incentives" and re.search(r"not\s+included", line, re.I):
                break  # "Cash Incentive $3500 not included in price" is a note, not a deduction
            vals = money_values(line[m.end():])[:1]
            if not vals and re.match(r"^\s*\$", line):
                vals = money_values(line[:m.start()])[-1:]
            if not vals and kind == "trade_in":
                vals = money_values(line)[-1:]
            if not vals and i + 1 < len(norm) and not re.search(r"[A-Za-z]{3}", MONEY.sub("", norm[i + 1])):
                vals = money_values(norm[i + 1])[:1]  # label alone on its line, amounts on the next
            if vals and abs(vals[0]) > 0:
                # same label + same amount = a summary repeated on another page; different amounts all count
                seen[(kind, re.sub(r"\W+", "", m.group(0).lower()), round(abs(vals[0]), 2))] = abs(vals[0])
            break
    out = {"fees": 0.0, "trade_in": 0.0, "incentives": 0.0}
    for (kind, _label, _amount), v in seen.items():
        out[kind] = round(out[kind] + v, 2)
    return out


TOTAL_LABELS = {  # field -> label patterns in priority order (most specific first); first pattern with a hit wins
    "total": [r"invoice\s+total", r"quote\s+total", r"total\s+net\s+sale\s+price", r"grand\s+total", r"finance\s+subtotal",
              r"cash\s*sub-?\s?total", r"total\s+amount(?!\s+financed)", r"total\s+due", r"\$[\d,.]+\s+total\s*$",
              r"\btotal\s*:(?=\s*\$)", r"^\s*total(?=\s*\$)"],
    "subtotal": [r"(?<!finance\s)(?<!cash\s)(?<!cash)\bsub[\s-]?total(?!\s+of)", r"\$[\d,.]+\s+subtotal\s*$", r"total\s*selling\s*price",
                 r"kubota\s+total\s+sale\s+price", r"total\s+sale\s+price", r"equipment\s*total", r"total\s+equipment\s+price"],
    "tax_total": [r"total\s+tax", r"\$[\d,.]+\s+sales\s+tax\s*$", r"sales\s+tax", r"net\s+taxes(?:\s+due)?", r"state\s+tax",
                  r"\btax\s*:", r"^\s*tax\b"],
}


PLAIN_AFTER_LABEL = re.compile(r"^\s*[:\-]?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})\b")


def parse_totals(lines: list[str]) -> dict:
    found: dict = {}
    norm = [normalize_dashes(raw) for raw in lines]
    for fld, patterns in TOTAL_LABELS.items():
        for pat in patterns:
            v = _first_value(norm, pat, fld)
            if v:
                found[fld] = v
                break
    return found


def _first_value(lines: list[str], pat: str, fld: str) -> float | None:
    for line in lines:
        m = re.search(pat, line, re.I)
        if not m:
            continue
        after = [v for v in money_values(line[m.end():]) if v is not None]
        inside = money_values(m.group(0))
        plain = PLAIN_AFTER_LABEL.match(line[m.end():])
        if inside:
            v = inside[-1]  # "$45,244.96 Subtotal"
        elif after:
            v = after[-1] if fld == "tax_total" and len(after) > 1 and after[0] < 1 else after[0]  # "$0.06 $4,248.00"
        elif plain:  # "Quote Total: 81470.00" (no currency symbol)
            v = float(plain.group(1).replace(",", ""))
        else:
            continue
        if v and v > 0:
            return v
    return None


# ---------- dealer (vendor) name ----------
DEALERISH = re.compile(r"\b(kubota|tractors?|equipment|implements?|machinery|farm\w*|power\w*|ag\b|agri\w*|rentals?|motors?|"
                       r"outdoor|turf|inc\.?|llc|l\.l\.c\.|co\.|company|corp\.?|supply)\b", re.I)
LABELISH = re.compile(r"(quoted\s+by|^\s*for\s*:|ship\s+to|bill\s+to|sold\s+to|remit|^\s*page\b|prepared\s+by|purchaser|"
                      r"customer|i\s*n\s*v\s*o\s*i\s*c\s*e|invoice\s*#|^\s*date|phone|fax|cell|www\.|@|quote\s*(?:id|number|#|:)|"
                      r"sales\s+agreement|salesperson|salesman|sold\s+by|^\s*name\s*:|we\s+are\s+pleased|kubota\s+credit|docusign|"
                      r"insurance|payment|financ|estimated|includes|indicate|stock\s*#|item\s+number|trade|\$|\bmodel\b|serial)", re.I)
NOT_DEALER_SITES = re.compile(r"(cdkglobal|docusign|kubotausa|kubotacredit|kubota\.com|adobe|google|microsoft|intuit|quickbooks)", re.I)
NOT_NAME_WORDS = {"quote", "final", "invoice", "target", "funds", "request", "cash", "price", "finance", "docs", "copy"}


def _decamel(token: str) -> str:
    t = re.sub(r"(?<=[a-z])of(?=[A-Z])", " of ", token)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t).strip()


def _domain_name(domain: str) -> str:
    core = re.sub(r"^www\.", "", domain.lower()).split(".")[0]
    return " ".join(w.capitalize() for w in re.split(r"[-_]", core) if w)


def dealer_name(lines: list[str], filename: str = "") -> str | None:
    """The selling dealer's name, or None when it only appears in a logo (then: escalate / review)."""
    head = lines[:14]
    # customer names often appear in the file name ("RYAN ANDERSON.pdf"): never take those as the dealer
    file_words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", filename) if not DEALERISH.search(w)} - NOT_NAME_WORDS
    # Kubota quote tool: "Ship To Kubota Dealer Bill To" then "<customer> <DealerName> <contact> <customer>"
    for i, ln in enumerate(head):
        if re.search(r"ship\s*to\s+kubota\s+dealer\s+bill\s*to", ln, re.I) and i + 1 < len(lines):
            toks = lines[i + 1].split()
            if len(toks) >= 2:
                return _decamel(toks[1])
    sites = [m.group(1) for ln in lines[:40] for m in re.finditer(r"\b(?:www\.)([a-z0-9-]+\.[a-z.]{2,6})", ln, re.I)]
    sites += [m.group(1) for ln in lines[:40] for m in re.finditer(r"\b([a-z0-9-]+\.(?:com|net))\b", ln, re.I)
              if not re.search(r"@", ln[max(0, m.start() - 1):m.start()])]
    sites = [d for d in sites if not NOT_DEALER_SITES.search(d)]
    candidates = []
    for ln in head:
        clean = re.sub(r"\s+(quotation|quote|tax\s+invoice|invoice|sales\s+agreement.*)$", "", ln.strip(), flags=re.I).strip(" ,")
        words = re.findall(r"[A-Za-z]{4,}", clean)
        half = len(clean) // 2
        repeated = len(clean) > 10 and clean[:half].strip().lower() == clean[half:].strip().lower()  # bill-to/ship-to side by side
        if (DEALERISH.search(clean) and not LABELISH.search(clean) and not re.match(r"^\s*\d", clean)
                and 3 < len(clean) < 60 and not re.search(r"\d{3}[-) ]\d{3}", clean) and not machine_models(clean)
                and not repeated and not any(w.lower() in file_words for w in words)):
            candidates.append(clean)
    if sites:
        site_words = {w for d in sites for w in re.split(r"[-_.]", d.lower().split(".")[0]) if len(w) >= 4}
        site_cores = {d.lower().split(".")[0].replace("-", "") for d in sites}
        for c in candidates:
            squashed = re.sub(r"[^a-z]", "", c.lower())
            if any(w in squashed for w in site_words) or any(core.startswith(squashed[:6]) for core in site_cores if len(squashed) >= 6):
                return c
        return _domain_name(sites[0])  # the dealer's own website beats an unmatched name-like line
    return candidates[0] if candidates else None
