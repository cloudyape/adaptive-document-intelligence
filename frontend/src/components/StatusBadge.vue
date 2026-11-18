<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ status: string }>()
const styles: Record<string, string> = {
  extracted: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  approved: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
  needs_review: 'bg-amber-50 text-amber-800 ring-amber-200',
  failed: 'bg-rose-50 text-rose-700 ring-rose-200',
  rejected: 'bg-rose-50 text-rose-700 ring-rose-200',
  processing: 'bg-sky-50 text-sky-700 ring-sky-200',
  received: 'bg-slate-50 text-slate-600 ring-slate-200',
  ignored: 'bg-slate-50 text-slate-400 ring-slate-200',
}
const cls = computed(() => styles[props.status] ?? styles.received)
</script>
<template>
  <span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset" :class="cls">
    {{ status.replace('_', ' ') }}
  </span>
</template>
