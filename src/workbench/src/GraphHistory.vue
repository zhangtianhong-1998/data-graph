<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { projectPath, request } from './api'

type Version = {
  version_id: string; created_at?: string; run_id?: string; node_id?: string
  graph_kind?: string; coverage?: string; nodes?: number; edges?: number
  is_current?: boolean; error_code?: string
}
const props = defineProps<{ projectId: string; modelValue: string; refreshKey?: unknown; configRevision?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const versions = ref<Version[]>([]), error = ref(''), loading = ref(false)
const previousRoot = ref(''), migrating = ref(false), migrationError = ref('')
let epoch = 0, controller: AbortController | undefined
const selected = computed(() => versions.value.find(item => item.version_id === props.modelValue))
function caption(item: Version) {
  const kind = ({ project_full: '项目图谱', node_full: '节点完整图', node_base: '补充抽取前的合并基础图', node_delta: '节点增量', draft: '草稿' } as Record<string, string>)[item.graph_kind || ''] || '历史图谱'
  const date = item.created_at && Number.isFinite(Date.parse(item.created_at)) ? new Date(item.created_at).toLocaleString() : ''
  return [date, kind, item.node_id, !date ? item.run_id || item.version_id : '', item.is_current ? '当前发布' : '', item.coverage === 'incomplete' || item.coverage === 'conflict_isolated' ? '覆盖不完整' : '', item.error_code ? '暂不可读' : ''].filter(Boolean).join(' · ') || item.version_id
}
async function refresh() {
  const requestEpoch = ++epoch
  controller?.abort(); controller = new AbortController(); loading.value = true; error.value = ''
  try {
    const value = await request<{ versions: Version[] }>(`${projectPath(props.projectId)}/graph/versions`, { signal: controller.signal })
    if (requestEpoch === epoch) versions.value = value.versions || []
  } catch (e) { if (requestEpoch === epoch) error.value = e instanceof Error ? e.message : '历史图谱列表读取失败' }
  finally { if (requestEpoch === epoch) loading.value = false }
}
watch(() => props.projectId, () => { versions.value = []; void refresh() }, { immediate: true })
watch(() => props.refreshKey, () => { void refresh() })
watch(() => [props.projectId, props.modelValue], () => { previousRoot.value = ''; migrationError.value = '' })
async function migrate() {
  const { projectId, modelValue: version, configRevision } = props
  if (!version || !previousRoot.value.trim() || !configRevision || migrating.value) return
  migrating.value = true; migrationError.value = ''
  try {
    await request(`${projectPath(projectId)}/graph/versions/${encodeURIComponent(version)}/migrate`, {
      method: 'POST', headers: { 'If-Match': configRevision },
      body: JSON.stringify({ previous_project_root: previousRoot.value.trim() }),
    })
    if (projectId === props.projectId && version === props.modelValue) {
      await refresh()
      if (projectId === props.projectId && version === props.modelValue) emit('update:modelValue', version)
    }
  } catch (e) { if (projectId === props.projectId && version === props.modelValue) migrationError.value = e instanceof Error ? e.message : '无法核验原项目路径' }
  finally { migrating.value = false }
}
onUnmounted(() => { epoch++; controller?.abort() })
</script>

<template>
  <section class="graph-history" aria-label="图谱版本">
    <label for="graph-version">查看版本</label>
    <select id="graph-version" :value="modelValue" @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)">
      <option value="">当前项目输出</option>
      <option v-if="modelValue && !selected" :value="modelValue">已选择的历史版本</option>
      <option v-for="item in versions" :key="item.version_id" :value="item.version_id">{{ caption(item) }}</option>
    </select>
    <button class="btn small" :disabled="loading" @click="refresh">{{ loading ? '读取中…' : '刷新版本' }}</button>
    <p v-if="selected?.graph_kind === 'node_delta'" class="field-hint">此快照只包含节点增量。</p>
    <p v-if="selected?.run_id" class="field-hint">所属运行：{{ selected.run_id }}</p>
    <template v-if="selected?.error_code === 'graph_access_context_changed'">
      <p class="field-hint">如果这是搬迁前生成的旧图，请填写搬迁前存放 project.yaml 的目录绝对路径。系统按原路径复算旧指纹，并核对当前访问权限。</p>
      <label>搬迁前的项目路径 <input v-model="previousRoot" type="text" autocomplete="off" /></label>
      <button class="btn small" :disabled="migrating || !previousRoot.trim() || !configRevision" @click="migrate">核验并读取旧图</button>
      <p v-if="migrationError" role="alert" class="note amber">{{ migrationError }}</p>
    </template>
    <p v-if="error" role="alert" class="note amber">{{ error }}</p>
  </section>
</template>

<style scoped>
.graph-history { min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: end; gap: 5px 8px; }
.graph-history > label { grid-column: 1 / -1; font-size: 12px; }
select { width: 100%; min-width: 0; }
p { grid-column: 1 / -1; margin: 0; overflow-wrap: anywhere; }
</style>
