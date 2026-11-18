<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api'

const emit = defineEmits<{ uploaded: [] }>()
const dragging = ref(false)
const busy = ref(false)
const message = ref('')

async function send(files: FileList | null | undefined) {
  if (!files?.length) return
  busy.value = true
  message.value = ''
  try {
    const r = await api.upload(files)
    const dup = r.documents.filter((d) => d.duplicate).length
    message.value = `Queued ${r.documents.length - dup} file(s)${dup ? `, ${dup} duplicate(s) skipped` : ''}.`
    emit('uploaded')
  } catch (e) {
    message.value = `Upload failed: ${(e as Error).message}`
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <label
    class="card flex cursor-pointer flex-col items-center justify-center gap-1 border-dashed p-6 text-center text-sm text-slate-500 transition"
    :class="dragging ? 'border-indigo-400 bg-indigo-50' : ''"
    @dragover.prevent="dragging = true" @dragleave="dragging = false"
    @drop.prevent="dragging = false; send($event.dataTransfer?.files)">
    <input type="file" multiple class="hidden" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp,.html,.txt"
      @change="send(($event.target as HTMLInputElement).files)" />
    <span class="font-medium text-slate-700">{{ busy ? 'Uploading…' : 'Drop invoices or quotations here' }}</span>
    <span>PDF, scans, photos, HTML — or click to choose files</span>
    <span v-if="message" class="mt-1 text-xs text-indigo-700">{{ message }}</span>
  </label>
</template>
