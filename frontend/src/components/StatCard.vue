<script setup lang="ts">
defineProps<{ label: string; value: string | number; hint?: string }>()
</script>
<template>
  <div class="card p-4">
    <div class="text-xs font-medium uppercase tracking-wide text-slate-500">{{ label }}</div>
    <div class="mt-1 text-2xl font-semibold tabular-nums">{{ value }}</div>
    <div v-if="hint" class="mt-1 text-xs text-slate-500">{{ hint }}</div>
  </div>
</template>
