<script setup lang="ts">
import { money, pct, usd, when, type DocSummary } from '../api'
import ComplexityBadge from './ComplexityBadge.vue'
import StatusBadge from './StatusBadge.vue'

defineProps<{ items: DocSummary[]; showError?: boolean }>()
</script>

<template>
  <div class="card overflow-x-auto">
    <table class="min-w-full divide-y divide-slate-100">
      <thead>
        <tr>
          <th class="th">Document</th><th class="th">Status</th><th class="th">Type</th><th class="th">Complexity</th>
          <th class="th">Vendor</th><th class="th text-right">Total</th><th class="th">Engine</th>
          <th class="th text-right">Score</th><th class="th text-right">Cost</th><th class="th">Received</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-100">
        <tr v-for="d in items" :key="d.id" class="cursor-pointer hover:bg-slate-50" @click="$router.push(`/documents/${d.id}`)">
          <td class="td max-w-64">
            <div class="truncate font-medium">{{ d.filename }}</div>
            <div class="truncate text-xs text-slate-500">
              <template v-if="d.parent_id">pages {{ d.page_start }}<template v-if="d.page_end !== d.page_start">–{{ d.page_end }}</template> of merged PDF #{{ d.parent_id }}</template>
              <template v-else>{{ d.email_subject ?? d.source }}</template>
            </div>
            <div v-if="showError && d.error" class="mt-0.5 truncate text-xs text-amber-700" :title="d.error">{{ d.error }}</div>
          </td>
          <td class="td"><StatusBadge :status="d.status" /></td>
          <td class="td text-xs text-slate-600">{{ d.doc_type?.replace('_', ' ') ?? '—' }}</td>
          <td class="td"><ComplexityBadge :level="d.complexity" /></td>
          <td class="td max-w-48 truncate">{{ d.vendor_name ?? '—' }}</td>
          <td class="td text-right tabular-nums">{{ money(d.total, d.currency) }}</td>
          <td class="td text-xs text-slate-600">{{ d.final_engine ?? '—' }}</td>
          <td class="td text-right tabular-nums">{{ pct(d.confidence) }}</td>
          <td class="td text-right tabular-nums text-slate-600">{{ usd(d.total_cost_usd) }}</td>
          <td class="td whitespace-nowrap text-xs text-slate-500">{{ when(d.created_at) }}</td>
        </tr>
        <tr v-if="!items.length"><td class="td text-slate-400" colspan="10">Nothing here.</td></tr>
      </tbody>
    </table>
  </div>
</template>
