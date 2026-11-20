"""Merged PDFs: one PDF holding several quotes is split and each quote is extracted on its own."""
import pytest
from fastapi.testclient import TestClient

from app import worker
from app.db import session_scope
from app.pipeline.split import segment

TERMS = "Terms and Conditions:\n" + "\n".join(
    f"{i}. The purchaser agrees to the warranty, arbitration and limitation of liability provisions stated herein "
    "and acknowledges receipt of the manufacturer limited warranty." for i in range(1, 15))


def ranges(texts):
    return [(s.start, s.end) for s in segment(texts)]


def test_quote_number_on_every_page():
    fce = lambda n, extra="": f"Salesperson: 404 N Frontage Rd\nPlant City, FL\nQUOTE#: {n} DATE: 06/22/2026\n{extra}"  # noqa: E731
    pages = [fce(125453, "KUBOTA SVL75 $70,000.00 $70,000.00"), fce(125453, "TOTALAMOUNTDUE: $74,550.00"), fce(125453, "SPECIFICATIONS"),
             fce(128508, "KUBOTA KX040 $60,000.00 $60,000.00"), fce(128508, "TotalCashPrice: $63,900.00")]
    assert ranges(pages) == [(1, 3), (4, 5)]


def test_page_markers_and_terms_pages():
    crystal = lambda p, n: f"PAGE {p}\nCRYSTAL TRACTOR & EQUIPMENT\nQUOTE NUMBER {n}\nKUBOTA L3902 $30,000.00 $30,000.00"  # noqa: E731
    order = "SALES ORDER\n9066 Hwy 301 South\nKUBOTA BX2380 $20,000.00\nTotal $21,400.00"
    pages = [crystal(1, "Q-0624-044038"), crystal(2, "Q-0624-044038") + "\nGRAND TOTAL $31,950.00",
             order, TERMS, order, TERMS, "", crystal(1, "Q-0629-044311"), crystal(2, "Q-0629-044311")]
    assert ranges(pages) == [(1, 2), (3, 4), (5, 7), (8, 9)]


def test_new_letterhead_without_numbers_and_single_quote_pdfs():
    mason = "Mason Tractor Company\n1275 Carrollton Villa Rica\nQuote\nKUBOTA SCL1000 $35,505.00\nTOTAL $38,803.42"
    nash = "Nash Equipment\n1190 NC Hwy 53 W\nBurgaw, NC 28425\nKubota SVL75-3HW $64,500.00\nTOTAL $68,370.00"
    assert ranges([mason, nash, mason]) == [(1, 1), (2, 2), (3, 3)]
    # A 3-page quote whose later pages carry no number or marker stays one quote
    assert ranges([mason.replace("TOTAL $38,803.42", ""), "Options continued\nAP-SSB72 bucket $2,000.00", "Subtotal $37,505.00\nTOTAL $40,000.00"]) == [(1, 3)]
    assert ranges(["Invoice\nAcme\nInvoice No: 1\nTotal $5.00"]) == [(1, 1)]


@pytest.fixture()
def client(monkeypatch):
    from app.service import process_document

    def run_inline(doc_id):
        with session_scope() as s:
            process_document(s, doc_id)  # no enqueue: the quotes of a merged PDF are processed inline

    monkeypatch.setattr(worker, "enqueue", run_inline)
    from app.api.main import app

    with TestClient(app) as c:
        yield c


def test_merged_pdf_is_split_and_every_quote_is_listed(client):
    from tests.samples import merged_quotes_pdf

    r = client.post("/api/documents/upload", files={"files": ("Brad quotes.pdf", merged_quotes_pdf(), "application/pdf")})
    parent_id = r.json()["documents"][0]["id"]
    parent = client.get(f"/api/documents/{parent_id}").json()
    assert parent["status"] == "split" and parent["record"] is None
    kids = parent["children"]
    assert [(k["page_start"], k["page_end"]) for k in kids] == [(1, 2), (3, 4), (5, 7), (8, 8)]

    first, second, again, empty = kids
    assert (first["status"], first["vendor_name"], first["document_number"], first["total"]) == (
        "extracted", "Pinecrest Kubota", "PK-1001", 88275)
    assert (second["status"], second["document_number"], second["total"]) == ("extracted", "RB-2002", 70642)
    assert again["status"] == "duplicate" and f"document #{first['id']}" in again["error"] and "pages 1-2" in again["error"]
    assert empty["status"] in ("needs_review", "failed") and empty["error"]
    assert parent["packet"]["by_status"] == {"extracted": 2, "duplicate": 1, empty["status"]: 1}

    child = client.get(f"/api/documents/{first['id']}").json()
    assert child["parent"]["id"] == parent_id and child["filename"] == "Brad quotes (p1-2).pdf"
    assert client.get(f"/api/documents/{first['id']}/file").content.startswith(b"%PDF")
    queue = {d["id"] for d in client.get("/api/review-queue").json()["items"]}
    assert again["id"] in queue and empty["id"] in queue and first["id"] not in queue
    listed = client.get(f"/api/documents?parent_id={parent_id}").json()["items"]
    assert [d["page_start"] for d in listed] == [1, 3, 5, 8]

    # Reprocessing rebuilds the quotes without duplicating them
    client.post(f"/api/documents/{parent_id}/reprocess")
    assert len(client.get(f"/api/documents/{parent_id}").json()["children"]) == 4
