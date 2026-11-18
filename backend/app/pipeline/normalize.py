"""Shared value normalizers: amounts, dates, GSTIN."""
from __future__ import annotations

import re
from datetime import date, datetime

GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
AMOUNT_RE = re.compile(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?(?![\w])")

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def gstin_checksum_ok(gstin: str) -> bool:
    """Validate the GSTIN check character (mod-36 Luhn variant used by GSTN)."""
    gstin = gstin.strip().upper()
    if len(gstin) != 15 or not GSTIN_RE.fullmatch(gstin):
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        val = _GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += val // 36 + val % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36] == gstin[14]


def parse_amount(raw: str | float | int | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = re.sub(r"[^\d.,]", "", s)
    if not s or not re.search(r"\d", s):
        return None
    # European style "1.234,56"
    if re.fullmatch(r"\d{1,3}(\.\d{3})+,\d{1,2}", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def amounts_in(line: str) -> list[float]:
    return [v for m in AMOUNT_RE.finditer(line) if (v := parse_amount(m.group())) is not None]


def parse_date(raw: str | None, dayfirst: bool = True) -> str | None:
    """Return ISO YYYY-MM-DD or None. Defaults to day-first (India/UK)."""
    if not raw:
        return None
    s = raw.strip().replace(",", " ").replace(".", "/")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s, flags=re.I)
    s = re.sub(r"\s+", " ", s)

    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        return _iso(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        d, mo = (a, b) if dayfirst else (b, a)
        if mo > 12 and d <= 12:
            d, mo = mo, d
        return _iso(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})[ /-]([A-Za-z]{3,9})[ /-](\d{2,4})", s)
    if m and m[2][:3].lower() in _MONTHS:
        y = int(m[3])
        return _iso(y + 2000 if y < 100 else y, _MONTHS[m[2][:3].lower()], int(m[1]))
    m = re.fullmatch(r"([A-Za-z]{3,9}) (\d{1,2}) (\d{4})", s)
    if m and m[1][:3].lower() in _MONTHS:
        return _iso(int(m[3]), _MONTHS[m[1][:3].lower()], int(m[2]))
    return None


DATE_TOKEN = r"(\d{4}[/.-]\d{1,2}[/.-]\d{1,2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}(?:st|nd|rd|th)?[ /-][A-Za-z]{3,9}[ /,-]+\d{2,4}|[A-Za-z]{3,9} \d{1,2},? \d{4})"


def _iso(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def is_iso_date(s: str | None) -> bool:
    if not s:
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False
