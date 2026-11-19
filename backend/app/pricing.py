"""Dealer cost price files: parse uploads (CSV / Excel / PDF) and look up costs by model number."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from app.pipeline.normalize import amounts_in, parse_amount

MODEL_HEADERS = ["model #", "model number", "model no", "model", "part number", "part #", "part no", "item number",
                 "item #", "item", "sku", "code"]
COST_HEADERS = ["dealer cost", "dealer net", "dealer price", "net dealer", "dealer invoice", "invoice", "net cost",
                "cost", "net"]
MSRP_HEADERS = ["msrp", "suggested retail", "retail", "list price", "list"]
DESC_HEADERS = ["description", "desc", "name"]


def model_key(model: str) -> str:
    """Case/separator-insensitive key: 'SVL75-3HFWVRCC' == 'svl75 3hfwvrcc'; unicode hyphens folded."""
    return re.sub(r"[^A-Z0-9]", "", (model or "").upper())


@dataclass
class ParsedPriceList:
    entries: list[dict] = field(default_factory=list)  # model_number, description, dealer_cost, msrp
    warnings: list[str] = field(default_factory=list)


def _pick(header: list[str], names: list[str]) -> int | None:
    low = [h.lower().strip() for h in header]
    for name in names:  # most specific names first
        for i, h in enumerate(low):
            if h == name or h.startswith(name) or name in h.split("/"):
                return i
    for name in names:
        for i, h in enumerate(low):
            if name in h:
                return i
    return None


def _from_rows(rows: list[list[str]], out: ParsedPriceList) -> None:
    header_idx = cols = None
    for idx, row in enumerate(rows[:25]):
        m, c = _pick(row, MODEL_HEADERS), _pick(row, COST_HEADERS)
        if m is not None and c is not None and m != c:
            header_idx, cols = idx, {"model": m, "cost": c, "msrp": _pick(row, MSRP_HEADERS), "desc": _pick(row, DESC_HEADERS)}
            if cols["msrp"] == c:
                cols["msrp"] = None
            break
    if header_idx is None:
        out.warnings.append("no header row with a model/part column and a dealer cost column was found")
        return
    for row in rows[header_idx + 1:]:
        cell = lambda k: row[cols[k]].strip() if cols.get(k) is not None and cols[k] < len(row) and row[cols[k]] else None  # noqa: E731
        model, cost = cell("model"), parse_amount(cell("cost"))
        if not model or cost is None or cost <= 0:
            continue  # a zero/blank cost is an unfilled row, never a real dealer cost
        out.entries.append({"model_number": model, "dealer_cost": cost, "msrp": parse_amount(cell("msrp")),
                            "description": cell("desc")})


def _from_pdf_text(text: str, out: ParsedPriceList) -> None:
    """Best effort: lines that start with a model/part code and carry one or two amounts."""
    out.warnings.append("PDF price list read line by line (best effort): check a few entries")
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z]{1,4}-?[0-9][A-Z0-9-]{2,})\s+(.*)$", line.strip())
        if not m:
            continue
        nums = [n for n in amounts_in(m.group(2)) if n >= 1]
        if not nums:
            continue
        desc = re.sub(r"[\$\d,.\s]+$", "", m.group(2)).strip() or None
        if len(nums) >= 2:  # convention on price lists: dealer cost is lower than MSRP
            cost, msrp = min(nums[-2:]), max(nums[-2:])
        else:
            cost, msrp = nums[-1], None
        if cost > 0:
            out.entries.append({"model_number": m.group(1), "dealer_cost": cost, "msrp": msrp, "description": desc})


def parse_price_file(data: bytes, filename: str) -> ParsedPriceList:
    out = ParsedPriceList()
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        for ws in wb.worksheets:
            rows = [["" if c is None else str(c) for c in r] for r in ws.iter_rows(values_only=True)]
            before = len(out.entries)
            _from_rows(rows, out)
            if len(out.entries) > before:
                out.warnings = [w for w in out.warnings if not w.startswith("no header")]
    elif name.endswith(".csv") or name.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        _from_rows(list(csv.reader(io.StringIO(text))), out)
    elif name.endswith(".pdf"):
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            tables = [t for p in pdf.pages for t in (p.extract_tables() or [])]
            for t in tables:
                _from_rows([[c or "" for c in r] for r in t], out)
            if not out.entries:
                out.warnings = []
                _from_pdf_text("\n".join(p.extract_text() or "" for p in pdf.pages), out)
    else:
        out.warnings.append(f"unsupported price file type: {filename}")
    # last entry for a model wins (later rows usually supersede)
    dedup = {model_key(e["model_number"]): e for e in out.entries}
    out.entries = list(dedup.values())
    return out


class CostLookup:
    """Dealer cost by model / part number, with longest-prefix fallback (SVL75-3HFWVRCC -> SVL75-3HFWVRCC*)."""

    def __init__(self, entries: dict[str, float], source: str | None = None):
        self.costs = entries
        self.source = source

    def cost(self, model: str | None) -> float | None:
        if not model:
            return None
        key = model_key(model)
        if key in self.costs:
            return self.costs[key]
        # quote may carry a longer code than the list (suffix letters), or vice versa; require a solid overlap
        candidates = [k for k in self.costs if len(k) >= 5 and (key.startswith(k) or k.startswith(key))]
        return self.costs[max(candidates, key=len)] if candidates else None

    def __bool__(self) -> bool:
        return bool(self.costs)
