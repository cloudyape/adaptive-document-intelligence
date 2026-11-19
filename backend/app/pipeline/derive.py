"""Fill derivable dealer-quote values: whichever of Margin By / Margin By % is missing.

Margin By % is taken as a percentage of the unit's pre-tax selling price:
base price + priced options (x quantity); if there's no base price, the line amount.
At quote level, the basis is the sum of the line bases, else the subtotal.
"""
from __future__ import annotations

from app.pipeline.kubota import enrich_models, part_code
from app.schemas import ExtractedDocument, LineItem


def line_basis(li: LineItem) -> float | None:
    qty = li.quantity or 1
    if li.base_price is not None:
        return (li.base_price + sum(o.price or 0 for o in li.options)) * qty
    if li.amount is not None:
        return li.amount
    if li.unit_price is not None:
        return li.unit_price * qty
    return None


def _fill(amount: float | None, percent: float | None, basis: float | None) -> tuple[float | None, float | None, str | None]:
    if not basis:
        return amount, percent, None
    if amount is None and percent is not None:
        return round(basis * percent / 100, 2), percent, "margin amount"
    if percent is None and amount is not None:
        return amount, round(amount / basis * 100, 2), "margin %"
    return amount, percent, None


def derive(doc: ExtractedDocument) -> list[str]:
    """Mutates `doc`; returns notes describing what was calculated."""
    enrich_models(doc)
    notes = []
    for i, li in enumerate(doc.line_items):
        li.margin_amount, li.margin_percent, what = _fill(li.margin_amount, li.margin_percent, line_basis(li))
        if what:
            notes.append(f"line {i + 1}: {what} calculated")

    line_margins = [li.margin_amount for li in doc.line_items if li.margin_amount is not None]
    if doc.margin_amount is None and line_margins and len(line_margins) == len(doc.line_items):
        doc.margin_amount = round(sum(line_margins), 2)
        notes.append("quote margin = sum of line margins")
    bases = [line_basis(li) for li in doc.line_items]
    quote_basis = sum(bases) if bases and all(b is not None for b in bases) else doc.subtotal
    doc.margin_amount, doc.margin_percent, what = _fill(doc.margin_amount, doc.margin_percent, quote_basis)
    if what:
        notes.append(f"quote {what} calculated")
    return notes


def apply_dealer_costs(doc: ExtractedDocument, lookup) -> list[str]:
    """Margin By = selling price - dealer cost (from the active price file); Margin By % = margin / selling price.

    Only fills margins the document didn't print. Priced options are included when every one of them has a
    known cost; otherwise the margin covers the machine alone (base price vs machine cost).
    """
    if not lookup:
        return []
    notes = []
    total_margin = total_selling = 0.0
    for i, li in enumerate(doc.line_items):
        unit_cost = lookup.cost(li.model)
        if unit_cost is None:
            continue
        qty = li.quantity or 1
        priced = [o for o in li.options if o.price is not None]
        opt_costs = [lookup.cost(part_code(o.name) or o.name) for o in priced]
        machine_price = li.base_price if li.base_price is not None else (
            li.unit_price if not priced else None)
        if priced and all(c is not None for c in opt_costs) and line_basis(li) is not None:
            selling, cost, scope = line_basis(li), (unit_cost + sum(opt_costs)) * qty, "machine + options"
        elif machine_price is not None:
            selling, cost, scope = machine_price * qty, unit_cost * qty, "machine only"
        elif li.amount is not None and not priced:
            selling, cost, scope = li.amount, unit_cost * qty, "machine only"
        else:
            continue
        if selling <= 0:
            continue
        if li.margin_amount is None and li.margin_percent is None:
            li.margin_amount = round(selling - cost, 2)
            li.margin_percent = round((selling - cost) / selling * 100, 2)
            notes.append(f"line {i + 1}: margin from dealer cost ({scope})")
        total_margin += li.margin_amount if li.margin_amount is not None else 0
        total_selling += selling
    if notes and doc.margin_amount is None and doc.margin_percent is None and total_selling:
        doc.margin_amount = round(total_margin, 2)
        doc.margin_percent = round(total_margin / total_selling * 100, 2)
        doc.margin_source = f"dealer cost: {lookup.source}" if lookup.source else "dealer cost price file"
    return notes
