# Dealer Quote Parsing

Pulls invoices and quotations from Outlook, decides how hard each one is to extract, and sends it to the
**cheapest engine that can handle it**. Easy and medium documents are handled locally for free. Azure AI
Document Intelligence, the only paid service, is called only when the local engines can't produce a
result that passes validation. Whatever still fails goes to a person for review. The output is standardized
financial data in PostgreSQL, available through a REST API and a Vue dashboard.

```
Outlook ──Graph──▶ identify ──▶ classify complexity ──▶ route ──▶ validate ──▶ PostgreSQL ──▶ API / dashboard
                  (invoice?      (easy/medium/          │            │  fail
                   quotation?)    difficult)            ▼            ▼
                                             ┌── T1 rules on PDF text layer ─────── free (local)
                                             ├── T2 Tesseract OCR + rules ───────── free (local)
                                             ├── T3 Azure DI prebuilt-read + rules ─ cheapest Azure call
                                             ├── T4 Azure DI prebuilt-invoice
                                             └── T5 prebuilt-invoice + high-res OCR, merged with T1-T4
                                                        │ still failing
                                                        ▼
                                                  human review queue
```

## How routing works

| Complexity | Typical document | First engine | Escalates to |
|---|---|---|---|
| Easy | Digital PDF with a text layer, HTML email body | `rules`: label patterns + pdfplumber tables, $0 | T2 → T3 → T4 → T5 |
| Medium | Clean scan or photo, long digital docs | `local_ocr`: Tesseract text → rules, $0 | T3 → T4 → T5 |
| Difficult | Handwriting, poor scans, low OCR confidence, unsupported formats | `azure_invoice`: Azure's prebuilt invoice model | T5 |

- **The complexity score is explainable.** [complexity.py](backend/app/pipeline/complexity.py) adds points for
  features it measures locally: no text layer, OCR confidence, garbled-token ratio, page count, tables, and
  image coverage. Each point is stored with its reason and shown on the dashboard.
- **Validation decides acceptance, not the engine's own confidence.** [validation.py](backend/app/pipeline/validation.py)
  checks required fields, ISO dates, the GSTIN mod-36 checksum, qty × price = amount, line sum = subtotal,
  subtotal − discount + tax + other = total, CGST = SGST, and IGST vs CGST/SGST against the place of supply.
  A result is accepted only with no critical or error issues (warnings are allowed) and a score ≥
  `ADI_ACCEPT_CONFIDENCE` (0.85).
- **Azure output is combined with local rules.** Azure's invoice model has no CGST/SGST/IGST split, no quotation
  validity date, and no GSTIN check. The system runs its India-aware rules over the text Azure recognised and fills those
  gaps at no extra cost. The tier-5 fallback also merges in whatever tiers 1–4 got right.
- **Spend is capped.** Escalation stops at `ADI_MAX_COST_PER_DOCUMENT_USD`, and every attempt's cost is recorded.
  Azure per-page prices are settings (`ADI_AZURE_DI_*_COST_PER_PAGE`). The defaults are placeholders, so set them
  from your Azure pricing.
- **Without Azure configured**, tiers 3–5 are skipped and anything the local engines can't validate goes to review.
- **Dedupe.** Files are deduplicated by SHA-256. A document with the same vendor and number as an earlier one
  is flagged as a possible duplicate.

## Extracted fields

Every document is stored in one standard shape (`backend/app/schemas.py`):

- **Quote / invoice header:** type, number, date, due date, valid until, PO number, currency, vendor and buyer
  (name, address, GSTIN, PAN), payment terms, **Comment / Notes**
- **Per line item (one per quoted machine):** description, **Series**, **Model**, HSN/SAC, quantity, **Base Price**
  (the machine alone), MSRP when printed, **Options** (the machine's listed features, plus separately priced
  attachments with their prices), unit price, tax rate, amount, **Margin By**, **Margin By %**
- **Totals:** subtotal, discount and incentives, sales tax (or CGST/SGST/IGST for Indian documents), fees (doc, title,
  UCC, processing…), trade-in allowance, grand total, **Margin By**, **Margin By %**, and where the margin came from

**How margin is defined.** *Margin By* = selling price − dealer cost. *Margin By %* = that margin ÷ selling price
(margin on sale, not markup on cost). Dealer quotes don't print a margin, so it is calculated from the **dealer cost
price file** uploaded on the *Price files* page (CSV or Excel with "Model / Part #" and "Dealer Cost / Dealer Net"
columns, optional "MSRP"; PDF best-effort; template: [docs/price-file-template.csv](docs/price-file-template.csv)). The newest active file is used, matched by model or part number while
ignoring case and separators. Priced options are included when all of them have a cost; otherwise the margin covers
the machine alone. Uploading a new file recalculates unverified records. A margin printed on the document is never
overwritten, and approved records are left alone. A reviewer can clear both margin fields to recalculate.

The selling price is the unit's pre-tax price (base price + priced options, × quantity); for the whole quote, it is the
sum of those line prices (or the subtotal). When a document does print one of the two margin values, the other is
calculated from it. The same applies when a reviewer types one of them. Validation also accepts a percentage of the base price
alone, and flags margins outside 0–100%, a margin larger than the quote total, and base price + options that don't add
up to the unit price.

**Where the values come from.** Labelled values (`Series:`, `Model:`, `Base Price:`, `Dealer Margin:`, `Margin %:`),
an `Options` / `Optional Equipment` / `Accessories` list, `Remarks` / `Notes` / `Comments` sections, or table columns
with those headings. A single-unit quote written as labels rather than a table gets its line item built from the labels.

**Series** is Kubota's series name: the one printed on the document ("S Series SVL75-3HFWVRCC"), else mapped from
the model prefix (SVL/SSV → S Series, KX → K Series, U → U Series, B/BX/L/LX/MX tractors). Models without a
reliable mapping (SCL, R, RTV, M62) are left blank for a reviewer.

### US dealer quotes

US dealer quotes (Kubota quote tool, sales agreements, wholegood sales orders, Word/Excel quote forms) are read by
patterns rather than per-dealer templates (`backend/app/pipeline/engines/us_quote.py`):

- **Money lines:** a description plus `$` amounts, with the quantity just before the first amount.
- **Machines:** recognised by Kubota model number. The unpriced lines below a machine are its options, and priced
  attachments (buckets, forks, mulchers) become priced options of that machine.
- **Charges:** fees, trade-ins, incentives and discounts are separated from equipment.
- **Totals:** US labels (Invoice Total, Total Selling Price, Finance Subtotal, Sales Tax…), reconciled as
  subtotal + tax + fees − trade-in − incentives.
- **Dates:** month-first.
- **Dealer name:** taken from the header, matched against the dealer's website. Names that only appear in a logo are
  left empty, so the document escalates to Azure (which reads logos) or review rather than taking the customer's
  name.
- **Fillable PDF forms and Excel quotes:** values are read from form fields and from visible, currency-formatted
  cells.

## Run it

**With Docker** (PostgreSQL + API + dashboard at http://localhost:8080):

```bash
cp .env.example .env   # add ADI_AZURE_DI_ENDPOINT / ADI_AZURE_DI_KEY and the ADI_GRAPH_* values
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

Tests (no paid API calls; Azure's REST API is mocked):

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
                  engines/{rules,azure_di}.py, merge.py, validation.py, orchestrator.py
  service.py      persistence: store, process, review, duplicate detection
  api/main.py     FastAPI
frontend/src/     Vue 3 + Tailwind v4: Overview, Documents, Document detail/review, Review queue, Records
```

## Known limits / next steps

- Tables are created with `create_all` on startup, and new nullable columns are added to existing databases
  automatically (`add_missing_columns` in `db.py`). Anything beyond adding columns (renames, type changes) needs
  Alembic, which should be adopted before production.
- Processing runs on an in-process thread pool. Move it to a real queue (Celery, RQ, or Arq) to run several API replicas.
- Word (.docx) attachments aren't parsed yet. They're skipped at ingestion. Excel (.xlsx / .xlsm) quotes are supported.
- Rules-engine heuristics are tuned for Indian GST invoices. Other regions mostly escalate to Azure.
- Azure's invoice model doesn't document support for quotations. They're sent with a quotation hint, and local
  rules fill the quotation-only fields, but check accuracy on your own quotations.
- Local OCR quality on handwriting is low. Handwritten documents are classified Difficult and go straight to Azure.
- Thresholds in `complexity.py` and `ADI_ACCEPT_CONFIDENCE` should be calibrated on a labelled set of your real documents.
