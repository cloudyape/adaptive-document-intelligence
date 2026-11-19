"""Kubota-specific knowledge: machine model numbers and their series.

Series names come from Kubota spec sheets ("S Series SVL75-3HFWVRCC", "K Series KX040-4R3T").
Only prefixes with clear evidence are mapped; anything else is left for a reviewer. A series
printed on the document always wins over the mapping.
"""
from __future__ import annotations

import re

# Whole-machine model numbers. Attachments/options use other codes (K7875A, DCU6770, S6606, AP-...).
MACHINE_MODEL = re.compile(
    r"\b(SVL|SSV|SCL|KX|U|R|MX|M|LX|L|BX|B|RTV)-?(\d{2,4})([A-Z0-9]*)((?:-\d[A-Z0-9]*)?)\b"
)
SERIES_BY_PREFIX = {
    "SVL": "S Series", "SSV": "S Series", "KX": "K Series", "U": "U Series",
    "B": "B Series", "BX": "BX Series", "L": "L Series", "LX": "LX Series", "MX": "MX Series",  # not M: M62 is a TLB
}
PRINTED_SERIES = re.compile(r"\b([A-Z]{1,3}) Series\s+([A-Z]{1,4}-?\d[A-Z0-9-]*)")
# Codes that look like models but are attachments, options or part numbers
NOT_A_MACHINE = re.compile(r"^(K\d{4}|DCU\d|S\d{4}|AP-)")


def normalize(text: str) -> str:
    """Unicode hyphens/dashes in model numbers ("SVL75‐3") -> ASCII hyphen."""
    return re.sub(r"[‐‑‒–—]", "-", text)


def machine_models(text: str) -> list[str]:
    """Distinct machine model numbers in order of appearance, longest variant kept."""
    found: list[str] = []
    for m in MACHINE_MODEL.finditer(normalize(text).upper()):
        model = m.group(0)
        prefix, digits = m.group(1), m.group(2)
        run = re.match(r"\d+", model[len(prefix):].lstrip("-")).group(0)
        if NOT_A_MACHINE.match(model) or len(run) >= 5 or (prefix in ("L", "B") and len(digits) != 4):
            continue  # stock numbers (B10399) and non-models (L190); B/L tractors have 4-digit models
        # keep the most specific spelling of the same base model (SVL75 < SVL75-3 < SVL75-3HFWVRCC)
        same = [i for i, f in enumerate(found) if f.startswith(model) or model.startswith(f)]
        if same:
            i = same[0]
            if len(model) > len(found[i]):
                found[i] = model
        else:
            found.append(model)
    return found


def printed_series(text: str) -> dict[str, str]:
    """{model: "S Series"} for series printed next to a model on the document."""
    return {normalize(m.group(2)).upper(): f"{m.group(1)} Series" for m in PRINTED_SERIES.finditer(normalize(text))}


def series_for(model: str | None, printed: dict[str, str] | None = None) -> str | None:
    if not model:
        return None
    model = normalize(model).upper()
    for printed_model, series in (printed or {}).items():
        if model.startswith(printed_model) or printed_model.startswith(model):
            return series
    m = MACHINE_MODEL.match(model)
    return SERIES_BY_PREFIX.get(m.group(1)) if m else None


# Attachment / option part codes: Land Pride AP-..., Kubota K7875A, DCU6770, S6606 ...
PART_CODE = re.compile(r"\b(AP-[A-Z0-9]{3,}|K\d{4}[A-Z]?|DCU\d{4}|S\d{4}[A-Z]?|[A-Z]{2,4}\d{3,5}[A-Z0-9-]{0,8})\b")
VIN_LIKE = re.compile(r"^[A-Z0-9]{15,}$")


def part_code(text: str | None) -> str | None:
    if not text:
        return None
    for m in PART_CODE.finditer(normalize(text).upper()):
        if not VIN_LIKE.match(m.group(1)):
            return m.group(1)
    return None


def enrich_models(doc, text: str | None = None) -> None:
    """Fill model (machine model number, else attachment part code) and Kubota series on each line."""
    printed = printed_series(text) if text else {}
    for li in doc.line_items:
        blob = " ".join(filter(None, [li.model, li.description]))
        if not li.model:
            machines = machine_models(blob)
            li.model = machines[0] if machines else part_code(li.description)
        if not li.series and li.model:
            li.series = series_for(li.model, printed)
