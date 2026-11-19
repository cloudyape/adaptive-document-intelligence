<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api, money, when, type PriceEntry, type PriceFile } from '../api'

const files = ref<PriceFile[]>([])
const selected = ref<PriceFile | null>(null)
const entries = ref<PriceEntry[]>([])
const q = ref('')
const busy = ref(false)
const message = ref('')
const error = ref('')

async function load() {
  files.value = (await api.priceFiles()).items
  if (!selected.value && files.value.length) selected.value = files.value.find((f) => f.active) ?? files.value[0]
}
async function loadEntries() {
  entries.value = selected.value ? (await api.priceEntries(selected.value.id, q.value)).items : []
}
watch([selected, q], loadEntries)
onMounted(load)

async function upload(ev: Event) {
  const file = (ev.target as HTMLInputElement).files?.[0]
  if (!file) return
  busy.value = true
  message.value = error.value = ''
  try {
    const r = await api.uploadPriceFile(file, localStorage.getItem('adi.reviewer') ?? undefined)
    message.value = `Read ${r.row_count} prices from ${r.filename}. Recalculated margins on ${r.records_recalculated} unverified record(s).`
    selected.value = null
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
    ;(ev.target as HTMLInputElement).value = ''
  }
}
async function toggle(f: PriceFile) {
  const r = await api.setPriceFileActive(f.id, !f.active)
  message.value = `${r.filename} ${r.active ? 'activated' : 'deactivated'}; recalculated ${r.records_recalculated} record(s).`
  await load()
}
const activeId = () => files.value.find((f) => f.active)?.id
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="text-2xl font-semibold tracking-tight">Price files</h1>
      <p class="text-sm text-slate-500">
        Dealer cost price lists used to calculate <b>Margin By</b> (selling price − dealer cost) and <b>Margin By %</b> (margin ÷ selling price).
        The newest active file is used; margins printed on a quote are never overwritten, and approved records are left as they are.
      </p>
    </div>

    <label class="card flex cursor-pointer flex-col items-center gap-1 border-dashed p-5 text-center text-sm text-slate-500">
      <input type="file" class="hidden" accept=".csv,.xlsx,.xlsm,.pdf,.txt" :disabled="busy" @change="upload" />
      <span class="font-medium text-slate-700">{{ busy ? 'Reading…' : 'Upload a dealer cost price file' }}</span>
      <span>CSV or Excel with columns like “Model / Part #” and “Dealer Cost / Dealer Net” (optional “MSRP”). PDF is read best-effort.</span>
    </label>
    <div v-if="message" class="text-sm text-indigo-700">{{ message }}</div>
    <div v-if="error" class="text-sm text-rose-600">{{ error }}</div>

    <div class="grid gap-4 lg:grid-cols-3">
      <section class="card overflow-hidden lg:col-span-1">
        <div v-if="!files.length" class="p-4 text-sm text-slate-400">No price files yet.</div>
        <button v-for="f in files" :key="f.id" class="block w-full border-b border-slate-100 px-4 py-3 text-left text-sm hover:bg-slate-50"
          :class="selected?.id === f.id ? 'bg-indigo-50' : ''" @click="selected = f">
          <div class="flex items-center justify-between gap-2">
            <span class="truncate font-medium">{{ f.filename }}</span>
            <span v-if="f.id === activeId()" class="rounded-full bg-emerald-100 px-2 text-xs text-emerald-800">in use</span>
            <span v-else-if="!f.active" class="text-xs text-slate-400">inactive</span>
          </div>
          <div class="text-xs text-slate-500">{{ f.row_count }} prices · {{ when(f.uploaded_at) }}<span v-if="f.uploaded_by"> · {{ f.uploaded_by }}</span></div>
          <div v-for="w in f.warnings" :key="w" class="mt-1 text-xs text-amber-700">{{ w }}</div>
        </button>
      </section>

      <section v-if="selected" class="card overflow-x-auto lg:col-span-2">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 p-3">
          <input v-model.lazy="q" class="input max-w-xs" placeholder="Search model or description" />
          <button class="btn-secondary" @click="toggle(selected)">{{ selected.active ? 'Deactivate' : 'Activate' }}</button>
        </div>
        <table class="min-w-full divide-y divide-slate-100">
          <thead><tr><th class="th">Model / Part #</th><th class="th">Description</th><th class="th text-right">Dealer cost</th><th class="th text-right">MSRP</th></tr></thead>
          <tbody class="divide-y divide-slate-100">
            <tr v-for="e in entries" :key="e.model_number">
              <td class="td font-mono text-xs">{{ e.model_number }}</td>
              <td class="td max-w-72 truncate text-xs text-slate-600">{{ e.description ?? '—' }}</td>
              <td class="td text-right tabular-nums">{{ money(e.dealer_cost, 'USD') }}</td>
              <td class="td text-right tabular-nums">{{ money(e.msrp, 'USD') }}</td>
            </tr>
            <tr v-if="!entries.length"><td class="td text-slate-400" colspan="4">No matching entries.</td></tr>
          </tbody>
        </table>
      </section>
    </div>
  </div>
</template>
