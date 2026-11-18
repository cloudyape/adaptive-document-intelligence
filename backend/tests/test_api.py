"""End-to-end through the HTTP API: upload -> process -> records -> review, plus Graph ingestion."""
import base64

import pytest
from fastapi.testclient import TestClient

from app import worker
from app.db import session_scope
from app.ingestion.service import ingest_new_messages
from app.models import Document


@pytest.fixture()
def client(monkeypatch):
    from app.service import process_document

    def run_inline(doc_id):  # process synchronously so tests are deterministic
        with session_scope() as s:
            process_document(s, doc_id)

    monkeypatch.setattr(worker, "enqueue", run_inline)
    from app.api.main import app

    with TestClient(app) as c:
        yield c


def upload(client, path, mime):
    r = client.post("/api/documents/upload", files={"files": (path.name, path.read_bytes(), mime)})
    assert r.status_code == 200
    return r.json()["documents"][0]


def test_upload_extract_and_query(client, sample_dir):
    d = upload(client, sample_dir / "invoice_digital.pdf", "application/pdf")
    doc = client.get(f"/api/documents/{d['id']}").json()
    assert doc["status"] == "extracted" and doc["complexity"] == "easy" and doc["final_engine"] == "rules"
    assert doc["total_cost_usd"] == 0 and doc["record"]["data"]["total"] == 70800.0
    assert len(doc["attempts"]) == 1 and doc["attempts"][0]["accepted"]

    # duplicate upload is detected by content hash
    assert upload(client, sample_dir / "invoice_digital.pdf", "application/pdf")["duplicate"]

    recs = client.get("/api/records", params={"gstin": "27AAPFU0939F1ZV"}).json()["items"]
    assert any(r["document_number"] == "SIS/2025-26/0142" for r in recs)
    assert client.get(f"/api/documents/{d['id']}/file").status_code == 200


def test_non_financial_is_ignored(client, sample_dir):
    d = upload(client, sample_dir / "letter.pdf", "application/pdf")
    assert client.get(f"/api/documents/{d['id']}").json()["status"] == "ignored"


def test_scanned_without_ocr_or_azure_goes_to_review_then_human_corrects(client, sample_dir):
    d = upload(client, sample_dir / "invoice_scanned.pdf", "application/pdf")
    doc = client.get(f"/api/documents/{d['id']}").json()
    # No OCR binary on the test host and no Azure configured -> every engine is skipped -> human review
    assert doc["status"] == "needs_review"
    assert d["id"] in [x["id"] for x in client.get("/api/review-queue").json()["items"]]

    r = client.post(f"/api/documents/{d['id']}/review", json={"action": "approve", "data": {"document_type": "invoice"}})
    assert r.status_code == 400 and "vendor name missing" in r.json()["detail"]

    corrected = {
        "document_type": "invoice", "document_number": "SIS/2025-26/0142", "document_date": "2025-08-14",
        "currency": "INR", "vendor": {"name": "Sharma Industrial Supplies Pvt Ltd", "gstin": "27AAPFU0939F1ZV"},
        "buyer": {"name": "Tutorlabs Learning Solutions LLP"},
        "line_items": [{"description": "Chairs", "quantity": 10, "unit_price": 6000, "amount": 60000}],
        "subtotal": 60000, "igst": 10800, "tax_total": 10800, "total": 70800,
    }
    r = client.post(f"/api/documents/{d['id']}/review", json={"action": "approve", "reviewer": "qa@example.com", "data": corrected})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved" and body["record"]["verified"] and body["reviews"][0]["changes"]


def test_stats(client, sample_dir):
    upload(client, sample_dir / "quotation_digital.pdf", "application/pdf")
    s = client.get("/api/stats").json()
    assert s["documents_total"] >= 1 and "easy" in s["by_complexity"]
    assert any(e["engine"] == "rules" for e in s["engines"])


class FakeGraph:
    mailbox = "accounts@example.com"

    def __init__(self, pdf: bytes, html: str):
        self.pdf, self.html = pdf, html

    def messages_since(self, since, limit):
        yield {"id": "m1", "subject": "Invoice SIS/0142", "from": {"emailAddress": {"address": "billing@vendor.in"}},
               "receivedDateTime": "2025-09-10T10:00:00Z", "hasAttachments": True, "bodyPreview": "Please find attached",
               "body": {"contentType": "html", "content": "<p>Please find attached</p>"}}
        yield {"id": "m2", "subject": "Invoice QP-7781", "from": {"emailAddress": {"address": "print@quick.in"}},
               "receivedDateTime": "2025-09-11T10:00:00Z", "hasAttachments": False, "bodyPreview": "",
               "body": {"contentType": "html", "content": self.html}}
        yield {"id": "m3", "subject": "Lunch?", "from": {"emailAddress": {"address": "a@b.c"}},
               "receivedDateTime": "2025-09-12T10:00:00Z", "hasAttachments": False, "bodyPreview": "",
               "body": {"contentType": "text", "content": "Lunch at 1?"}}

    def attachments(self, message_id):
        yield {"@odata.type": "#microsoft.graph.fileAttachment", "name": "logo.png", "contentType": "image/png",
               "isInline": True, "content": b"\x89PNG tiny"}
        yield {"@odata.type": "#microsoft.graph.fileAttachment", "name": "INV-0142.pdf", "contentType": "application/pdf",
               "isInline": False, "contentBytes": base64.b64encode(self.pdf).decode(), "content": self.pdf}


def test_graph_ingestion(client, sample_dir):
    from tests.samples import INVOICE_EMAIL_HTML, invoice_pdf

    # Slightly different bytes than earlier tests so the hash dedupe doesn't hide it
    pdf = invoice_pdf() + b"\n%ingest"
    with session_scope() as s:
        stats = ingest_new_messages(s, FakeGraph(pdf, INVOICE_EMAIL_HTML.replace("QP-7781", "QP-7782")))
    assert stats["messages"] == 3 and stats["documents"] == 2  # inline logo + lunch email skipped
    with session_scope() as s:
        stats2 = ingest_new_messages(s, FakeGraph(pdf, INVOICE_EMAIL_HTML))
        assert stats2["skipped_existing"] == 3 and stats2["documents"] == 0
        sources = {d.source for d in s.query(Document).filter(Document.id.in_(stats["document_ids"]))}
    assert sources == {"email", "email_body"}


def test_duplicate_invoice_is_flagged(client, sample_dir):
    from tests.samples import invoice_pdf

    first = upload(client, sample_dir / "invoice_digital.pdf", "application/pdf")  # may already exist from earlier test
    r = client.post("/api/documents/upload", files={"files": ("copy-of-invoice.pdf", invoice_pdf() + b"\n%copy", "application/pdf")})
    dup = client.get(f"/api/documents/{r.json()['documents'][0]['id']}").json()
    assert dup["status"] == "needs_review" and f"duplicate of document #{first['id']}" in dup["error"]
