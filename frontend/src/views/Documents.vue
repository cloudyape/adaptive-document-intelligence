<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, type DocSummary } from '../api'
import DocTable from '../components/DocTable.vue'
import UploadBox from '../components/UploadBox.vue'

const filters = reactive({ q: '', status: '', complexity: '', doc_type: '' })
const items = ref<DocSummary[]>([])
const total = ref(0)
const offset = ref(0)
const limit = 50
let timer: number | undefined

async function load() {
  const r = await api.documents({ ...filters, limit, offset: offset.value })
  items.value = r.items
  total.value = r.total
}
watch(filters, () => { offset.value = 0; load() })
onMounted(() => { load(); timer = window.setInterval(load, 8000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="space-y-4">
    <h1 class="text-2xl font-semibold tracking-tight">Documents</h1>
    <UploadBox @uploaded="load" />
    <div class="flex flex-wrap gap-2">
      <input v-model.lazy="filters.q" class="input max-w-xs" placeholder="Search vendor, number, GSTIN, file…" />
      <select v-model="filters.status" class="input w-auto">
        <option value="">All statuses</option>
        <option v-for="s in ['extracted', 'needs_review', 'duplicate', 'approved', 'rejected', 'processing', 'failed', 'ignored', 'split']" :key="s" :value="s">{{ s === 'split' ? 'merged PDF' : s.replace('_', ' ') }}</option>
      </select>
      <select v-model="filters.complexity" class="input w-auto">
        <option value="">All complexity</option>
        <option v-for="c in ['easy', 'medium', 'difficult']" :key="c" :value="c">{{ c }}</option>
      </select>
      <select v-model="filters.doc_type" class="input w-auto">
        <option value="">All types</option>
        <option v-for="t in ['invoice', 'quotation', 'proforma_invoice', 'credit_note', 'purchase_order', 'other']" :key="t" :value="t">{{ t.replace('_', ' ') }}</option>
      </select>
    </div>
    <DocTable :items="items" />
    <div class="flex items-center justify-between text-sm text-slate-500">
      <span>{{ total }} document(s)</span>
      <div class="flex gap-2">
        <button class="btn-secondary" :disabled="offset === 0" @click="offset -= limit; load()">Previous</button>
        <button class="btn-secondary" :disabled="offset + limit >= total" @click="offset += limit; load()">Next</button>
      </div>
    </div>
  </div>
</template>
