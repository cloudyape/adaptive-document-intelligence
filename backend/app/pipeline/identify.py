"""Is this a financial document, and which kind?

Cheap keyword scoring over filename, email subject and document text decides most cases.
Only when the score is ambiguous do we ask a small LLM (Haiku) to triage.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel

from app.config import get_settings
from app.pipeline import llm
from app.schemas import DocType

log = logging.getLogger(__name__)

# (pattern, weight). Patterns are matched case-insensitively.
KEYWORDS: dict[DocType, list[tuple[str, float]]] = {
    DocType.INVOICE: [
        (r"\btax\s+invoice\b", 3), (r"\binvoice\s*(no|number|#|date)\b", 3), (r"\binvoice\b", 1.5),
        (r"\bbill\s+(no|of supply)\b", 2), (r"\bamount\s+due\b", 1), (r"\bgstin\b", 1), (r"\birn\b", 1),
    ],
    DocType.QUOTATION: [
        (r"\bquotation\b", 3), (r"\bquote\s*(no|number|#|ref)\b", 3), (r"\bestimate\b", 1.5),
        (r"\bvalid\s+(until|till|for)\b", 1.5), (r"\bquote\b", 1), (r"\boffer\b", 0.5),
    ],
    DocType.PROFORMA: [(r"\bpro[\s-]?forma\b", 4)],
    DocType.CREDIT_NOTE: [(r"\bcredit\s+note\b", 4), (r"\bdebit\s+note\b", 3)],
    DocType.PURCHASE_ORDER: [(r"\bpurchase\s+order\b", 4), (r"\bp\.?o\.?\s*(no|number|#)\b", 1.5)],
}
FINANCIAL_SIGNALS = [r"\btotal\b", r"\bqty\b|\bquantity\b", r"\brate\b|\bunit\s+price\b", r"\bcgst|sgst|igst|gst|vat\b", r"₹|\brs\.?\b|\binr\b|\$"]
NEGATIVE = [r"\bnewsletter\b", r"\bunsubscribe\b", r"\bresume\b|\bcurriculum vitae\b", r"\bpayslip\b|\bsalary slip\b"]


@dataclass
class Identification:
    doc_type: DocType
    confidence: float
    method: str  # keywords | llm
    scores: dict[str, float]


class _Triage(BaseModel):
    document_type: DocType
    confidence: float


def _score(text: str, weight: float, scores: dict[DocType, float]) -> None:
    for doc_type, patterns in KEYWORDS.items():
        for pat, w in patterns:
            if re.search(pat, text, re.I):
                scores[doc_type] += w * weight


def identify(text: str, filename: str = "", subject: str = "") -> Identification:
    scores = {t: 0.0 for t in KEYWORDS}
    _score(text[:6000], 1.0, scores)  # header area carries the title
    _score(filename.replace("_", " ").replace("-", " "), 1.5, scores)
    _score(subject, 0.75, scores)

    financial = sum(1 for p in FINANCIAL_SIGNALS if re.search(p, text, re.I))
    negative = sum(1 for p in NEGATIVE if re.search(p, text, re.I))
    best, best_score = max(scores.items(), key=lambda kv: kv[1])
    ordered = sorted(scores.values(), reverse=True)
    margin = ordered[0] - ordered[1]
    raw = {k.value: round(v, 2) for k, v in scores.items()} | {"financial_signals": financial, "negative": negative}

    if best_score >= 3 and financial >= 2 and margin >= 1.5 and negative == 0:
        conf = min(0.99, 0.6 + 0.05 * best_score + 0.05 * financial)
        return Identification(best, round(conf, 2), "keywords", raw)
    if best_score == 0 and financial <= 1:
        return Identification(DocType.OTHER, 0.8 if text.strip() else 0.3, "keywords", raw)

    # Ambiguous: ask a small model if available, otherwise take the keyword best guess.
    settings = get_settings()
    if settings.llm_enabled and llm.available() and text.strip():
        try:
            triage, _usage = llm.parse(
                model=settings.model_classifier,
                system="You triage business documents. Decide which financial document type this is. "
                "Use 'other' for anything that is not an invoice, quotation, proforma invoice, credit/debit note, "
                "or purchase order (e.g. statements, reminders, marketing, contracts).",
                content=[{"type": "text", "text": f"Filename: {filename}\nEmail subject: {subject}\n\n{text[:4000]}"}],
                output_model=_Triage,
                max_tokens=512,
            )
            return Identification(triage.document_type, round(triage.confidence, 2), "llm", raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM triage failed, using keyword guess: %s", exc)

    if best_score > 0:
        return Identification(best, round(min(0.7, 0.3 + 0.05 * best_score), 2), "keywords", raw)
    return Identification(DocType.OTHER, 0.4, "keywords", raw)


def looks_financial_email(subject: str, body: str) -> bool:
    """Fast pre-filter for email bodies without attachments."""
    blob = f"{subject}\n{body[:5000]}"
    return any(re.search(p, blob, re.I) for t in KEYWORDS for p, w in KEYWORDS[t] if w >= 3) and sum(
        1 for p in FINANCIAL_SIGNALS if re.search(p, blob, re.I)
    ) >= 2
