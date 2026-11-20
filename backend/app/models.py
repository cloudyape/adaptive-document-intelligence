"""PostgreSQL schema (SQLAlchemy 2.0). JSON columns become JSONB on PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    graph_message_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    mailbox: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str | None] = mapped_column(String(320))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body_preview: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    documents: Mapped[list[Document]] = relationship(back_populates="email")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"), index=True)
    # A merged PDF holding several quotes is a "packet": each quote becomes a child document of its pages.
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    page_texts: Mapped[list | None] = mapped_column(JSONType)  # OCR text already read while splitting (reused)
    source: Mapped[str] = mapped_column(String(32), default="email")  # email | email_body | upload
    filename: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    storage_path: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    doc_type: Mapped[str | None] = mapped_column(String(32), index=True)
    identification_confidence: Mapped[float | None] = mapped_column(Float)
    complexity: Mapped[str | None] = mapped_column(String(16), index=True)
    complexity_score: Mapped[float | None] = mapped_column(Float)
    complexity_reasons: Mapped[list | None] = mapped_column(JSONType)
    features: Mapped[dict | None] = mapped_column(JSONType)

    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    final_engine: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    email: Mapped[Email | None] = relationship(back_populates="documents")
    parent: Mapped[Document | None] = relationship(back_populates="children", remote_side="Document.id")
    children: Mapped[list[Document]] = relationship(
        back_populates="parent", order_by="Document.page_start", cascade="all, delete-orphan"
    )
    attempts: Mapped[list[ExtractionAttempt]] = relationship(
        back_populates="document", order_by="ExtractionAttempt.id", cascade="all, delete-orphan"
    )
    record: Mapped[FinancialRecord | None] = relationship(
        back_populates="document", uselist=False, cascade="all, delete-orphan"
    )
    reviews: Mapped[list[ReviewAction]] = relationship(
        back_populates="document", order_by="ReviewAction.id", cascade="all, delete-orphan"
    )


class ExtractionAttempt(Base):
    """One engine run. Every escalation step is recorded for cost + audit."""

    __tablename__ = "extraction_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    engine: Mapped[str] = mapped_column(String(64))
    tier: Mapped[int] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)  # engine not configured / not applicable
    confidence: Mapped[float | None] = mapped_column(Float)
    result: Mapped[dict | None] = mapped_column(JSONType)
    validation: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="attempts")


class FinancialRecord(Base):
    """The standardized, queryable output: one per document."""

    __tablename__ = "financial_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), unique=True)
    doc_type: Mapped[str] = mapped_column(String(32), index=True)
    document_number: Mapped[str | None] = mapped_column(String(128), index=True)
    document_date: Mapped[str | None] = mapped_column(String(10), index=True)
    due_date: Mapped[str | None] = mapped_column(String(10))
    valid_until: Mapped[str | None] = mapped_column(String(10))
    po_number: Mapped[str | None] = mapped_column(String(128))
    currency: Mapped[str | None] = mapped_column(String(3))
    vendor_name: Mapped[str | None] = mapped_column(String(512), index=True)
    vendor_gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    buyer_name: Mapped[str | None] = mapped_column(String(512))
    buyer_gstin: Mapped[str | None] = mapped_column(String(15))
    subtotal: Mapped[float | None] = mapped_column(Float)
    discount_total: Mapped[float | None] = mapped_column(Float)
    cgst: Mapped[float | None] = mapped_column(Float)
    sgst: Mapped[float | None] = mapped_column(Float)
    igst: Mapped[float | None] = mapped_column(Float)
    tax_total: Mapped[float | None] = mapped_column(Float)
    total: Mapped[float | None] = mapped_column(Float)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    margin_amount: Mapped[float | None] = mapped_column(Float)
    margin_percent: Mapped[float | None] = mapped_column(Float)
    trade_in: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONType)  # full ExtractedDocument
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[str | None] = mapped_column(String(320))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    document: Mapped[Document] = relationship(back_populates="record")
    line_items: Mapped[list[LineItemRow]] = relationship(
        back_populates="record", order_by="LineItemRow.position", cascade="all, delete-orphan"
    )


class LineItemRow(Base):
    __tablename__ = "line_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("financial_records.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    series: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(256), index=True)
    options: Mapped[list | None] = mapped_column(JSONType)
    base_price: Mapped[float | None] = mapped_column(Float)
    msrp: Mapped[float | None] = mapped_column(Float)
    margin_amount: Mapped[float | None] = mapped_column(Float)
    margin_percent: Mapped[float | None] = mapped_column(Float)
    hsn_sac: Mapped[str | None] = mapped_column(String(16))
    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    unit_price: Mapped[float | None] = mapped_column(Float)
    discount: Mapped[float | None] = mapped_column(Float)
    tax_rate: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)

    record: Mapped[FinancialRecord] = relationship(back_populates="line_items")


class ReviewAction(Base):
    __tablename__ = "review_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(32))  # approve | reject | correct | reprocess
    reviewer: Mapped[str | None] = mapped_column(String(320))
    comment: Mapped[str | None] = mapped_column(Text)
    changes: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="reviews")


class KeyValue(Base):
    """Small state store (e.g. Graph ingestion watermark)."""

    __tablename__ = "kv_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class PriceFile(Base):
    """An uploaded dealer cost price list. The newest active file is used for margins."""

    __tablename__ = "price_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    uploaded_by: Mapped[str | None] = mapped_column(String(320))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list | None] = mapped_column(JSONType)

    entries: Mapped[list[PriceEntry]] = relationship(back_populates="price_file", cascade="all, delete-orphan")


class PriceEntry(Base):
    __tablename__ = "price_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    price_file_id: Mapped[int] = mapped_column(ForeignKey("price_files.id", ondelete="CASCADE"), index=True)
    model_key: Mapped[str] = mapped_column(String(64), index=True)  # normalised model / part number
    model_number: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    dealer_cost: Mapped[float | None] = mapped_column(Float)
    msrp: Mapped[float | None] = mapped_column(Float)

    price_file: Mapped[PriceFile] = relationship(back_populates="entries")
