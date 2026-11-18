# Adaptive Document Intelligence (ADI)

Pulls invoices and quotations from Outlook, decides how hard each one is to extract, and sends it to the
**cheapest engine that can handle it**. Every result is validated, a failed extraction moves up to a
stronger engine, and whatever still fails goes to a person for review. The output is standardized
financial data in PostgreSQL, available through a REST API and a Vue dashboard.

```
Outlook ──Graph──▶ identify ──▶ classify complexity ──▶ route ──▶ validate ──▶ PostgreSQL ──▶ API / dashboard
                  (invoice?      (easy/medium/          │            │  fail
                   quotation?)    difficult)            ▼            ▼
                                             ┌── T1 rules engine ──── free
                                             ├── T2 OCR + Haiku ───── fractions of a cent
                                             ├── T3 Azure DI / Claude Sonnet vision
                                             └── T4 Claude Opus fallback (sees earlier errors)
                                                        │ still failing
                                                        ▼
                                                  human review queue
```

## How routing works

| Complexity | Typical document | First engine | Escalates to |
|---|---|---|---|
| Easy | Digital PDF with a text layer, HTML email body | `rules`: label patterns + pdfplumber tables, $0 | T2 → T3 → T4 |
| Medium | Clean scan or photo, long digital docs | `ocr_llm`: Tesseract text → Claude Haiku (page images if there's no OCR) | T3 → T4 |
| Difficult | Handwriting, poor scans, low OCR confidence, unsupported formats | `azure_di` (if configured), else `claude_vision` (Sonnet reads the PDF/images) | T4 |

- **The complexity score is explainable.** [complexity.py](backend/app/pipeline/complexity.py) adds points for
  features it measures locally: no text layer, OCR confidence, garbled-token ratio, page count, tables, and
  image coverage. Each point is stored with its reason and shown on the dashboard.
- **Validation decides acceptance, not the engine's own confidence.** [validation.py](backend/app/pipeline/validation.py)
  checks required fields, ISO dates, the GSTIN mod-36 checksum, qty × price = amount, line sum = subtotal,
  subtotal − discount + tax + other = total, CGST = SGST, and IGST vs CGST/SGST against the place of supply.
  A result is accepted only with no critical issues and a score ≥ `ADI_ACCEPT_CONFIDENCE` (0.85).
- **Escalation carries context.** The Opus fallback gets the best earlier attempt plus its validation errors.
- **Spend is capped.** Escalation stops at `ADI_MAX_COST_PER_DOCUMENT_USD`. Every attempt's tokens and cost are recorded.
- **Dedupe.** Files are deduplicated by SHA-256. A document with the same vendor and number as an earlier one
  is flagged as a possible duplicate.

## Run it

**With Docker** (PostgreSQL + API + dashboard at http://localhost:8080):

```bash
cp .env.example .env   # add ANTHROPIC_API_KEY and the ADI_GRAPH_* values
docker compose up --build
```

**Local dev** (SQLite, no Docker needed):

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
ADI_DATABASE_URL=sqlite:///./adi-dev.db .venv/bin/uvicorn app.api.main:app --port 8000
```

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173, proxies /api to :8000
```

Generate sample documents to upload (digital invoice, quotation, scanned invoice, a non-financial letter, an HTML email invoice):

```bash
cd backend && .venv/bin/python -m tests.samples ../samples
```

Tests (no paid API calls; the LLM engines are faked):

```bash
cd backend && .venv/bin/python -m pytest -q
```

## Outlook setup (Microsoft Graph)

1. Entra ID → App registrations → New registration.
2. API permissions → Microsoft Graph → **Application** permission `Mail.Read` → Grant admin consent.
   Restrict it to the accounts mailbox with an Exchange
   [application access policy](https://learn.microsoft.com/graph/auth-limit-mailbox-access).
3. Certificates & secrets → create a client secret.
4. Set `ADI_GRAPH_TENANT_ID`, `ADI_GRAPH_CLIENT_ID`, `ADI_GRAPH_CLIENT_SECRET`, and `ADI_GRAPH_MAILBOX`. Then set
   `ADI_GRAPH_POLL_ENABLED=true` to poll, or use **Fetch from Outlook** on the dashboard.

Ingestion keeps a `receivedDateTime` watermark, skips messages it has already seen, ignores small inline
images (logos and signatures), and treats a financial-looking email body as a document when it has no attachment.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/stats` | routing mix, per-engine runs/acceptance/spend, straight-through rate |
| GET | `/api/documents?status=&complexity=&doc_type=&q=` | document list |
| GET | `/api/documents/{id}` | detail: features, complexity reasons, every attempt with validation, review history |
| GET | `/api/documents/{id}/file` | original file |
| POST | `/api/documents/upload` | multipart `files` (manual intake) |
| POST | `/api/documents/{id}/reprocess` | run the pipeline again |
| POST | `/api/documents/{id}/review` | `{action: approve\|reject\|correct, reviewer, comment, data?}` |
| GET | `/api/review-queue` | needs_review + failed |
| GET | `/api/records?vendor=&gstin=&doc_type=&date_from=&date_to=&verified_only=` | standardized output for ERP/accounting |
| POST | `/api/ingest/run` | pull Outlook now |

Interactive docs are at `/docs`.

## Layout

```
backend/app/
  ingestion/      graph.py (Graph client, throttling, large attachments), service.py (email → documents)
  pipeline/       preprocess.py (text layer, OCR, features), identify.py, complexity.py,
                  engines/{rules,claude,azure_di}.py, validation.py, orchestrator.py, llm.py
  service.py      persistence: store, process, review, duplicate detection
  api/main.py     FastAPI
frontend/src/     Vue 3 + Tailwind v4: Overview, Documents, Document detail/review, Review queue, Records
```

## Known limits / next steps

- Tables are created with `create_all` on startup. Add Alembic before the first production schema change.
- Processing runs on an in-process thread pool. Move it to a real queue (Celery, RQ, or Arq) to run several API replicas.
- DOCX and XLSX attachments aren't parsed yet. They're skipped at ingestion.
- Rules-engine heuristics are tuned for Indian GST invoices. Other regions mostly escalate to the LLM tiers.
- Thresholds in `complexity.py` and `ADI_ACCEPT_CONFIDENCE` should be calibrated on a labelled set of your real documents.
