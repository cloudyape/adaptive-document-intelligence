<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type DocSummary } from '../api'
import DocTable from '../components/DocTable.vue'

const items = ref<DocSummary[]>([])
onMounted(async () => (items.value = (await api.reviewQueue()).items))
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="text-2xl font-semibold tracking-tight">Review queue</h1>
      <p class="text-sm text-slate-500">Documents where every automatic engine, including the fallback, left validation issues.</p>
    </div>
    <DocTable :items="items" show-error />
  </div>
</template>
