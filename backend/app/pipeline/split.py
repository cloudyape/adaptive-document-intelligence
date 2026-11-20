"""Find where each quote starts in a merged PDF ("packet") so every quote is extracted on its own.

Dealers send one PDF with many quotes stapled together: digital quotes, scans, terms-and-conditions
pages, finance worksheets. Each page is reduced to a few signals and a boundary is placed only on
evidence, strongest first:

1. A page marker: "Page 1 of 3" / "PAGE 1" starts a quote; "Page 2 of 3" continues one.
2. The quote / order / invoice number: a different number on the page starts a new quote.
3. A new letterhead: other company words or another dealer's phone number at the top of the page.
4. Terms-and-conditions, blank and signature-only pages stay with the quote before them.

When nothing says "new quote", the page stays with the current one: merging two quotes sends the
pair to review (validation catches the mismatched totals), while cutting one quote in half would
produce two broken records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.pipeline.engines.rules import RulesEngine
from app.pipeline.engines.us_quote import money_values
from app.schemas import DocType

PAGE_OF = re.compile(r"\bpage\s*[:#]?\s*(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b", re.I)
PAGE_ALONE = re.compile(r"^\s*page\s*[:#]?\s*(\d{1,3})\s*$", re.I | re.M)
TITLE = re.compile(r"\b(quot(?:e|ation)|sales\s+(?:order|agreement|contract|quotation)|purchase\s+(?:order|agreement)|"
                   r"bill\s+of\s+sale|invoice|proposal|buyer'?s?\s+order|retail\s+(?:purchase|order)|estimate)\b", re.I)
TOTALISH = re.compile(r"total|balance\s*due|amount\s*due|net\s*amount", re.I)  # dealer tools glue words: TOTALAMOUNTDUE
APPENDIX = re.compile(r"(terms\s+(?:and|&)\s+conditions|limited\s+warranty|warranty\s+(?:terms|statement)|arbitration|"
                      r"disclaimer\s+of\s+warrant|important\s*[:\-]?\s*read|privacy\s+notice|conditions\s+of\s+sale|"
                      r"customer\s+acknowledg)", re.I)
# Pages that belong to the quote before them: trade-in sheets, finance worksheets, spec sheets
CONTINUATION = re.compile(r"(finance\s+calculator|trade\s*in\s+description|sales\s+cost\s+sheet|standard\s*equipment|"
                          r"specifications|equipment\s*in\s*standard\s*machine)", re.I)
WORD = re.compile(r"[a-z]{3,}")
PHONE = re.compile(r"(?<!\d)(\d{3})[.\-](\d{4})(?!\d)")  # 745-2148: OCR often splits off the area code
STOP = {"the", "and", "for", "page", "quote", "date", "customer", "phone", "fax", "email", "www", "com", "inc", "llc"}


@dataclass
class PageSignals:
    number: int  # 1-based page number in the packet
    text: str
    marker: tuple[int, int | None] | None = None  # (page x, of y) printed on the page
    doc_number: str | None = None
    header: set[str] = field(default_factory=set)  # words of the letterhead
    phones: set[str] = field(default_factory=set)  # phone numbers printed in the letterhead
    titled: bool = False  # says QUOTE / SALES ORDER / INVOICE...
    has_total: bool = False
    money: int = 0  # dollar amounts on the page
    appendix: bool = False  # terms, warranty, legal text
    continuation: bool = False  # trade-in / finance / spec sheet that belongs to the previous quote
    blank: bool = False


@dataclass
class Segment:
    start: int
    end: int
    reason: str  # why a new quote starts on `start`

    @property
    def pages(self) -> range:
        return range(self.start, self.end + 1)


def _marker(text: str) -> tuple[int, int | None] | None:
    m = PAGE_OF.search(text)
    if m and 0 < int(m.group(1)) <= int(m.group(2)) <= 99:
        return int(m.group(1)), int(m.group(2))
    m = PAGE_ALONE.search(text)
    if m and 0 < int(m.group(1)) <= 20:
        return int(m.group(1)), None
    return None


def number_key(number: str | None) -> str | None:
    from app.service import number_key as nk

    return nk(number) if number else None


def signals(number: int, text: str) -> PageSignals:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    words = len(WORD.findall(text.lower()))
    s = PageSignals(number, text, blank=words < 4)
    if s.blank:
        return s
    s.marker = _marker(text)
    number = RulesEngine._doc_number(text, DocType.QUOTATION)
    # OCR noise yields "numbers" like 93 or O21: too short to tell quotes apart
    s.doc_number = number if number and sum(ch.isdigit() for ch in number) >= 4 else None
    head = " ".join(lines[:4]).lower()
    s.header = {w for w in WORD.findall(head) if w not in STOP}
    s.phones = phones("\n".join(lines[:8]))
    s.titled = bool(TITLE.search(" ".join(lines[:12])))
    s.money = sum(len(money_values(ln)) for ln in lines if "$" in ln)
    s.has_total = any(TOTALISH.search(ln) and money_values(ln) for ln in lines)
    legal = len(APPENDIX.findall(text))
    # Terms pages are long legal prose with few amounts; a quote can mention its terms in a footer.
    s.continuation = bool(CONTINUATION.search(" ".join(lines[:6]))) and not s.doc_number and not s.marker
    s.appendix = legal >= 1 and s.money <= 2 and not s.doc_number and (words > 250 or legal >= 2)
    return s


def phones(text: str) -> set[str]:
    """Local part (7 digits) of every phone number in the text."""
    return {"".join(m.groups()) for m in PHONE.finditer(text)}


def _same_number(key: str, keys: set[str]) -> bool:
    """OCR misreads one digit now and then (127266 vs 427266): one substitution still counts as the same number."""
    return any(key == k or (len(key) == len(k) >= 5 and sum(x != y for x, y in zip(key, k)) <= 1) for k in keys)


def _similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return True  # unknown: don't split on it
    return len(a & b) / min(len(a), len(b)) >= 0.34


def segment(page_texts: list[str]) -> list[Segment]:
    """Split a packet's pages into quotes. A single-quote PDF returns one segment."""
    pages = [signals(i + 1, t) for i, t in enumerate(page_texts)]
    if not pages:
        return []
    segs = [Segment(1, 1, "first page")]
    cur = [pages[0]]

    for p in pages[1:]:
        reason = _boundary(cur, p)
        if reason:
            segs.append(Segment(p.number, p.number, reason))
            cur = [p]
        else:
            segs[-1].end = p.number
            cur.append(p)
    return _merge_leading_fragments(segs, pages)


def _boundary(cur: list[PageSignals], p: PageSignals) -> str | None:
    """Why `p` starts a new quote, or None when it continues the current one."""
    if p.blank or p.appendix or p.continuation:
        return None
    first = next((q for q in cur if not q.blank), cur[0])
    numbers = {number_key(q.doc_number) for q in cur if q.doc_number}
    total_seen = any(q.has_total for q in cur)
    prev = next((q for q in reversed(cur) if not q.blank), cur[-1])
    form_only = all(q.money == 0 for q in cur)  # an empty quote form (no amounts filled in)

    if p.marker:
        x, y = p.marker
        if x == 1:
            return "page 1 marker"
        prev_m = next((q.marker for q in reversed(cur) if q.marker), None)
        if prev_m and (prev_m[1] == y or y is None) and prev_m[0] < x:
            return None  # "page 2 of 3" after "page 1 of 3"
        if not prev_m and x > 1 and (not p.doc_number or _same_number(number_key(p.doc_number), numbers) or not numbers):
            return None
    if p.doc_number and numbers:
        if _same_number(number_key(p.doc_number), numbers):
            return None
        return f"new number {p.doc_number}"
    if p.doc_number and not numbers and total_seen:
        return f"number {p.doc_number} after a completed quote"
    if prev.appendix and (p.titled or p.doc_number or p.money >= 2 or (
            p.money and not _similar(first.header, p.header))):
        return "new quote after terms pages"
    if form_only and p.money >= 2 and (p.titled or p.has_total):
        return "filled-in quote after an empty form"
    if total_seen and (p.titled or p.money >= 2) and not _similar(first.header, p.header):
        return "new letterhead after a completed quote"
    # Handwritten / scanned forms rarely OCR into a title or dollar amounts, but the dealer's printed
    # letterhead phone number does: a letterhead phone the current quote never mentions means another dealer.
    if (total_seen or first.phones) and p.phones and not _similar(first.header, p.header) and not any(
            p.phones & phones(q.text) for q in cur):
        return "another dealer's letterhead (new phone number)"
    if p.titled and p.money >= 2 and not _similar(first.header, p.header) and not _similar(prev.header, p.header):
        return "new letterhead"
    if total_seen and p.titled and p.money >= 2 and p.has_total:
        return "a second complete quote"
    return None


def _merge_leading_fragments(segs: list[Segment], pages: list[PageSignals]) -> list[Segment]:
    """A segment of only blank/legal pages (a cover sheet) is joined to the quote after it."""
    out: list[Segment] = []
    for s in segs:
        substantive = [p for p in pages[s.start - 1:s.end] if not (p.blank or p.appendix)]
        if not substantive and out:
            out[-1].end = s.end
        elif out and not any(not (p.blank or p.appendix) for p in pages[out[-1].start - 1:out[-1].end]):
            out[-1] = Segment(out[-1].start, s.end, s.reason)
        else:
            out.append(s)
    return out
