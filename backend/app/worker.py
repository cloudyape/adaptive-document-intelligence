"""In-process background work: a small thread pool for document processing and an Outlook poller.

For higher volume, swap the executor for a real queue (Celery/RQ/Arq); `process_document` is
idempotent per document, so the call sites don't change.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.config import get_settings
from app.db import session_scope

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="adi-worker")
_stop = threading.Event()


def _process(doc_id: int) -> None:
    from app.service import process_document

    try:
        with session_scope() as session:
            doc = process_document(session, doc_id)
            log.info("document %s -> %s via %s ($%.4f)", doc_id, doc.status, doc.final_engine, doc.total_cost_usd)
    except Exception:  # noqa: BLE001
        log.exception("worker failed for document %s", doc_id)


def enqueue(doc_id: int) -> None:
    _executor.submit(_process, doc_id)


def run_ingestion_once() -> dict:
    from app.ingestion.service import ingest_new_messages

    with session_scope() as session:
        stats = ingest_new_messages(session)
    for doc_id in stats["document_ids"]:
        enqueue(doc_id)
    return stats


def _poll_loop() -> None:
    interval = get_settings().graph_poll_seconds
    while not _stop.is_set():
        try:
            stats = run_ingestion_once()
            if stats["messages"]:
                log.info("ingested %s messages, %s documents", stats["messages"], stats["documents"])
        except Exception:  # noqa: BLE001
            log.exception("Outlook ingestion run failed")
        _stop.wait(interval)


def start_poller() -> None:
    s = get_settings()
    if not s.graph_poll_enabled:
        return
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_mailbox]):
        log.warning("ADI_GRAPH_POLL_ENABLED is true but Outlook isn't configured (ADI_GRAPH_* missing); poller not started")
        return
    threading.Thread(target=_poll_loop, name="adi-graph-poller", daemon=True).start()
    log.info("Outlook poller started")


def stop() -> None:
    _stop.set()
    _executor.shutdown(wait=False, cancel_futures=True)
