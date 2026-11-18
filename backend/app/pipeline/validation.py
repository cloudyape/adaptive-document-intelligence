"""Validation: does an extraction add up?

Engines can be confidently wrong, so acceptance is decided by independent checks, not by the
engine's self-reported confidence alone: completeness, formats, GSTIN checksums, line arithmetic,
subtotal/tax/total reconciliation, and GST consistency.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from app.config import get_settings
from app.pipeline.normalize import GSTIN_RE, gstin_checksum_ok, is_iso_date
from app.schemas import FINANCIAL_TYPES, DocType, ExtractedDocument

PENALTY = {"critical": 0.25, "error": 0.12, "warning": 0.04}


@dataclass
class Issue:
    field: str
    code: str
    message: str
    severity: str  # critical | error | warning


@dataclass
class ValidationReport:
    score: float
    passed: bool
    issues: list[Issue] = field(default_factory=list)
    checks_run: int = 0

    @property
    def critical(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "critical"]

    def messages(self) -> list[str]:
        return [f"[{i.severity}] {i.field}: {i.message}" for i in self.issues]

    def to_dict(self) -> dict:
        return {"score": self.score, "passed": self.passed, "checks_run": self.checks_run, "issues": [asdict(i) for i in self.issues]}


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= max(tol, abs(b) * 0.002)


def validate(doc: ExtractedDocument, engine_confidence: float = 0.5) -> ValidationReport:
    tol = get_settings().amount_tolerance
    issues: list[Issue] = []
    checks = 0

    def check(ok: bool, fld: str, code: str, msg: str, sev: str) -> None:
        nonlocal checks
        checks += 1
        if not ok:
            issues.append(Issue(fld, code, msg, sev))

    # ---- completeness ----
    check(doc.document_type in FINANCIAL_TYPES, "document_type", "not_financial", f"type is '{doc.document_type}'", "critical")
    check(bool(doc.vendor.name), "vendor.name", "missing", "vendor name missing", "critical")
    check(doc.total is not None, "total", "missing", "grand total missing", "critical")
    number_sev = "warning" if doc.document_type == DocType.QUOTATION else "error"
    check(bool(doc.document_number), "document_number", "missing", "document number missing", number_sev)
    check(bool(doc.document_date), "document_date", "missing", "document date missing", "error")
    check(bool(doc.line_items), "line_items", "missing", "no line items", "error")
    check(bool(doc.currency), "currency", "missing", "currency missing", "warning")

    # ---- formats / plausibility ----
    for name in ("document_date", "due_date", "valid_until"):
        val = getattr(doc, name)
        if val:
            check(is_iso_date(val), name, "bad_date", f"'{val}' is not a valid YYYY-MM-DD date", "error")
    if is_iso_date(doc.document_date):
        d = date.fromisoformat(doc.document_date)
        check(d <= date.today() + timedelta(days=2), "document_date", "future", f"{d} is in the future", "error")
        check(d >= date.today() - timedelta(days=365 * 5), "document_date", "very_old", f"{d} is over 5 years old", "warning")
        if is_iso_date(doc.due_date):
            check(date.fromisoformat(doc.due_date) >= d, "due_date", "before_issue", "due date is before document date", "error")
    if doc.vendor.name:
        vn = doc.vendor.name.strip().lower()
        check(vn != (doc.buyer.name or "").strip().lower(), "vendor.name", "same_as_buyer", "vendor name equals buyer name", "critical")
        check(sum(h in vn for h in ("description", "qty", "rate", "amount", "price")) < 2, "vendor.name", "looks_like_header",
              f"vendor name '{doc.vendor.name}' looks like a table header", "critical")
    for party in ("vendor", "buyer"):
        g = getattr(doc, party).gstin
        if g:
            g = g.replace(" ", "").upper()
            check(bool(GSTIN_RE.fullmatch(g)), f"{party}.gstin", "bad_format", f"'{g}' is not a GSTIN", "error")
            if GSTIN_RE.fullmatch(g):
                check(gstin_checksum_ok(g), f"{party}.gstin", "bad_checksum", f"'{g}' fails GSTIN checksum (likely misread)", "error")
    if doc.vendor.gstin and doc.buyer.gstin:
        check(doc.vendor.gstin != doc.buyer.gstin, "buyer.gstin", "same_as_vendor", "buyer GSTIN equals vendor GSTIN", "error")
    for name in ("total", "subtotal", "tax_total"):
        v = getattr(doc, name)
        if v is not None:
            check(v >= 0 or doc.document_type == DocType.CREDIT_NOTE, name, "negative", f"{name} is negative", "error")

    # ---- line arithmetic ----
    bad_lines = 0
    for i, li in enumerate(doc.line_items):
        if li.quantity is not None and li.unit_price is not None and li.amount is not None:
            expected = li.quantity * li.unit_price - (li.discount or 0)
            # Some vendors print tax-inclusive line amounts
            incl = expected * (1 + (li.tax_rate or 0) / 100)
            ok = _close(expected, li.amount, tol) or _close(li.quantity * li.unit_price, li.amount, tol) or (li.tax_rate and _close(incl, li.amount, tol))
            check(bool(ok), f"line_items[{i}].amount", "line_math", f"{li.quantity} x {li.unit_price} - {li.discount or 0} != {li.amount}", "error" if bad_lines < 2 else "warning")
            bad_lines += 0 if ok else 1

    # ---- reconciliation ----
    line_sum = sum(li.amount or 0 for li in doc.line_items) if doc.line_items and all(li.amount is not None for li in doc.line_items) else None
    taxes = doc.tax_total if doc.tax_total is not None else (
        sum(v or 0 for v in (doc.cgst, doc.sgst, doc.igst, doc.cess)) if any(v is not None for v in (doc.cgst, doc.sgst, doc.igst)) else None
    )
    if line_sum is not None and doc.subtotal is not None:
        ok = _close(line_sum, doc.subtotal, tol) or _close(line_sum - (doc.discount_total or 0), doc.subtotal, tol)
        check(ok, "subtotal", "lines_vs_subtotal", f"line items sum to {line_sum:.2f} but subtotal is {doc.subtotal:.2f}", "error")
    if doc.total is not None:
        base = doc.subtotal if doc.subtotal is not None else line_sum
        if base is not None:
            extras = (taxes or 0) + (doc.shipping or 0) + (doc.round_off or 0)
            candidates = [base + extras, base - (doc.discount_total or 0) + extras]
            # Some invoices round the total to the rupee without printing a round-off line
            ok = any(_close(c, doc.total, tol) for c in candidates)
            check(ok, "total", "total_mismatch",
                  f"subtotal {base:.2f} + tax {taxes or 0:.2f} + other {(doc.shipping or 0) + (doc.round_off or 0):.2f} "
                  f"- discount {doc.discount_total or 0:.2f} != total {doc.total:.2f}", "critical")
    if doc.tax_total is not None and any(v is not None for v in (doc.cgst, doc.sgst, doc.igst)):
        comp = sum(v or 0 for v in (doc.cgst, doc.sgst, doc.igst, doc.cess))
        check(_close(comp, doc.tax_total, tol), "tax_total", "tax_components", f"CGST+SGST+IGST+cess = {comp:.2f} != tax total {doc.tax_total:.2f}", "error")

    # ---- GST consistency ----
    if doc.cgst is not None or doc.sgst is not None:
        check(doc.cgst is not None and doc.sgst is not None and _close(doc.cgst, doc.sgst, tol), "sgst", "cgst_sgst", "CGST and SGST should be equal", "warning")
        check(not doc.igst, "igst", "mixed_gst", "both IGST and CGST/SGST charged", "warning")
    vg, bg = doc.vendor.gstin, doc.buyer.gstin
    if vg and bg and len(vg) >= 2 and len(bg) >= 2 and (doc.cgst or doc.sgst or doc.igst):
        same_state = vg[:2] == bg[:2]
        check(bool(doc.cgst or doc.sgst) == same_state, "igst", "place_of_supply",
              "intra-state supply should use CGST+SGST; inter-state should use IGST", "warning")

    penalty = sum(PENALTY[i.severity] for i in issues)
    validation_score = max(0.0, 1.0 - penalty)
    score = round(0.8 * validation_score + 0.2 * engine_confidence, 3)
    # Auto-accept only clean results: any critical or error issue means escalate (or human review).
    passed = not any(i.severity in ("critical", "error") for i in issues) and score >= get_settings().accept_confidence
    return ValidationReport(score=score, passed=passed, issues=issues, checks_run=checks)
