<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
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
const scopeDialog = ref<HTMLDialogElement>()
async function openScope() {
  expanded.value = true
  await nextTick()
  scopeDialog.value?.showModal()
}
function closeScope() {
  scopeDialog.value?.close()
  expanded.value = false
}
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
  closeScope()
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
  closeScope()
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
        <button class="scope-toggle" type="button" aria-haspopup="dialog" :aria-labelledby="`scope-label-${file.file_id} scope-value-${file.file_id}`"
          :aria-expanded="expanded" :aria-controls="`scope-${file.file_id}`" @click="openScope">
          <span :id="`scope-value-${file.file_id}`" :title="bindingText">{{ bindingText }}</span><span aria-hidden="true">⌄</span>
        </button>
      </div>
    </div>
    <footer class="card-footer">
      <small v-if="!change || expanded">{{ file.size.toLocaleString() }} B</small>
      <div v-if="change && !expanded" class="save-row">
        <button class="text-button" :disabled="locked" @click="reset">撤销修改</button>
        <button class="btn small primary" :disabled="!canSave" @click="save">保存关联</button>
      </div>
      <div class="card-tools">
        <button class="text-button" :disabled="busy" @click="emit('preview', file)">查看原文</button>
        <button class="text-button danger-text" :aria-label="`删除文档 ${file.name}`" :disabled="locked" @click="emit('remove', file)">删除</button>
      </div>
    </footer>
    <dialog v-if="expanded" ref="scopeDialog" :id="`scope-${file.file_id}`" class="scope-dialog"
      :aria-labelledby="`scope-title-${file.file_id}`" @close="expanded = false">
      <header class="scope-head">
        <div><h3 :id="`scope-title-${file.file_id}`">关联范围</h3><p>{{ file.name }}</p></div>
        <button type="button" class="close" aria-label="关闭关联范围" @click="closeScope">×</button>
      </header>
      <div class="scope-editor">
        <div class="document-path"><span>项目路径</span><code>{{ file.relative_path }}</code></div>
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
      <footer class="scope-footer save-row">
        <button type="button" class="btn small" :disabled="locked || !change" @click="reset">撤销修改</button>
        <button type="button" class="btn small primary" :disabled="!canSave" @click="save">保存关联</button>
      </footer>
    </dialog>
  </article>
</template>

<style scoped>
.document-card { min-width: 0; background: white; border: 1px solid #dce4ed; border-radius: 12px; box-shadow: 0 2px 7px #233b5909; }
.card-head, .card-fields, .card-footer { padding-inline: 18px; }
.card-head { display: flex; align-items: center; gap: 10px; padding-block: 16px 12px; }
.card-head h3 { flex: 1; min-width: 0; font-size: 15px; line-height: 1.45; margin: 0; overflow-wrap: anywhere; }
.card-head .badge { white-space: normal; max-width: 9em; }
.card-icon { width: 34px; height: 36px; flex: none; padding: 7px; border-radius: 9px; color: #3560a4; background: #edf3fc; }
.card-fields { display: grid; grid-template-columns: minmax(95px, .8fr) minmax(0, 1.2fr); gap: 12px; padding-bottom: 16px; }
.card-field { display: grid; align-content: start; min-width: 0; gap: 5px; font-size: 12px; color: #53657e; }
.card-field select, .scope-toggle { width: 100%; height: 42px; padding: 6px 10px; font-size: 13px; color: #26334b; border-radius: 6px; border: 1px solid #d7e0ed; background-color: white; }
.scope-toggle { display: flex; align-items: center; justify-content: space-between; gap: 8px; text-align: left; }
.scope-toggle:hover { border-color: #8faed8; }
.scope-toggle span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-footer, .scope-footer, .scope-head, .card-tools, .save-row { display: flex; align-items: center; gap: 12px; }
.card-footer { min-height: 43px; padding-block: 6px; justify-content: space-between; border-top: 1px solid #e9edf3; background: #f9fbfd; border-radius: 0 0 12px 12px; }
.card-footer small { color: #63738b; font-variant-numeric: tabular-nums; }
.card-footer .text-button { font-size: 12px; }
.identity-path { margin: -4px 18px 10px; color: #5b6a80; font-size: 12px; overflow-wrap: anywhere; user-select: all; }
.scope-dialog { position: fixed; inset: 0; width: min(460px, calc(100vw - 24px)); max-height: calc(100dvh - 32px); padding: 0; border: 1px solid #cdd8e6; border-radius: 12px; color: inherit; box-shadow: 0 18px 64px #14274735; }
.scope-dialog[open] { display: flex; flex-flow: column; }
.scope-dialog::backdrop { background: #192e4b52; }
.scope-head, .scope-footer { justify-content: space-between; flex-shrink: 0; padding: 14px 18px; }
.scope-head { border-bottom: 1px solid var(--line); }
.scope-head h3 { font-size: 17px; }
.scope-head p, .document-path { color: #576981; overflow-wrap: anywhere; font-size: 12px; }
.scope-head .close { padding: 5px; }
.scope-editor { display: grid; gap: 12px; padding: 16px 18px; overflow: auto; overscroll-behavior: contain; }
.scope-editor > div:empty { display: none; }
.document-path, .restriction { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px; }
.document-path code { user-select: all; }
.scope-footer { justify-content: flex-end; border-top: 1px solid var(--line); }
.table-options { overflow-y: auto; max-height: 15rem; overscroll-behavior: contain; border: 1px solid var(--line); border-radius: 8px; padding: 3px; }
.table-option { display: flex; align-items: center; gap: 8px; padding: 6px; overflow-wrap: anywhere; cursor: pointer; }
.table-option:has(input:checked) { background: #eff5ff; }
.restriction { font-size: 12px; overflow-wrap: anywhere; }
@media(max-width: 550px) {
  .card-fields { grid-template-columns: minmax(0, 1fr); }
}
</style>
