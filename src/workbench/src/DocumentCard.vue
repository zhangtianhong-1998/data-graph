<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { DocumentScope, ProjectFile } from './types'

const props = defineProps<{ file: ProjectFile; showPath: boolean; active: boolean; tableKeys: string[]; locked: boolean; busy: boolean }>()
const emit = defineEmits<{
  save: [file: ProjectFile, purpose: string, scope: DocumentScope]
  preview: [file: ProjectFile]
  remove: [file: ProjectFile]
}>()
const purposeLabels: Record<string, string> = {
  data_description: '数据关联', analysis_knowledge: '分析知识', mixed: '混合文档', unresolved: '待分类',
}
const expanded = ref(false), search = ref('')
const purpose = ref(''), useGlobal = ref(false)
const scope = ref<DocumentScope>({ tables: [], columns: [], source_ids: [] })
const saved = computed(() => {
  const value = props.file.scope
  return {
    purpose: props.file.purpose || 'data_description',
    source_ids: value?.source_ids || [], tables: value?.tables || [], columns: value?.columns || [],
  }
})
const hasLimits = (value: DocumentScope) => !!(value.tables.length || value.source_ids.length || value.columns.length)
const pending = computed(() => props.file.inactive_reason === 'pending_assignment')
const savedGlobal = computed(() => !pending.value && !hasLimits(saved.value))
function reset() {
  purpose.value = saved.value.purpose
  useGlobal.value = savedGlobal.value
  scope.value = { source_ids: [...saved.value.source_ids], tables: [...saved.value.tables], columns: [...saved.value.columns] }
  expanded.value = false
  search.value = ''
}
// A refresh of a different card must leave this card's unsaved selection intact.
watch(() => JSON.stringify([props.file.file_id, saved.value, props.active, pending.value]), reset, { immediate: true })
const change = computed(() => purpose.value !== saved.value.purpose || useGlobal.value !== savedGlobal.value ||
  (['tables', 'columns', 'source_ids'] as const).some(key =>
    scope.value[key].length !== saved.value[key].length || scope.value[key].some(item => !saved.value[key].includes(item))))
const canSave = computed(() => !props.locked && change.value && (useGlobal.value || hasLimits(scope.value)))
const visibleTables = computed(() => {
  const needle = search.value.trim().toLowerCase()
  return [...new Set([...props.tableKeys, ...scope.value.tables])].filter(key => key.toLowerCase().includes(needle))
})
const bindingText = computed(() => {
  if (useGlobal.value) return '全局使用'
  if (scope.value.tables.length === 1) return scope.value.tables[0]
  if (scope.value.tables.length) return `已关联 ${scope.value.tables.length} 张表`
  if (hasLimits(scope.value)) return '已限定来源或列'
  return pending.value ? '待分配' : '选择关联表'
})
function save() {
  if (!canSave.value) return
  const { source_ids, tables, columns } = scope.value
  emit('save', props.file, purpose.value, useGlobal.value ? { source_ids: [], tables: [], columns: [] } :
    { source_ids: [...source_ids], tables: [...tables], columns: [...columns] })
}
</script>

<template>
  <article class="document-card" :aria-labelledby="`doc-title-${file.file_id}`">
    <div class="card-head">
      <svg class="card-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h4"/></svg>
      <h3 :id="`doc-title-${file.file_id}`" :title="file.relative_path">{{ file.name }}</h3>
      <span v-if="!active" class="badge amber">{{ file.inactive_reason === 'binding_target_removed' ? '关联表已移除 · 未参与抽取' : '未参与抽取' }}</span>
      <div class="card-tools">
        <button class="text-button" :disabled="busy" @click="emit('preview', file)">查看原文</button>
        <button class="text-button danger-text" :aria-label="`删除文档 ${file.name}`" :disabled="locked" @click="emit('remove', file)">删除</button>
      </div>
    </div>
    <p v-if="showPath" class="identity-path"><code>{{ file.manifest_path || file.relative_path }}</code></p>
    <div class="card-fields">
      <label class="card-field"><span>文档类型</span>
        <select v-model="purpose" :disabled="locked">
          <option v-for="(label, value) in purposeLabels" :key="value" :value="value">{{ label }}</option>
        </select>
      </label>
      <div class="card-field">
        <span :id="`scope-label-${file.file_id}`">关联范围</span>
        <button class="scope-toggle" type="button" :aria-labelledby="`scope-label-${file.file_id} scope-value-${file.file_id}`"
          :aria-expanded="expanded" :aria-controls="`scope-${file.file_id}`" @click="expanded = !expanded">
          <span :id="`scope-value-${file.file_id}`" :title="bindingText">{{ bindingText }}</span><span aria-hidden="true">⌄</span>
        </button>
      </div>
    </div>
    <div v-if="expanded" :id="`scope-${file.file_id}`" class="scope-editor">
      <div class="document-path"><span>项目路径</span><code>{{ file.relative_path }}</code><span>· {{ file.size.toLocaleString() }} B</span></div>
      <div v-if="file.manifest_path && file.manifest_path !== file.relative_path" class="document-path"><span>清单路径</span><code>{{ file.manifest_path }}</code></div>
      <div class="radio-line">
        <label><input v-model="useGlobal" type="radio" :value="false" :name="`scope-mode-${file.file_id}`" :disabled="locked"/>关联数据表</label>
        <label><input v-model="useGlobal" type="radio" :value="true" :name="`scope-mode-${file.file_id}`" :disabled="locked"/>全局使用</label>
      </div>
      <template v-if="!useGlobal">
        <div v-for="restriction in (['source_ids', 'columns'] as const)" :key="restriction">
          <div v-if="scope[restriction].length" class="restriction">
            <span>{{ restriction === 'columns' ? '限定列' : '限定来源' }} · {{ scope[restriction].join('、') }}</span>
            <button class="text-button" :disabled="locked" @click="scope[restriction] = []">清除{{ restriction === 'columns' ? '列限制' : '来源限制' }}</button>
          </div>
        </div>
        <input v-if="tableKeys.length" v-model="search" type="search" aria-label="筛选关联表" placeholder="筛选表名或来源"/>
        <div v-if="visibleTables.length" class="table-options" role="group" aria-label="可多选的关联表">
          <label v-for="key in visibleTables" :key="key" class="table-option">
            <input v-model="scope.tables" type="checkbox" :value="key" :disabled="locked"/><span>{{ key }}</span>
          </label>
        </div>
        <p v-else class="field-hint">{{ search ? '没有匹配的表' : '请先在数据接入中选择数据表。' }}</p>
      </template>
    </div>
    <div v-if="change" class="save-row">
      <button class="text-button" :disabled="locked" @click="reset">撤销修改</button>
      <button class="btn small primary" :disabled="!canSave" @click="save">保存关联</button>
    </div>
  </article>
</template>

<style scoped>
.document-card { padding: 14px 18px 16px; background: white; border: 1px solid var(--line); border-radius: 8px; min-width: 0; }
.card-head, .card-tools, .save-row { display: flex; gap: 12px; align-items: center; }
.card-head { flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.card-head h3 { font-size: 15px; margin: 0; overflow-wrap: anywhere; }
.card-icon { width: 19px; height: 21px; flex: none; color: #617796; }
.card-tools { margin-left: auto; }
.card-tools button { font-size: 12px; }
.identity-path { margin: -6px 0 10px; color: #5b6a80; font-size: 12px; overflow-wrap: anywhere; user-select: all; }
.card-fields { display: grid; grid-template-columns: minmax(110px, .65fr) minmax(170px, 1.35fr); gap: 12px; }
.card-field { display: grid; min-width: 0; gap: 4px; font-size: 12px; color: #5b6a80; }
.card-field select, .scope-toggle { min-height: 37px; width: 100%; color: #26334b; }
.scope-toggle { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 12px; background: white; border-radius: 6px; border: 1px solid #dce3ed; text-align: left; font-size: 14px; }
.scope-toggle span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.scope-editor { display: grid; gap: 8px; margin-top: 12px; border-top: 1px solid var(--line); padding-top: 10px; }
.document-path { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px; font-size: 12px; color: #5b6a80; }
.document-path code { user-select: all; overflow-wrap: anywhere; }
.scope-editor > div:empty { display: none; }
.table-options { overflow-y: auto; max-height: 13rem; overscroll-behavior: contain; }
.table-option { display: flex; align-items: center; gap: 9px; padding: 5px 2px; overflow-wrap: anywhere; }
.restriction { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; overflow-wrap: anywhere; }
.save-row { justify-content: flex-end; margin-top: 10px; }
@media(max-width: 550px) {
  .document-card { padding: 12px; }
  .card-fields { grid-template-columns: minmax(0, 1fr); gap: 6px; }
}
</style>
