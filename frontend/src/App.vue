<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from './api'

const nav = [
  { to: '/', label: 'Overview' },
  { to: '/documents', label: 'Documents' },
  { to: '/review', label: 'Review queue' },
  { to: '/records', label: 'Financial records' },
  { to: '/price-files', label: 'Price files' },
]
const reviewCount = ref(0)
const refreshCount = () => api.reviewQueue().then((r) => (reviewCount.value = r.total)).catch(() => {})
onMounted(() => { refreshCount(); setInterval(refreshCount, 15000) })
</script>

<template>
  <div class="min-h-screen md:flex">
    <aside class="border-b border-slate-200 bg-white md:w-60 md:shrink-0 md:border-b-0 md:border-r">
      <div class="px-5 py-4">
        <div class="text-lg font-semibold tracking-tight">Dealer Quote Parsing</div>
      </div>
      <nav class="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-col">
        <RouterLink v-for="n in nav" :key="n.to" :to="n.to"
          class="flex items-center justify-between whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100"
          active-class="!bg-indigo-50 !text-indigo-700" :exact-active-class="n.to === '/' ? '!bg-indigo-50 !text-indigo-700' : ''">
          {{ n.label }}
          <span v-if="n.to === '/review' && reviewCount" class="ml-2 rounded-full bg-amber-100 px-2 text-xs text-amber-800">{{ reviewCount }}</span>
        </RouterLink>
      </nav>
    </aside>
    <main class="min-w-0 flex-1 p-4 md:p-8">
      <RouterView @changed="refreshCount" />
    </main>
  </div>
</template>
