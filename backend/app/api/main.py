"""REST API for the dashboard and downstream systems (ERP / accounting)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import Integer, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import worker
from app.config import get_settings
from app.db import get_session, init_db
from app.ingestion.graph import GraphNotConfigured
from app.models import Document, ExtractionAttempt, FinancialRecord
from app.pipeline.preprocess import _tesseract_available
from app.schemas import DocStatus, ExtractedDocument
from app.service import guess_mime, review, store_document

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    worker.start_poller()
    yield
    worker.stop()


app = FastAPI(title="Dealer Quote Parsing", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


# ---------- serializers ----------
def doc_summary(d: Document) -> dict:
    rec = d.record
    return {
        "id": d.id, "filename": d.filename, "mime_type": d.mime_type, "source": d.source, "status": d.status,
        "doc_type": d.doc_type, "complexity": d.complexity, "complexity_score": d.complexity_score,
        "final_engine": d.final_engine, "confidence": d.confidence, "total_cost_usd": d.total_cost_usd,
        "error": d.error, "created_at": d.created_at, "updated_at": d.updated_at,
        "email_subject": d.email.subject if d.email else None, "email_sender": d.email.sender if d.email else None,
        "vendor_name": rec.vendor_name if rec else None, "document_number": rec.document_number if rec else None,
        "document_date": rec.document_date if rec else None, "total": rec.total if rec else None,
        "currency": rec.currency if rec else None,
    }


def doc_detail(d: Document) -> dict:
    return doc_summary(d) | {
        "identification_confidence": d.identification_confidence,
        "complexity_reasons": d.complexity_reasons, "features": d.features, "size_bytes": d.size_bytes,
        "email": {"subject": d.email.subject, "sender": d.email.sender, "received_at": d.email.received_at,
                  "body_preview": d.email.body_preview} if d.email else None,
        "record": {"id": d.record.id, "data": d.record.data, "verified": d.record.verified,
                   "verified_by": d.record.verified_by, "verified_at": d.record.verified_at} if d.record else None,
        "attempts": [{
            "id": a.id, "engine": a.engine, "tier": a.tier, "model": a.model, "duration_ms": a.duration_ms,
            "cost_usd": a.cost_usd, "input_tokens": a.input_tokens, "output_tokens": a.output_tokens,
            "success": a.success, "accepted": a.accepted, "skipped": a.skipped, "confidence": a.confidence,
            "validation": a.validation, "error": a.error, "result": a.result,
        } for a in d.attempts],
        "reviews": [{"action": r.action, "reviewer": r.reviewer, "comment": r.comment, "changes": r.changes,
                     "created_at": r.created_at} for r in d.reviews],
    }


def _load(session: Session, doc_id: int) -> Document:
    d = session.scalar(select(Document).where(Document.id == doc_id).options(
        selectinload(Document.attempts), selectinload(Document.record), selectinload(Document.reviews),
        selectinload(Document.email)))
    if d is None:
        raise HTTPException(404, "document not found")
    return d


# ---------- endpoints ----------
@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
def config() -> dict:
    s = get_settings()
    return {
        "engines": {
            "rules": True,
            "ocr": _tesseract_available(),
            "azure_di": bool(s.azure_di_endpoint and s.azure_di_key),
        },
        "azure": {
            "read_enabled": s.azure_di_read_enabled, "highres_fallback": s.azure_di_highres_fallback,
            "cost_per_page": {"read": s.azure_di_read_cost_per_page, "invoice": s.azure_di_invoice_cost_per_page,
                              "highres_addon": s.azure_di_highres_cost_per_page},
        },
        "graph_configured": bool(s.graph_tenant_id and s.graph_client_id and s.graph_client_secret and s.graph_mailbox),
        "graph_poll_enabled": s.graph_poll_enabled,
        "accept_confidence": s.accept_confidence,
        "max_cost_per_document_usd": s.max_cost_per_document_usd,
    }


@app.get("/api/stats")
def stats(session: Session = Depends(get_session)) -> dict:
    by = lambda col: {k or "unknown": v for k, v in session.execute(select(col, func.count()).group_by(col)).all()}  # noqa: E731
    engine_rows = session.execute(
        select(ExtractionAttempt.engine, ExtractionAttempt.tier, func.count(), func.sum(ExtractionAttempt.cost_usd),
               func.sum(cast(ExtractionAttempt.accepted, Integer)))
        .where(ExtractionAttempt.skipped.is_(False))
        .group_by(ExtractionAttempt.engine, ExtractionAttempt.tier)
    ).all()
    total_cost = session.scalar(select(func.coalesce(func.sum(Document.total_cost_usd), 0.0)))
    processed = session.scalar(select(func.count()).where(Document.complexity.is_not(None)))
    cost_by_complexity = {k or "unknown": round(v or 0, 4) for k, v in session.execute(
        select(Document.complexity, func.sum(Document.total_cost_usd)).group_by(Document.complexity)).all()}
    auto = session.scalar(select(func.count()).where(Document.status == DocStatus.EXTRACTED))
    return {
        "documents_total": session.scalar(select(func.count()).select_from(Document)),
        "by_status": by(Document.status), "by_complexity": by(Document.complexity), "by_doc_type": by(Document.doc_type),
        "by_final_engine": by(Document.final_engine),
        "engines": [{"engine": e, "tier": t, "runs": n, "cost_usd": round(c or 0, 4), "accepted": int(a or 0)}
                    for e, t, n, c, a in sorted(engine_rows, key=lambda r: r[1])],
        "total_cost_usd": round(total_cost or 0, 4),
        "avg_cost_per_document_usd": round((total_cost or 0) / processed, 5) if processed else 0,
        "cost_by_complexity": cost_by_complexity,
        "straight_through_rate": round(auto / processed, 3) if processed else None,
    }


@app.get("/api/documents")
def list_documents(
    status: str | None = None, complexity: str | None = None, doc_type: str | None = None, q: str | None = None,
    limit: int = Query(50, le=500), offset: int = 0, session: Session = Depends(get_session),
) -> dict:
    stmt = select(Document).outerjoin(FinancialRecord).options(selectinload(Document.record), selectinload(Document.email))
    if status:
        stmt = stmt.where(Document.status.in_(status.split(",")))
    if complexity:
        stmt = stmt.where(Document.complexity == complexity)
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Document.filename.ilike(like), FinancialRecord.vendor_name.ilike(like),
                              FinancialRecord.document_number.ilike(like), FinancialRecord.vendor_gstin.ilike(like)))
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.scalars(stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "items": [doc_summary(d) for d in rows]}


@app.get("/api/review-queue")
def review_queue(session: Session = Depends(get_session)) -> dict:
    return list_documents(status=f"{DocStatus.NEEDS_REVIEW},{DocStatus.FAILED}", limit=200, session=session)


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int, session: Session = Depends(get_session)) -> dict:
    return doc_detail(_load(session, doc_id))


@app.get("/api/documents/{doc_id}/file")
def get_document_file(doc_id: int, session: Session = Depends(get_session)) -> FileResponse:
    d = _load(session, doc_id)
    path = Path(d.storage_path)
    if not path.exists():
        raise HTTPException(410, "file no longer in storage")
    return FileResponse(path, media_type=d.mime_type, filename=d.filename, content_disposition_type="inline")


@app.post("/api/documents/upload")
async def upload(files: list[UploadFile] = File(...), session: Session = Depends(get_session)) -> dict:
    out = []
    for f in files:
        data = await f.read()
        doc, created = store_document(session, data, f.filename or "upload", guess_mime(f.filename or "", f.content_type))
        session.commit()
        if created:
            worker.enqueue(doc.id)
        out.append({"id": doc.id, "filename": doc.filename, "duplicate": not created})
    return {"documents": out}


@app.post("/api/documents/{doc_id}/reprocess")
def reprocess(doc_id: int, session: Session = Depends(get_session)) -> dict:
    d = _load(session, doc_id)
    d.status = DocStatus.RECEIVED
    session.commit()
    worker.enqueue(doc_id)
    return {"queued": doc_id}


class ReviewIn(BaseModel):
    action: str  # approve | reject | correct
    reviewer: str | None = None
    comment: str | None = None
    data: ExtractedDocument | None = None


@app.post("/api/documents/{doc_id}/review")
def submit_review(doc_id: int, body: ReviewIn, session: Session = Depends(get_session)) -> dict:
    if body.action not in {"approve", "reject", "correct"}:
        raise HTTPException(422, "action must be approve, reject or correct")
    try:
        review(session, doc_id, body.action, body.reviewer, body.comment, body.data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.expire_all()
    return doc_detail(_load(session, doc_id))


@app.get("/api/records")
def list_records(
    vendor: str | None = None, gstin: str | None = None, doc_type: str | None = None,
    date_from: str | None = None, date_to: str | None = None, verified_only: bool = False,
    limit: int = Query(100, le=1000), offset: int = 0, session: Session = Depends(get_session),
) -> dict:
    """Standardized data for downstream systems."""
    stmt = select(FinancialRecord).join(Document).options(selectinload(FinancialRecord.line_items))
    stmt = stmt.where(Document.status.in_([DocStatus.EXTRACTED, DocStatus.APPROVED]))
    if vendor:
        stmt = stmt.where(FinancialRecord.vendor_name.ilike(f"%{vendor}%"))
    if gstin:
        stmt = stmt.where(FinancialRecord.vendor_gstin == gstin.upper())
    if doc_type:
        stmt = stmt.where(FinancialRecord.doc_type == doc_type)
    if date_from:
        stmt = stmt.where(FinancialRecord.document_date >= date_from)
    if date_to:
        stmt = stmt.where(FinancialRecord.document_date <= date_to)
    if verified_only:
        stmt = stmt.where(FinancialRecord.verified.is_(True))
    rows = session.scalars(stmt.order_by(FinancialRecord.document_date.desc()).limit(limit).offset(offset)).all()
    return {"items": [{"id": r.id, "document_id": r.document_id, "verified": r.verified, **r.data} for r in rows]}


@app.post("/api/ingest/run")
def ingest_now() -> dict:
    try:
        return worker.run_ingestion_once()
    except GraphNotConfigured as exc:
        raise HTTPException(400, str(exc)) from exc
