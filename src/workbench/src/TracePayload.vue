<script setup lang="ts">
import { computed, onUnmounted, ref, shallowRef, watch } from 'vue'
import { request } from './api'
import { formatTraceValue, requireNodeTrace, traceBlocks } from './tracePresentation'
import type { NodeTraceIdentity, TraceEvent } from './tracePresentation'
const props = defineProps<{ seq: number; url: string; identity?: NodeTraceIdentity }>()
const event = shallowRef<TraceEvent | null>(null)
const error = ref('')
let controller: AbortController | undefined
async function load() {
  controller?.abort()
  const current = new AbortController()
  controller = current
  event.value = null; error.value = ''; open.value.clear()
  try {
    const page = await request<{ events: TraceEvent[] } & Partial<NodeTraceIdentity>>(props.url, { signal: current.signal })
    if (current.signal.aborted) return
    requireNodeTrace(page, props.identity)
    if (page.events[0]?.seq !== props.seq) throw new Error('轨迹记录已变更，请刷新轨迹。')
    event.value = page.events[0]
  } catch (exception) {
    if (!current.signal.aborted) error.value = exception instanceof Error ? exception.message : '轨迹记录读取失败。'
  }
}
const blocks = computed(() => event.value ? traceBlocks(event.value) : [])
const open = ref(new Set<number>())
function toggle(index: number, event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) open.value.add(index)
  else open.value.delete(index)
}
watch([() => props.url, () => props.seq], load, { immediate: true })
onUnmounted(() => controller?.abort())
</script>
<template>
  <p v-if="error" class="note amber" role="alert">{{ error }} <button class="btn small" @click="load">重试</button></p>
  <p v-else-if="!event" role="status">正在读取记录…</p>
  <div v-else class="trace-content">
    <details v-for="(block, index) in blocks" :key="`${event.seq}:${index}`" class="trace-message" @toggle="toggle(index, $event)">
      <summary>{{ block.title }}</summary>
      <pre v-if="open.has(index)" class="code trace-payload">{{ formatTraceValue(block.value) }}</pre>
    </details>
    <details class="trace-raw" @toggle="toggle(-1, $event)"><summary>原始记录（含关联 ID）</summary><pre v-if="open.has(-1)" class="code trace-payload">{{ JSON.stringify(event, null, 2) }}</pre></details>
  </div>
</template>
<style scoped>
.trace-content { padding: 0 12px 12px; }
.trace-message { margin-bottom: 8px; border-left: 2px solid var(--line, #e5eaf1); padding-left: 12px; }
.trace-content summary { cursor: pointer; padding: 6px 0; font-size: 13px; }
.trace-raw { color: #64748b; }
.trace-payload { max-height: 480px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
