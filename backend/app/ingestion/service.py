"""Pull new Outlook messages and turn financial-looking content into Documents."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingestion.graph import GraphClient
from app.models import Email, KeyValue
from app.pipeline.identify import looks_financial_email
from app.pipeline.preprocess import html_to_text
from app.service import SUPPORTED_MIMES, guess_mime, store_document

log = logging.getLogger(__name__)
WATERMARK_KEY = "graph.last_received_at"
MIN_INLINE_IMAGE_BYTES = 30_000  # inline images smaller than this are logos/signatures


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ingest_new_messages(session: Session, client: GraphClient | None = None) -> dict:
    settings = get_settings()
    client = client or GraphClient()
    wm = session.get(KeyValue, WATERMARK_KEY)
    since = _parse_dt(wm.value) if wm else None

    stats = {"messages": 0, "skipped_existing": 0, "documents": 0, "document_ids": []}
    latest = since
    for msg in client.messages_since(since, settings.graph_max_messages_per_run):
        received = _parse_dt(msg["receivedDateTime"])
        latest = max(latest, received) if latest else received
        if session.scalar(select(Email.id).where(Email.graph_message_id == msg["id"])):
            stats["skipped_existing"] += 1
            continue
        stats["messages"] += 1
        body = (msg.get("body") or {}).get("content") or ""
        email = Email(
            graph_message_id=msg["id"], mailbox=client.mailbox, subject=msg.get("subject"),
            sender=((msg.get("from") or {}).get("emailAddress") or {}).get("address"),
            received_at=received, body_preview=msg.get("bodyPreview"),
        )
        session.add(email)
        session.flush()

        created = []
        if msg.get("hasAttachments"):
            for att in client.attachments(msg["id"]):
                mime = guess_mime(att.get("name") or "", att.get("contentType"))
                if mime not in SUPPORTED_MIMES:
                    log.info("skip attachment %s (%s)", att.get("name"), mime)
                    continue
                if att.get("isInline") and mime.startswith("image/") and len(att["content"]) < MIN_INLINE_IMAGE_BYTES:
                    continue
                doc, new = store_document(session, att["content"], att.get("name") or "attachment", mime, email=email, source="email")
                if new:
                    created.append(doc.id)

        # Invoice/quotation written in the email body itself
        if not created:
            body_text = html_to_text(body) if (msg.get("body") or {}).get("contentType") == "html" else body
            if looks_financial_email(msg.get("subject") or "", body_text):
                doc, new = store_document(session, body.encode(), f"{(msg.get('subject') or 'email')[:80]}.html",
                                          "text/html", email=email, source="email_body")
                if new:
                    created.append(doc.id)

        stats["documents"] += len(created)
        stats["document_ids"].extend(created)
        session.commit()

    if latest and latest != since:
        session.merge(KeyValue(key=WATERMARK_KEY, value=latest.isoformat()))
        session.commit()
    return stats
