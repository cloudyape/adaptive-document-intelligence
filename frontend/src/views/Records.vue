<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { api, money, type Extracted } from '../api'

type Rec = Extracted & { id: number; document_id: number; verified: boolean }
const filters = reactive({ vendor: '', doc_type: '', date_from: '', date_to: '' })
const items = ref<Rec[]>([])
const load = async () => {
  const params = Object.fromEntries(Object.entries(filters).filter(([, v]) => v)) as Record<string, string>
  items.value = (await api.records(params)).items
}
watch(filters, load)
onMounted(load)

function exportCsv() {
  const cols = ['document_type', 'document_number', 'document_date', 'due_date', 'vendor', 'vendor_gstin', 'buyer', 'buyer_gstin',
    'currency', 'subtotal', 'cgst', 'sgst', 'igst', 'tax_total', 'total', 'payment_terms', 'verified'] as const
  const rows = items.value.map((r) => [r.document_type, r.document_number, r.document_date, r.due_date, r.vendor.name, r.vendor.gstin,
    r.buyer.name, r.buyer.gstin, r.currency, r.subtotal, r.cgst, r.sgst, r.igst, r.tax_total, r.total, r.payment_terms, r.verified])
  const esc = (v: unknown) => (v == null ? '' : `"${String(v).replace(/"/g, '""')}"`)
  const csv = [cols.join(','), ...rows.map((r) => r.map(esc).join(','))].join('\n')
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }))
  a.download = 'financial-records.csv'
  a.click()
}
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 class="text-2xl font-semibold tracking-tight">Financial records</h1>
        <p class="text-sm text-slate-500">Standardized output from accepted and approved documents. Also at <code>/api/records</code>.</p>
      </div>
      <button class="btn-secondary" @click="exportCsv">Export CSV</button>
    </div>
    <div class="flex flex-wrap gap-2">
      <input v-model.lazy="filters.vendor" class="input max-w-xs" placeholder="Vendor" />
      <select v-model="filters.doc_type" class="input w-auto">
        <option value="">All types</option>
        <option v-for="t in ['invoice', 'quotation', 'proforma_invoice', 'credit_note', 'purchase_order']" :key="t" :value="t">{{ t.replace('_', ' ') }}</option>
      </select>
      <input v-model="filters.date_from" type="date" class="input w-auto" />
      <input v-model="filters.date_to" type="date" class="input w-auto" />
    </div>
    <div class="card overflow-x-auto">
      <table class="min-w-full divide-y divide-slate-100">
        <thead><tr>
          <th class="th">Type</th><th class="th">Number</th><th class="th">Date</th><th class="th">Vendor</th><th class="th">GSTIN</th>
          <th class="th text-right">Subtotal</th><th class="th text-right">Tax</th><th class="th text-right">Total</th><th class="th">Terms</th><th class="th"></th>
        </tr></thead>
        <tbody class="divide-y divide-slate-100">
          <tr v-for="r in items" :key="r.id" class="cursor-pointer hover:bg-slate-50" @click="$router.push(`/documents/${r.document_id}`)">
            <td class="td text-xs">{{ r.document_type.replace('_', ' ') }}</td>
            <td class="td font-medium">{{ r.document_number ?? '—' }}</td>
            <td class="td whitespace-nowrap">{{ r.document_date ?? '—' }}</td>
            <td class="td max-w-56 truncate">{{ r.vendor.name }}</td>
            <td class="td font-mono text-xs">{{ r.vendor.gstin ?? '—' }}</td>
            <td class="td text-right tabular-nums">{{ money(r.subtotal, r.currency) }}</td>
            <td class="td text-right tabular-nums">{{ money(r.tax_total, r.currency) }}</td>
            <td class="td text-right font-medium tabular-nums">{{ money(r.total, r.currency) }}</td>
            <td class="td max-w-48 truncate text-xs text-slate-600">{{ r.payment_terms ?? '—' }}</td>
            <td class="td"><span v-if="r.verified" class="text-xs text-emerald-700">verified</span></td>
          </tr>
          <tr v-if="!items.length"><td class="td text-slate-400" colspan="10">No records yet.</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
