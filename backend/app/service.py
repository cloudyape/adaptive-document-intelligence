"""Persistence-aware pipeline: store a file, process a document, apply human review."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Document, Email, ExtractionAttempt, FinancialRecord, LineItemRow, ReviewAction
from app.pipeline import complexity as complexity_mod
from app.pipeline.identify import identify
from app.pipeline.orchestrator import Orchestrator
from app.pipeline.preprocess import analyze
from app.pipeline.validation import validate
from app.schemas import FINANCIAL_TYPES, DocStatus, DocType, ExtractedDocument

log = logging.getLogger(__name__)

SUPPORTED_MIMES = {"application/pdf", "image/png", "image/jpeg", "image/jpg", "image/tiff", "image/webp", "image/bmp",
                   "image/gif", "text/plain", "text/html"}


def guess_mime(filename: str, declared: str | None = None) -> str:
    if declared and declared != "application/octet-stream":
        return declared.split(";")[0].strip().lower()
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def store_document(
    session: Session, data: bytes, filename: str, mime_type: str, *, email: Email | None = None, source: str = "upload"
) -> tuple[Document, bool]:
    """Save bytes to storage and create a Document row. Returns (document, created). Dedupes by SHA-256."""
    sha = hashlib.sha256(data).hexdigest()
    existing = session.scalar(select(Document).where(Document.sha256 == sha))
    if existing:
        return existing, False
    storage = get_settings().storage_dir
    storage.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix or mimetypes.guess_extension(mime_type) or ""
    path = storage / f"{sha}{ext.lower()}"
    path.write_bytes(data)
    doc = Document(
        email=email, source=source, filename=filename[:500], mime_type=mime_type, sha256=sha,
        storage_path=str(path), size_bytes=len(data), status=DocStatus.RECEIVED,
    )
    session.add(doc)
    session.flush()
    return doc, True


def process_document(session: Session, doc_id: int, orchestrator: Orchestrator | None = None) -> Document:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise ValueError(f"document {doc_id} not found")
    doc.status, doc.error = DocStatus.PROCESSING, None
    session.commit()

    try:
        if doc.mime_type not in SUPPORTED_MIMES:
            # Unsupported formats still go to the document-AI tier rather than being dropped.
            log.info("unsupported mime %s for %s; routing as difficult", doc.mime_type, doc.filename)
        ctx = analyze(Path(doc.storage_path), doc.mime_type, doc.filename)
        doc.features = ctx.features()

        subject = doc.email.subject if doc.email else ""
        ident = identify(ctx.text, doc.filename, subject or "")
        doc.doc_type, doc.identification_confidence = ident.doc_type.value, ident.confidence
        if ident.doc_type not in FINANCIAL_TYPES and ident.confidence >= 0.6:
            doc.status = DocStatus.IGNORED
            doc.complexity_reasons = [f"identified as non-financial ({ident.method}, {ident.confidence})"]
            session.commit()
            return doc

        cx = complexity_mod.classify(ctx)
        doc.complexity, doc.complexity_score, doc.complexity_reasons = cx.level.value, cx.score, cx.reasons

        hint = ident.doc_type if ident.doc_type in FINANCIAL_TYPES else None
        outcome = (orchestrator or Orchestrator()).run(ctx, cx.level, hint)

        doc.attempts.clear()
        session.flush()
        for a in outcome.attempts:
            r = a.result
            doc.attempts.append(ExtractionAttempt(
                engine=a.engine, tier=a.tier, model=r.model if r else None, duration_ms=a.duration_ms,
                cost_usd=a.cost, input_tokens=r.input_tokens if r else 0, output_tokens=r.output_tokens if r else 0,
                success=r is not None, skipped=a.skipped, accepted=a is outcome.best and outcome.accepted,
                confidence=a.validation.score if a.validation else None,
                result=r.document.model_dump(mode="json") if r else None,
                validation=a.validation.to_dict() if a.validation else None,
                error=a.error or ("; ".join(r.warnings) if r and r.warnings else None),
            ))
        doc.total_cost_usd = outcome.total_cost

        if outcome.best and outcome.best.result:
            best = outcome.best
            doc.final_engine, doc.confidence = best.engine, best.score
            _upsert_record(session, doc, best.result.document)
            doc.status = DocStatus.EXTRACTED if outcome.accepted else DocStatus.NEEDS_REVIEW
            if not outcome.accepted:
                doc.error = "; ".join(best.validation.messages()[:5]) if best.validation else None
            dup = find_duplicate(session, doc)
            if dup:
                doc.status = DocStatus.NEEDS_REVIEW
                doc.error = f"possible duplicate of document #{dup}" + (f"; {doc.error}" if doc.error else "")
        else:
            doc.status = DocStatus.NEEDS_REVIEW
            doc.error = "all engines failed: " + "; ".join(f"{a.engine}: {a.error}" for a in outcome.attempts if a.error)
    except Exception as exc:  # noqa: BLE001
        log.exception("processing failed for document %s", doc_id)
        session.rollback()
        doc = session.get(Document, doc_id)
        doc.status, doc.error = DocStatus.FAILED, f"{type(exc).__name__}: {exc}"
    session.commit()
    return doc


# Characters OCR commonly confuses, folded to one canonical form for duplicate matching.
_OCR_FOLD = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "S": "5", "B": "8", "Z": "2", "G": "6"})


def number_key(number: str) -> str:
    """Comparable form of a document number: case-, separator- and OCR-confusion-insensitive."""
    return "".join(ch for ch in number.upper().translate(_OCR_FOLD) if ch.isalnum())


def find_duplicate(session: Session, doc: Document) -> int | None:
    """Another non-rejected document from the same vendor (GSTIN, else name) with the same number."""
    rec = doc.record
    if rec is None or not rec.document_number:
        return None
    session.flush()
    stmt = select(FinancialRecord.document_id, FinancialRecord.document_number).join(Document).where(
        FinancialRecord.document_id != doc.id,
        FinancialRecord.doc_type == rec.doc_type,
        FinancialRecord.document_number.is_not(None),
        Document.status != DocStatus.REJECTED,
    )
    if rec.vendor_gstin:
        stmt = stmt.where(FinancialRecord.vendor_gstin == rec.vendor_gstin)
    elif rec.vendor_name:
        stmt = stmt.where(func.lower(FinancialRecord.vendor_name) == rec.vendor_name.lower())
    else:
        return None
    key = number_key(rec.document_number)
    for other_id, other_number in session.execute(stmt.order_by(FinancialRecord.document_id)):
        if number_key(other_number) == key:
            return other_id
    return None


def _upsert_record(session: Session, doc: Document, data: ExtractedDocument, *, verified_by: str | None = None) -> FinancialRecord:
    rec = doc.record or FinancialRecord(document=doc)
    rec.doc_type = data.document_type.value if data.document_type != DocType.OTHER else (doc.doc_type or "other")
    for f in ("document_number", "document_date", "due_date", "valid_until", "po_number", "subtotal",
              "discount_total", "cgst", "sgst", "igst", "tax_total", "total", "payment_terms"):
        setattr(rec, f, getattr(data, f))
    rec.currency = (data.currency or "")[:3] or None
    rec.vendor_name, rec.vendor_gstin = data.vendor.name, (data.vendor.gstin or "")[:15] or None
    rec.buyer_name, rec.buyer_gstin = data.buyer.name, (data.buyer.gstin or "")[:15] or None
    rec.data = data.model_dump(mode="json")
    rec.line_items = [
        LineItemRow(position=i, **li.model_dump()) for i, li in enumerate(data.line_items)
    ]
    if verified_by:
        rec.verified, rec.verified_by, rec.verified_at = True, verified_by, datetime.now(timezone.utc)
    session.add(rec)
    return rec


def review(session: Session, doc_id: int, action: str, reviewer: str | None, comment: str | None,
           corrected: ExtractedDocument | None = None) -> Document:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise ValueError(f"document {doc_id} not found")
    changes = None
    if corrected is not None:
        before = doc.record.data if doc.record else {}
        after = corrected.model_dump(mode="json")
        changes = {k: {"from": before.get(k), "to": v} for k, v in after.items() if before.get(k) != v}
        report = validate(corrected, 1.0)
        doc.confidence = report.score
        _upsert_record(session, doc, corrected, verified_by=reviewer or "reviewer")
    if action == "approve":
        if doc.record is None:
            raise ValueError("nothing to approve: no extracted data")
        # Humans may approve documents whose own arithmetic is wrong, but not records missing essentials.
        missing = [i.message for i in validate(ExtractedDocument.model_validate(doc.record.data)).critical
                   if i.code in ("missing", "not_financial")]
        if missing:
            session.rollback()
            raise ValueError("cannot approve: " + "; ".join(missing))
        doc.status = DocStatus.APPROVED
        doc.record.verified, doc.record.verified_by = True, reviewer or "reviewer"
        doc.record.verified_at = datetime.now(timezone.utc)
    elif action == "reject":
        doc.status = DocStatus.REJECTED
    elif action == "correct":
        doc.status = DocStatus.NEEDS_REVIEW
    session.add(ReviewAction(document=doc, action=action, reviewer=reviewer, comment=comment, changes=changes))
    session.commit()
    return doc
