<script setup lang="ts">
import { computed, ref } from 'vue'
import { formatTraceValue, traceBlocks } from './tracePresentation'
import type { TraceEvent } from './tracePresentation'
const props = defineProps<{ event: TraceEvent }>()
const blocks = computed(() => traceBlocks(props.event))
const open = ref(new Set<number>())
function toggle(index: number, event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) open.value.add(index)
  else open.value.delete(index)
}
</script>
<template>
  <div class="trace-content">
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
