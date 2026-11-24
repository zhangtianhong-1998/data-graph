<script setup lang="ts">
import { computed } from 'vue'
import type { RetrievalStatusResponse } from './types'

const props = defineProps<{ status?: RetrievalStatusResponse | null; working: boolean; busy: boolean }>()
defineEmits<{ build: [] }>()
const semantic = computed(() => props.status?.configured_provider === 'openai_compatible')
const ready = computed(() => props.status?.indexed || props.status?.vectorized)
const label = computed(() => {
  if (props.status?.vectorized) return `已完成向量化 · ${props.status.embedder_model} (${props.status.vector_dim} 维)`
  if (ready.value) return '关键词与关系索引已就绪'
  if (props.status?.status === 'stale') return '检索索引待更新'
  return semantic.value ? '未向量化' : '尚未建立检索索引'
})
const action = computed(() => {
  if (props.working) return '正在建立索引…'
  if (semantic.value) return ready.value ? '重新向量化' : '立即向量化'
  return ready.value ? '重新建立索引' : '建立检索索引'
})
</script>

<template>
  <span class="badge" :class="ready ? 'green' : 'amber'">{{ status?.status === 'no_graph' ? '尚无图谱' : label }}</span>
  <button v-if="status?.status !== 'no_graph'" class="btn small" type="button" :disabled="working || busy" @click="$emit('build')">{{ action }}</button>
</template>
