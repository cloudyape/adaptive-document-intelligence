<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, money, pct, usd, when, type DocDetail, type Extracted, type Issue, type LineItem } from '../api'
import ComplexityBadge from '../components/ComplexityBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'

const props = defineProps<{ id: string }>()
const emit = defineEmits<{ changed: [] }>()

const doc = ref<DocDetail | null>(null)
const form = ref<Extracted | null>(null)
const tab = ref<'data' | 'routing' | 'source'>('data')
const reviewer = ref(localStorage.getItem('adi.reviewer') ?? '')
const comment = ref('')
const busy = ref(false)
const error = ref('')
let timer: number | undefined
// Vue reactive proxies can't go through structuredClone; the data is plain JSON anyway.
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v))

const emptyDoc = (): Extracted => ({
  document_type: 'invoice', document_number: null, document_date: null, due_date: null, valid_until: null, po_number: null,
  currency: 'INR', vendor: { name: null, gstin: null }, buyer: { name: null, gstin: null }, line_items: [], subtotal: null,
  discount_total: null, cgst: null, sgst: null, igst: null, cess: null, tax_total: null, shipping: null, round_off: null,
  total: null, payment_terms: null, notes: null,
})

async function load(resetForm = true) {
  doc.value = await api.document(props.id)
  if (resetForm) form.value = clone(doc.value.record?.data ?? emptyDoc())
  clearInterval(timer)
  if (['received', 'processing'].includes(doc.value.status)) timer = window.setInterval(() => load(), 2000)
}
onMounted(() => load())
onUnmounted(() => clearInterval(timer))

const best = computed(() => doc.value?.attempts.find((a) => a.accepted)
  ?? [...(doc.value?.attempts ?? [])].filter((a) => a.validation).sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0))[0])
const issues = computed<Issue[]>(() => best.value?.validation?.issues ?? [])
const issueFor = (field: string) => issues.value.filter((i) => i.field === field || i.field.startsWith(`${field}[`))
const fieldClass = (field: string) => {
  const sev = issueFor(field)[0]?.severity
  return sev === 'critical' || sev === 'error' ? '!border-rose-400 !bg-rose-50' : sev === 'warning' ? '!border-amber-300' : ''
}
const lineSum = computed(() => form.value?.line_items.reduce((s, li) => s + (Number(li.amount) || 0), 0) ?? 0)
const canReview = computed(() => doc.value && !['received', 'processing', 'ignored'].includes(doc.value.status))
const isImage = computed(() => doc.value?.mime_type.startsWith('image/'))
const sevClass: Record<string, string> = { critical: 'text-rose-700', error: 'text-rose-600', warning: 'text-amber-700' }

function num(v: unknown): number | null {
  if (v === '' || v == null) return null
  const n = Number(String(v).replace(/,/g, ''))
  return Number.isFinite(n) ? n : null
}
function normalized(): Extracted {
  const f = clone(form.value!)
  for (const k of ['subtotal', 'discount_total', 'cgst', 'sgst', 'igst', 'cess', 'tax_total', 'shipping', 'round_off', 'total'] as const)
    f[k] = num(f[k])
  f.line_items = f.line_items.map((li) => ({ ...li, quantity: num(li.quantity), unit_price: num(li.unit_price), discount: num(li.discount), tax_rate: num(li.tax_rate), amount: num(li.amount) }))
  for (const k of ['document_number', 'document_date', 'due_date', 'valid_until', 'po_number', 'payment_terms'] as const)
    if (f[k] === '') f[k] = null
  return f
}
function addLine() {
  form.value!.line_items.push({ description: '', hsn_sac: null, quantity: null, unit: null, unit_price: null, discount: null, tax_rate: null, amount: null } as LineItem)
}

async function act(action: 'approve' | 'reject' | 'correct') {
  if (!doc.value) return
  busy.value = true
  error.value = ''
  localStorage.setItem('adi.reviewer', reviewer.value)
  try {
    const changed = JSON.stringify(normalized()) !== JSON.stringify(doc.value.record?.data ?? null)
    doc.value = await api.review(doc.value.id, {
      action, reviewer: reviewer.value || undefined, comment: comment.value || undefined,
      data: action !== 'reject' && changed ? normalized() : undefined,
    })
    form.value = clone(doc.value.record?.data ?? emptyDoc())
    comment.value = ''
    emit('changed')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
async function reprocess() {
  if (!doc.value) return
  await api.reprocess(doc.value.id)
  await load()
}
const headerFields: [keyof Extracted, string, string?][] = [
  ['document_number', 'Document number'], ['document_date', 'Date', 'date'], ['due_date', 'Due date', 'date'],
  ['valid_until', 'Valid until', 'date'], ['po_number', 'PO number'], ['currency', 'Currency'],
]
const totalFields: [keyof Extracted, string][] = [
  ['subtotal', 'Subtotal'], ['discount_total', 'Discount'], ['cgst', 'CGST'], ['sgst', 'SGST'], ['igst', 'IGST'],
  ['cess', 'Cess'], ['tax_total', 'Tax total'], ['shipping', 'Shipping'], ['round_off', 'Round off'], ['total', 'Grand total'],
]
</script>

<template>
  <div v-if="doc" class="space-y-4">
    <RouterLink to="/documents" class="text-sm text-indigo-600 hover:underline">← Documents</RouterLink>
    <header class="flex flex-wrap items-start justify-between gap-3">
      <div class="min-w-0">
        <h1 class="truncate text-xl font-semibold tracking-tight">{{ doc.filename }}</h1>
        <div class="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-500">
          <StatusBadge :status="doc.status" />
          <ComplexityBadge :level="doc.complexity" />
          <span>{{ doc.doc_type?.replace('_', ' ') }}</span>
          <span>· score {{ pct(doc.confidence) }}</span>
          <span>· {{ usd(doc.total_cost_usd) }}</span>
          <span v-if="doc.final_engine">· via {{ doc.final_engine }}</span>
        </div>
      </div>
      <button class="btn-secondary" @click="reprocess">Reprocess</button>
    </header>

    <div v-if="['received', 'processing'].includes(doc.status)" class="card p-4 text-sm text-sky-700">Processing… this page refreshes automatically.</div>
    <div v-if="doc.status === 'ignored'" class="card p-4 text-sm text-slate-600">
      Identified as not a financial document ({{ pct(doc.identification_confidence) }} confidence). Reprocess or correct the data below if this is wrong.
    </div>

    <div class="grid gap-4 xl:grid-cols-2">
      <!-- Original -->
      <section class="card overflow-hidden xl:sticky xl:top-4 xl:self-start">
        <img v-if="isImage" :src="api.fileUrl(doc.id)" class="w-full" alt="Original document" />
        <iframe v-else :src="api.fileUrl(doc.id)" class="h-[75vh] w-full bg-white" title="Original document" />
      </section>

      <!-- Data / routing -->
      <section class="space-y-4">
        <div class="flex gap-1 rounded-lg bg-slate-100 p-1 text-sm">
          <button v-for="t in (['data', 'routing', 'source'] as const)" :key="t" class="flex-1 rounded-md px-3 py-1.5 font-medium"
            :class="tab === t ? 'bg-white shadow-sm' : 'text-slate-500'" @click="tab = t">
            {{ { data: 'Extracted data', routing: `Routing (${doc.attempts.length})`, source: 'Source' }[t] }}
          </button>
        </div>

        <!-- Extracted data (editable) -->
        <template v-if="tab === 'data' && form">
          <div v-if="issues.length" class="card space-y-1 p-4 text-sm">
            <div class="font-medium">Validation</div>
            <div v-for="(i, k) in issues" :key="k" :class="sevClass[i.severity]">
              <span class="font-mono text-xs">{{ i.field }}</span> — {{ i.message }}
            </div>
          </div>

          <div class="card space-y-4 p-4">
            <div class="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <div>
                <label class="label">Type</label>
                <select v-model="form.document_type" class="input" :class="fieldClass('document_type')">
                  <option v-for="t in ['invoice', 'quotation', 'proforma_invoice', 'credit_note', 'purchase_order', 'other']" :key="t" :value="t">{{ t.replace('_', ' ') }}</option>
                </select>
              </div>
              <div v-for="[k, label, type] in headerFields" :key="k">
                <label class="label">{{ label }}</label>
                <input v-model="(form[k] as string)" :type="type ?? 'text'" class="input" :class="fieldClass(k)" />
              </div>
            </div>
            <div class="grid gap-4 sm:grid-cols-2">
              <div v-for="p in (['vendor', 'buyer'] as const)" :key="p" class="space-y-2">
                <div class="text-xs font-semibold uppercase tracking-wide text-slate-500">{{ p }}</div>
                <input v-model="form[p].name" class="input" placeholder="Name" :class="fieldClass(`${p}.name`)" />
                <input v-model="form[p].gstin" class="input font-mono" placeholder="GSTIN" :class="fieldClass(`${p}.gstin`)" />
                <input v-model="form[p].address" class="input" placeholder="Address" />
              </div>
            </div>
          </div>

          <div class="card overflow-x-auto">
            <table class="min-w-full text-sm">
              <thead><tr>
                <th class="th">Description</th><th class="th">HSN</th><th class="th text-right">Qty</th>
                <th class="th text-right">Unit price</th><th class="th text-right">Tax %</th><th class="th text-right">Amount</th><th></th>
              </tr></thead>
              <tbody>
                <tr v-for="(li, i) in form.line_items" :key="i" class="border-t border-slate-100">
                  <td class="p-1 min-w-48"><input v-model="li.description" class="input" /></td>
                  <td class="p-1 w-20"><input v-model="li.hsn_sac" class="input" /></td>
                  <td class="p-1 w-20"><input v-model="li.quantity" class="input text-right" /></td>
                  <td class="p-1 w-28"><input v-model="li.unit_price" class="input text-right" /></td>
                  <td class="p-1 w-16"><input v-model="li.tax_rate" class="input text-right" /></td>
                  <td class="p-1 w-28"><input v-model="li.amount" class="input text-right" :class="fieldClass(`line_items[${i}].amount`)" /></td>
                  <td class="p-1"><button class="text-slate-400 hover:text-rose-600" title="Remove line" @click="form.line_items.splice(i, 1)">✕</button></td>
                </tr>
              </tbody>
            </table>
            <div class="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <button class="text-indigo-600 hover:underline" @click="addLine">+ Add line</button>
              <span>Lines sum to <span class="tabular-nums font-medium text-slate-700">{{ money(lineSum, form.currency) }}</span></span>
            </div>
          </div>

          <div class="card p-4">
            <div class="grid grid-cols-2 gap-3 sm:grid-cols-5">
              <div v-for="[k, label] in totalFields" :key="k">
                <label class="label">{{ label }}</label>
                <input v-model="(form[k] as number)" class="input text-right tabular-nums" :class="[fieldClass(k), k === 'total' ? 'font-semibold' : '']" />
              </div>
            </div>
            <div class="mt-3">
              <label class="label">Payment terms</label>
              <input v-model="form.payment_terms" class="input" />
            </div>
          </div>

          <div v-if="canReview" class="card space-y-3 p-4">
            <div class="grid gap-3 sm:grid-cols-2">
              <input v-model="reviewer" class="input" placeholder="Your name or email" />
              <input v-model="comment" class="input" placeholder="Comment (optional)" />
            </div>
            <div class="flex flex-wrap gap-2">
              <button class="btn-primary" :disabled="busy" @click="act('approve')">Approve{{ doc.status === 'approved' ? 'd' : '' }}</button>
              <button class="btn-secondary" :disabled="busy" @click="act('correct')">Save corrections</button>
              <button class="btn-danger" :disabled="busy" @click="act('reject')">Reject</button>
            </div>
            <div v-if="error" class="text-sm text-rose-600">{{ error }}</div>
            <div v-if="doc.record?.verified" class="text-xs text-emerald-700">Verified by {{ doc.record.verified_by }} · {{ when(doc.record.verified_at!) }}</div>
          </div>
        </template>

        <!-- Routing explanation + escalation trail -->
        <template v-if="tab === 'routing'">
          <div class="card p-4 text-sm">
            <div class="mb-2 font-medium">Why <ComplexityBadge :level="doc.complexity" /> ? <span class="text-slate-400">(score {{ doc.complexity_score }})</span></div>
            <ul class="space-y-0.5 font-mono text-xs text-slate-600">
              <li v-for="r in doc.complexity_reasons" :key="r">{{ r }}</li>
            </ul>
          </div>
          <ol class="space-y-3">
            <li v-for="a in doc.attempts" :key="a.id" class="card p-4 text-sm"
              :class="a.accepted ? 'ring-2 ring-emerald-300' : a.skipped ? 'opacity-60' : ''">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="font-medium">Tier {{ a.tier }} · {{ a.engine }} <span v-if="a.model" class="font-normal text-slate-500">({{ a.model }})</span></div>
                <div class="text-xs text-slate-500 tabular-nums">
                  {{ a.duration_ms }} ms · {{ usd(a.cost_usd) }}<span v-if="a.input_tokens"> · {{ a.input_tokens }}/{{ a.output_tokens }} tok</span>
                </div>
              </div>
              <div class="mt-1 text-xs">
                <span v-if="a.accepted" class="text-emerald-700">Accepted · score {{ pct(a.confidence) }}</span>
                <span v-else-if="a.validation" class="text-amber-700">Rejected by validation · score {{ pct(a.confidence) }} → escalated</span>
                <span v-else-if="a.skipped" class="text-slate-500">Skipped — {{ a.error }}</span>
                <span v-else class="text-rose-600">Failed — {{ a.error }} → escalated</span>
              </div>
              <ul v-if="a.validation?.issues.length" class="mt-2 space-y-0.5 text-xs">
                <li v-for="(i, k) in a.validation.issues" :key="k" :class="sevClass[i.severity]">{{ i.field }}: {{ i.message }}</li>
              </ul>
            </li>
          </ol>
          <div v-if="doc.reviews.length" class="card p-4 text-sm">
            <div class="mb-2 font-medium">Review history</div>
            <div v-for="(r, k) in doc.reviews" :key="k" class="text-xs text-slate-600">
              {{ when(r.created_at) }} — <b>{{ r.action }}</b> by {{ r.reviewer ?? 'unknown' }}
              <span v-if="r.changes"> ({{ Object.keys(r.changes).length }} field(s) changed)</span>
              <span v-if="r.comment">: “{{ r.comment }}”</span>
            </div>
          </div>
        </template>

        <!-- Source -->
        <template v-if="tab === 'source'">
          <div v-if="doc.email" class="card space-y-1 p-4 text-sm">
            <div><span class="label inline">From</span> {{ doc.email.sender }}</div>
            <div><span class="label inline">Subject</span> {{ doc.email.subject }}</div>
            <div><span class="label inline">Received</span> {{ when(doc.email.received_at) }}</div>
            <p class="pt-2 text-slate-600">{{ doc.email.body_preview }}</p>
          </div>
          <div class="card p-4">
            <div class="mb-2 text-sm font-medium">Measured features</div>
            <dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
              <template v-for="(v, k) in doc.features" :key="k">
                <dt class="text-slate-500">{{ k }}</dt><dd class="font-mono">{{ v ?? '—' }}</dd>
              </template>
            </dl>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>
