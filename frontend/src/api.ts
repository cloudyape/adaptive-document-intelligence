export type Status = 'received' | 'ignored' | 'processing' | 'extracted' | 'needs_review' | 'approved' | 'rejected' | 'failed' | 'duplicate' | 'split'
export type Complexity = 'easy' | 'medium' | 'difficult'

export interface Party { name: string | null; address?: string | null; gstin: string | null; pan?: string | null; email?: string | null; phone?: string | null }
export interface OptionItem { name: string; price: number | null }
export interface LineItem {
  description: string | null; hsn_sac: string | null; quantity: number | null; unit: string | null
  unit_price: number | null; discount: number | null; tax_rate: number | null; amount: number | null
  series: string | null; model: string | null; options: OptionItem[]; base_price: number | null; msrp?: number | null
  margin_amount: number | null; margin_percent: number | null
}
export interface PriceFile { id: number; filename: string; uploaded_at: string; uploaded_by: string | null; active: boolean; row_count: number; warnings: string[] }
export interface PriceEntry { model_number: string; description: string | null; dealer_cost: number | null; msrp: number | null }
export interface Extracted {
  document_type: string; document_number: string | null; document_date: string | null; due_date: string | null
  valid_until: string | null; po_number: string | null; currency: string | null; vendor: Party; buyer: Party
  line_items: LineItem[]; subtotal: number | null; discount_total: number | null; cgst: number | null; sgst: number | null
  igst: number | null; cess: number | null; tax_total: number | null; shipping: number | null; round_off: number | null
  total: number | null; payment_terms: string | null; notes: string | null
  margin_amount: number | null; margin_percent: number | null; margin_source?: string | null
  other_charges?: number | null; trade_in?: number | null
}
export interface Issue { field: string; code: string; message: string; severity: 'critical' | 'error' | 'warning' }
export interface Attempt {
  id: number; engine: string; tier: number; model: string | null; duration_ms: number; cost_usd: number
  input_tokens: number; output_tokens: number; success: boolean; accepted: boolean; skipped: boolean; confidence: number | null
  validation: { score: number; passed: boolean; checks_run: number; issues: Issue[] } | null
  error: string | null; result: Extracted | null
}
export interface DocSummary {
  id: number; filename: string; mime_type: string; source: string; status: Status; doc_type: string | null
  complexity: Complexity | null; complexity_score: number | null; final_engine: string | null; confidence: number | null
  total_cost_usd: number; error: string | null; created_at: string; updated_at: string
  email_subject: string | null; email_sender: string | null; vendor_name: string | null
  document_number: string | null; document_date: string | null; total: number | null; currency: string | null
  parent_id: number | null; page_start: number | null; page_end: number | null
}
export interface DocDetail extends DocSummary {
  identification_confidence: number | null; complexity_reasons: string[] | null; features: Record<string, unknown> | null
  size_bytes: number
  email: { subject: string; sender: string; received_at: string; body_preview: string } | null
  record: { id: number; data: Extracted; verified: boolean; verified_by: string | null; verified_at: string | null } | null
  attempts: Attempt[]
  reviews: { action: string; reviewer: string | null; comment: string | null; changes: Record<string, unknown> | null; created_at: string }[]
  parent: { id: number; filename: string } | null
  children: DocSummary[]
  packet: { quotes: number; by_status: Record<string, number> } | null
}
export interface Stats {
  documents_total: number; by_status: Record<string, number>; by_complexity: Record<string, number>
  by_doc_type: Record<string, number>; by_final_engine: Record<string, number>
  engines: { engine: string; tier: number; runs: number; cost_usd: number; accepted: number }[]
  total_cost_usd: number; avg_cost_per_document_usd: number; cost_by_complexity: Record<string, number>
  straight_through_rate: number | null
}
export interface Config {
  engines: Record<string, boolean>; graph_configured: boolean
  azure: { read_enabled: boolean; highres_fallback: boolean; cost_per_page: { read: number; invoice: number; highres_addon: number } }
  graph_poll_enabled: boolean; accept_confidence: number; max_cost_per_document_usd: number
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, init)
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* not json */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return res.json() as Promise<T>
}
const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })

export const api = {
  stats: () => req<Stats>('/stats'),
  config: () => req<Config>('/config'),
  documents: (params: Record<string, string | number | undefined> = {}) => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]))
    return req<{ total: number; items: DocSummary[] }>(`/documents?${q}`)
  },
  reviewQueue: () => req<{ total: number; items: DocSummary[] }>('/review-queue'),
  document: (id: number | string) => req<DocDetail>(`/documents/${id}`),
  fileUrl: (id: number | string) => `/api/documents/${id}/file`,
  upload: (files: FileList | File[]) => {
    const fd = new FormData()
    Array.from(files).forEach((f) => fd.append('files', f))
    return req<{ documents: { id: number; filename: string; duplicate: boolean }[] }>('/documents/upload', { method: 'POST', body: fd })
  },
  reprocess: (id: number) => req<{ queued: number }>(`/documents/${id}/reprocess`, { method: 'POST' }),
  review: (id: number, body: { action: 'approve' | 'reject' | 'correct'; reviewer?: string; comment?: string; data?: Extracted }) =>
    req<DocDetail>(`/documents/${id}/review`, json(body)),
  records: (params: Record<string, string> = {}) => req<{ items: (Extracted & { id: number; document_id: number; verified: boolean })[] }>(`/records?${new URLSearchParams(params)}`),
  priceFiles: () => req<{ items: PriceFile[] }>('/price-files'),
  priceEntries: (id: number, q = '') => req<{ items: PriceEntry[] }>(`/price-files/${id}/entries?q=${encodeURIComponent(q)}`),
  uploadPriceFile: (file: File, uploadedBy?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    const qs = uploadedBy ? `?uploaded_by=${encodeURIComponent(uploadedBy)}` : ''
    return req<PriceFile & { records_recalculated: number }>(`/price-files${qs}`, { method: 'POST', body: fd })
  },
  setPriceFileActive: (id: number, active: boolean) =>
    req<PriceFile & { records_recalculated: number }>(`/price-files/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ active }) }),
  ingestNow: () => req<{ messages: number; documents: number; skipped_existing: number }>('/ingest/run', { method: 'POST' }),
}

export const money = (v: number | null | undefined, currency?: string | null) =>
  v == null ? '—' : new Intl.NumberFormat('en-IN', { style: 'currency', currency: currency || 'INR', maximumFractionDigits: 2 }).format(v)
export const usd = (v: number | null | undefined) =>
  v == null ? '—' : v === 0 ? '$0' : v < 0.01 ? `$${v.toFixed(5)}` : `$${v.toFixed(3)}`
export const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v * 100)}%`)
export const when = (s: string) => new Date(s).toLocaleString()
