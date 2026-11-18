<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, pct, usd, type Config, type Stats } from '../api'
import StatCard from '../components/StatCard.vue'
import UploadBox from '../components/UploadBox.vue'

const stats = ref<Stats | null>(null)
const config = ref<Config | null>(null)
const ingestMsg = ref('')
const ingesting = ref(false)
let timer: number | undefined

async function load() {
  ;[stats.value, config.value] = await Promise.all([api.stats(), api.config()])
}
onMounted(() => { load(); timer = window.setInterval(load, 10000) })
onUnmounted(() => clearInterval(timer))

async function ingest() {
  ingesting.value = true
  try {
    const r = await api.ingestNow()
    ingestMsg.value = `${r.messages} new message(s), ${r.documents} document(s) queued.`
    load()
  } catch (e) {
    ingestMsg.value = (e as Error).message
  } finally {
    ingesting.value = false
  }
}

const tiers = [
  { key: 'easy', label: 'Easy', route: 'Rules engine', color: 'bg-teal-500' },
  { key: 'medium', label: 'Medium', route: 'OCR + small LLM', color: 'bg-indigo-500' },
  { key: 'difficult', label: 'Difficult', route: 'Document AI', color: 'bg-fuchsia-500' },
]
const routed = computed(() => tiers.reduce((n, t) => n + (stats.value?.by_complexity[t.key] ?? 0), 0))
const engineNames: Record<string, string> = {
  rules: 'Rules engine', ocr: 'Local OCR (Tesseract)', claude: 'Claude API', azure_di: 'Azure Document Intelligence',
}
const engineLabels: Record<string, string> = {
  rules: 'Rules (lightweight)', ocr_llm: 'OCR + LLM', claude_vision: 'Claude vision', azure_di: 'Azure Document Intelligence', claude_fallback: 'Fallback (top model)',
}
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 class="text-2xl font-semibold tracking-tight">Overview</h1>
        <p class="text-sm text-slate-500">How documents were routed, what it cost, and what still needs a human.</p>
      </div>
      <div class="flex items-center gap-3">
        <span v-if="ingestMsg" class="text-xs text-slate-500">{{ ingestMsg }}</span>
        <button class="btn-primary" :disabled="ingesting || !config?.graph_configured" @click="ingest"
          :title="config?.graph_configured ? '' : 'Set ADI_GRAPH_* variables to connect Outlook'">
          {{ ingesting ? 'Fetching…' : 'Fetch from Outlook' }}
        </button>
      </div>
    </header>

    <div v-if="stats" class="grid grid-cols-2 gap-4 lg:grid-cols-4">
      <StatCard label="Documents" :value="stats.documents_total" :hint="`${stats.by_status.ignored ?? 0} non-financial ignored`" />
      <StatCard label="Straight-through" :value="pct(stats.straight_through_rate)" hint="accepted with no human touch" />
      <StatCard label="Needs review" :value="(stats.by_status.needs_review ?? 0) + (stats.by_status.failed ?? 0)" />
      <StatCard label="Extraction spend" :value="usd(stats.total_cost_usd)" :hint="`${usd(stats.avg_cost_per_document_usd)} per document`" />
    </div>

    <div class="grid gap-6 lg:grid-cols-5">
      <section class="card p-5 lg:col-span-3">
        <h2 class="text-sm font-semibold">Complexity routing</h2>
        <p class="mb-4 text-xs text-slate-500">Each document starts at the cheapest engine its complexity allows.</p>
        <div class="space-y-4">
          <div v-for="t in tiers" :key="t.key">
            <div class="mb-1 flex items-baseline justify-between text-sm">
              <span><span class="font-medium">{{ t.label }}</span> <span class="text-slate-400">→ {{ t.route }}</span></span>
              <span class="tabular-nums text-slate-600">
                {{ stats?.by_complexity[t.key] ?? 0 }} docs · {{ usd(stats?.cost_by_complexity[t.key] ?? 0) }}
              </span>
            </div>
            <div class="h-2 overflow-hidden rounded-full bg-slate-100">
              <div class="h-full rounded-full" :class="t.color"
                :style="{ width: routed ? `${((stats?.by_complexity[t.key] ?? 0) / routed) * 100}%` : '0%' }" />
            </div>
          </div>
        </div>
      </section>

      <section class="card p-5 lg:col-span-2">
        <h2 class="mb-3 text-sm font-semibold">Engines</h2>
        <ul class="space-y-2 text-sm">
          <li v-for="(on, name) in config?.engines" :key="name" class="flex items-center justify-between">
            <span>{{ engineNames[name] ?? name }}</span>
            <span class="text-xs" :class="on ? 'text-emerald-600' : 'text-slate-400'">{{ on ? 'available' : 'not configured' }}</span>
          </li>
          <li class="flex items-center justify-between">
            <span>Outlook (Graph)</span>
            <span class="text-xs" :class="config?.graph_configured ? 'text-emerald-600' : 'text-slate-400'">
              {{ config?.graph_configured ? (config.graph_poll_enabled ? 'polling' : 'connected, manual') : 'not configured' }}
            </span>
          </li>
        </ul>
        <div v-if="config" class="mt-4 border-t border-slate-100 pt-3 text-xs text-slate-500">
          Accept at ≥ {{ pct(config.accept_confidence) }} validation score · cost cap {{ usd(config.max_cost_per_document_usd) }} / doc
        </div>
      </section>
    </div>

    <section class="card overflow-x-auto">
      <table class="min-w-full divide-y divide-slate-100">
        <thead><tr>
          <th class="th">Tier</th><th class="th">Engine</th><th class="th text-right">Runs</th>
          <th class="th text-right">Accepted</th><th class="th text-right">Acceptance</th><th class="th text-right">Spend</th>
        </tr></thead>
        <tbody class="divide-y divide-slate-100">
          <tr v-for="e in stats?.engines" :key="e.engine">
            <td class="td text-slate-500">{{ e.tier }}</td>
            <td class="td font-medium">{{ engineLabels[e.engine] ?? e.engine }}</td>
            <td class="td text-right tabular-nums">{{ e.runs }}</td>
            <td class="td text-right tabular-nums">{{ e.accepted }}</td>
            <td class="td text-right tabular-nums">{{ pct(e.runs ? e.accepted / e.runs : null) }}</td>
            <td class="td text-right tabular-nums">{{ usd(e.cost_usd) }}</td>
          </tr>
          <tr v-if="!stats?.engines.length"><td class="td text-slate-400" colspan="6">No extractions yet — upload a document below.</td></tr>
        </tbody>
      </table>
    </section>

    <UploadBox @uploaded="load" />
  </div>
</template>
