<script setup lang="ts">
defineProps<{ level: string | null }>()
const styles: Record<string, string> = {
  easy: 'bg-teal-50 text-teal-700',
  medium: 'bg-indigo-50 text-indigo-700',
  difficult: 'bg-fuchsia-50 text-fuchsia-700',
}
</script>
<template>
  <span v-if="level" class="inline-flex rounded px-1.5 py-0.5 text-xs font-medium" :class="styles[level]">{{ level }}</span>
  <span v-else class="text-xs text-slate-400">—</span>
</template>
