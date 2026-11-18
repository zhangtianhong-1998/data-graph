<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { projectPath, request } from './api'
import type { BusinessContext } from './types'

const props = defineProps<{ projectId: string; modelValue: BusinessContext | null; version?: string; refreshKey?: unknown; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: BusinessContext | null] }>()
const contexts = ref<BusinessContext[]>([]), unknown = ref(0), error = ref(''), nextOffset = ref<number | null>(null)
let epoch = 0
let pinnedVersion: string | undefined
const selected = computed(() => props.modelValue === null ? '' : JSON.stringify(props.modelValue))
async function load(more = false) {
  const current = ++epoch
  error.value = ''
  const params = new URLSearchParams({ offset: String(more ? nextOffset.value ?? 0 : 0) })
  const version = more ? pinnedVersion || props.version : props.version
  if (version) params.set('version', version)
  try {
    const value = await request<{ version_id?: string; contexts: BusinessContext[]; support_counts: Record<string, number>; next_offset: number | null }>(`${projectPath(props.projectId)}/graph/contexts?${params}`)
    if (current !== epoch) return
    if (!more) pinnedVersion = value.version_id || props.version
    if (more && pinnedVersion && value.version_id !== pinnedVersion) throw new Error('图谱版本已变化，请重新读取适用范围。')
    contexts.value = more ? [...contexts.value, ...value.contexts] : value.contexts
    unknown.value = value.support_counts.unknown || 0
    nextOffset.value = value.next_offset
  } catch (e) { if (current === epoch) error.value = e instanceof Error ? e.message : '适用范围读取失败' }
}
const caption = (value: BusinessContext) => Object.entries(value).map(([key, val]) => `${key}：${String(val)}`).join(' · ')
watch([() => props.projectId, () => props.version, () => props.refreshKey], () => { contexts.value = []; pinnedVersion = undefined; nextOffset.value = null; void load() }, { immediate: true })
</script>
<template>
  <div class="business-context-select">
    <label>内容适用范围
      <select :value="selected" :disabled="disabled" @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value ? JSON.parse(($event.target as HTMLSelectElement).value) : null)">
        <option value="">全部内容</option>
        <option value="{}">仅通用知识</option>
        <option v-if="selected && selected !== '{}' && !contexts.some(c => JSON.stringify(c) === selected)" :value="selected">{{ caption(modelValue!) }}</option>
        <option v-for="context in contexts" :key="JSON.stringify(context)" :value="JSON.stringify(context)">{{ caption(context) }}</option>
      </select>
    </label>
    <button v-if="nextOffset !== null" type="button" class="text-button" @click="load(true)">加载更多范围</button>
    <p class="field-hint">指定范围时同时包含适用的通用知识。{{ unknown ? '部分内容尚未声明适用范围，只在“全部内容”中显示。' : '' }}</p>
    <p v-if="error" role="alert" class="note amber">{{ error }} <button type="button" class="text-button" @click="load()">重试</button></p>
  </div>
</template>
<style scoped>
.business-context-select { margin: 12px 0 } label { display: flex; gap: 12px; align-items: center; flex-wrap: wrap } select { max-width: 100%; min-width: 220px }
</style>
