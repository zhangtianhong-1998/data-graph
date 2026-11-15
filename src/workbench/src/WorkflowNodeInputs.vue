<script setup lang="ts">
import { computed } from 'vue'
import type { ProjectFile, SourceConfig, WorkflowUnitConfig } from './types'

type NodeInputs = Omit<WorkflowUnitConfig, 'unit_id' | 'label' | 'position'>
const props = defineProps<{
  node: NodeInputs
  sources: SourceConfig[]
  tablesForSource: (source: SourceConfig) => string[]
  documents: ProjectFile[]
  prefix: string
  locked: boolean
}>()
const emit = defineEmits<{ update: [patch: Partial<NodeInputs>] }>()
const catalog = computed(() => props.sources.map(s => ({ id: s.id, tables: props.tablesForSource(s) })))
// The runtime treats an empty source list as inherited access to every project source.
const selectedSources = computed(() => props.node.source_ids.length ? props.node.source_ids : props.sources.map(s => s.id))
function selectedTables(source: string, tables: string[]): string[] {
  return selectedSources.value.includes(source) ? props.node.source_table_overrides[source] || tables : []
}
function isOnlySelection(source: string, tables: string[], table?: string) {
  if (selectedSources.value.length !== 1 || !selectedSources.value.includes(source)) return false
  if (!table) return true
  const selected = selectedTables(source, tables)
  return selected.length === 1 && selected[0] === table
}
function select(source: string, tables: string[] | null) {
  const ids = selectedSources.value.filter(id => id !== source)
  const overrides = { ...props.node.source_table_overrides }
  delete overrides[source]
  if (tables === null || tables.length > 0) {
    ids.push(source)
    if (tables !== null) overrides[source] = tables
  }
  if (ids.length) emit('update', { source_ids: ids, source_table_overrides: overrides })
}
function toggleTable(source: string, tables: string[], table: string) {
  const current = selectedTables(source, tables)
  select(source, current.includes(table) ? current.filter(t => t !== table) : [...current, table])
}
function selectOnly(source: string, table: string) {
  emit('update', { source_ids: [source], source_table_overrides: { [source]: [table] } })
}
function docKeys(file: ProjectFile) { return [file.relative_path, file.file_id, file.name] }
function includesDoc(file: ProjectFile) { return props.node.document_ids?.some(id => docKeys(file).includes(id)) ?? true }
function toggleDoc(file: ProjectFile) {
  const ids = props.node.document_ids || []
  emit('update', { document_ids: includesDoc(file) ? ids.filter(id => !docKeys(file).includes(id)) : [...ids, file.relative_path] })
}
function inputValue(event: Event) { return (event.target as HTMLInputElement).value }
function numberValue(event: Event) { return inputValue(event) === '' ? null : Number(inputValue(event)) }
function projectionValue(event: Event) { const value = inputValue(event); return value === 'inherit' ? null : value === 'true' }
</script>

<template>
  <div class="unit-section">
    <div class="unit-section-head">
      <h3>数据表</h3>
      <button v-if="node.source_ids.length || Object.keys(node.source_table_overrides).length" class="text-button" type="button" :disabled="locked" @click="emit('update', { source_ids: [], source_table_overrides: {} })">恢复全部来源</button>
    </div>
    <p v-if="!catalog.length" class="field-hint">项目尚未接入数据源。</p>
    <div v-else class="inspector-scroll-box">
      <div v-for="source in catalog" :key="source.id" class="source-box">
        <div class="source-box-head"><strong>{{ source.id }}</strong></div>
        <div v-for="tbl in source.tables" :key="tbl" class="wf-table-choice">
          <label class="check-option compact">
            <input type="checkbox" :checked="selectedTables(source.id, source.tables).includes(tbl)" :disabled="locked || isOnlySelection(source.id, source.tables, tbl)" @change="toggleTable(source.id, source.tables, tbl)" />
            <span class="mono">{{ tbl }}</span>
          </label>
          <button type="button" class="text-button" :aria-label="`仅选择 ${source.id} 的 ${tbl}`" :disabled="locked || isOnlySelection(source.id, source.tables, tbl)" @click="selectOnly(source.id, tbl)">仅此表</button>
        </div>
        <label v-if="!source.tables.length" class="check-option compact">
          <input type="checkbox" :checked="selectedSources.includes(source.id)" :disabled="locked || isOnlySelection(source.id, [])" @change="select(source.id, selectedSources.includes(source.id) ? [] : null)" />使用整个来源
        </label>
      </div>
    </div>
  </div>
  <div class="unit-section">
    <div class="unit-section-head"><h3>知识文档</h3></div>
    <div class="radio-line">
      <label><input type="radio" :name="`${prefix}-doc-mode`" :checked="node.document_ids === null" :disabled="locked" @change="emit('update', { document_ids: null })" />全部项目文档</label>
      <label><input type="radio" :name="`${prefix}-doc-mode`" :checked="node.document_ids !== null" :disabled="locked" @change="emit('update', { document_ids: documents.map(f => f.relative_path) })" />仅选定文档</label>
    </div>
    <div v-if="node.document_ids !== null" class="inspector-doc-list">
      <p v-if="!documents.length" class="field-hint">项目配置中没有可用的 Markdown 文档。</p>
      <label v-for="doc in documents" :key="doc.file_id" class="check-option compact">
        <input type="checkbox" :checked="includesDoc(doc)" :disabled="locked" @change="toggleDoc(doc)" />{{ doc.name }}
      </label>
    </div>
  </div>
  <div class="field">
    <label :for="`${prefix}-goal`">本节点补充抽取目标</label>
    <textarea :id="`${prefix}-goal`" :value="node.goal_markdown || ''" maxlength="32000" :disabled="locked" @input="emit('update', { goal_markdown: inputValue($event) })" />
  </div>
  <div class="field">
    <label :for="`${prefix}-filter`">数据筛选提示</label>
    <textarea :id="`${prefix}-filter`" :value="node.probe_filter_hint || ''" maxlength="2000" :disabled="locked" @input="emit('update', { probe_filter_hint: inputValue($event) })" />
  </div>
  <div class="field">
    <label :for="`${prefix}-projection`">联合投影</label>
    <select :id="`${prefix}-projection`" :value="node.enable_co_projection == null ? 'inherit' : String(node.enable_co_projection)" :disabled="locked" @change="emit('update', { enable_co_projection: projectionValue($event) })">
      <option value="inherit">继承全局配置</option><option value="true">启用</option><option value="false">关闭</option>
    </select>
  </div>
  <div class="two-fields">
    <div class="field"><label :for="`${prefix}-calls`">调用上限</label><input :id="`${prefix}-calls`" type="number" min="0" step="1" :value="node.max_calls ?? ''" :disabled="locked" placeholder="继承全局" @input="emit('update', { max_calls: numberValue($event) })" /></div>
    <div class="field"><label :for="`${prefix}-seconds`">时限（秒）</label><input :id="`${prefix}-seconds`" type="number" min="0.1" step="any" :value="node.max_run_seconds ?? ''" :disabled="locked" placeholder="继承全局" @input="emit('update', { max_run_seconds: numberValue($event) })" /></div>
  </div>
</template>

<style scoped>
.wf-table-choice { display: flex; align-items: center; justify-content: space-between; gap: .5rem; }
.wf-table-choice label { min-width: 0; overflow-wrap: anywhere; }
.wf-table-choice button { flex: none; }
</style>
