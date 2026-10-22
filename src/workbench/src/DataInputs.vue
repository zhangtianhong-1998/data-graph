<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import MetadataPreview from './MetadataPreview.vue'
import { isDatabaseSource, sourceKindLabel } from './database'
import { projectPath, request } from './api'
import type { Metadata, ProjectFile, SourceConfig } from './types'

const props = defineProps<{ projectId: string; sources: SourceConfig[]; files: ProjectFile[]; metadata: Metadata; locked: boolean }>()
const emit = defineEmits<{ connect: [source?: SourceConfig]; upload: []; refresh: []; preview: [file: ProjectFile]; import: [file: ProjectFile] }>()
const databases = computed(() => props.sources.filter(isDatabaseSource))
const localFiles = computed(() => props.files.filter(file => file.kind === 'excel' || file.kind === 'csv'))
const yamlFiles = computed(() => props.files.filter(file => file.kind === 'yaml'))
const kind = ref<'database' | 'file'>(databases.value.length || !localFiles.value.length ? 'database' : 'file')
watch([() => databases.value.length, () => localFiles.value.length], ([db, local], [oldDb, oldLocal]) => {
  if (db > oldDb) kind.value = 'database'
  else if (local > oldLocal) kind.value = 'file'
})
const selected = ref(''), files = ref<Array<{ name: string; content: string }>>([])
const pending = ref(false), error = ref('')
let revision = 0
const entries = computed(() => kind.value === 'database'
  ? databases.value.flatMap(source => (Array.isArray(source.allowed_tables) ? source.allowed_tables as string[] : []).map(name => {
    const separator = name.indexOf('.')
    return { key: `db:${source.id}:${name}`, label: name, sourceId: source.id, schema: separator < 0 ? 'public' : name.slice(0, separator), table: separator < 0 ? name : name.slice(separator + 1) }
  }))
  : localFiles.value.map(file => ({ key: `file:${file.file_id}`, label: file.name, sourceId: props.sources.find(source => source.path === file.relative_path)?.id || '', schema: '', table: '' })))
const current = computed(() => entries.value.find(item => item.key === selected.value) || entries.value[0])
const selectedTables = computed(() => current.value ? props.metadata.tables.filter(table => table.source_id === current.value!.sourceId && (!current.value!.table || table.schema === current.value!.schema && table.name === current.value!.table)) : [])
watch(entries, values => { if (!values.some(item => item.key === selected.value)) selected.value = values[0]?.key || '' }, { immediate: true })
// Each selection owns its request; an older response must never replace a newer selection.
watch([current, () => props.metadata], async () => {
  const epoch = ++revision, item = current.value
  files.value = []; error.value = ''; pending.value = false
  if (!item?.sourceId || !selectedTables.value.length) return
  pending.value = true
  const query = new URLSearchParams({ source_id: item.sourceId })
  if (item.table) { query.set('schema', item.schema); query.set('table', item.table) }
  try {
    const value = await request<{ files: typeof files.value }>(`${projectPath(props.projectId)}/metadata/yaml?${query}`)
    if (epoch === revision) files.value = value.files
  } catch (reason) { if (epoch === revision) error.value = reason instanceof Error ? reason.message : '元数据读取失败' }
  finally { if (epoch === revision) pending.value = false }
}, { immediate: true })
onUnmounted(() => { revision++ })
</script>

<template>
  <div class="input-workspace">
    <nav class="panel input-types" aria-label="数据类型">
      <button :class="{ active: kind === 'database' }" :aria-pressed="kind === 'database'" @click="kind = 'database'"><span>数据库</span><small>{{ databases.length }}</small></button>
      <button :class="{ active: kind === 'file' }" :aria-pressed="kind === 'file'" @click="kind = 'file'"><span>本地文件</span><small>{{ localFiles.length }}</small></button>
    </nav>
    <section class="panel input-selection">
      <div class="panel-head"><h2>{{ kind === 'database' ? '已选数据表' : '数据文件' }}</h2><button class="btn primary small" :disabled="locked" @click="kind === 'database' ? emit('connect') : emit('upload')">{{ kind === 'database' ? '＋ 连接' : '＋ 上传' }}</button></div>
      <div class="input-items">
        <template v-if="kind === 'database'">
          <section v-for="source in databases" :key="source.id" class="database-group">
            <div class="source-heading"><div><strong>{{ source.id }}</strong><small>{{ sourceKindLabel(source.kind) }}</small></div><button class="text-button" :disabled="locked" :aria-label="`配置 ${source.id}`" @click="emit('connect', source)">配置</button></div>
            <button v-for="item in entries.filter(entry => entry.sourceId === source.id)" :key="item.key" class="input-item" :class="{ active: current?.key === item.key }" :aria-pressed="current?.key === item.key" @click="selected = item.key"><span>{{ item.label }}</span><small>表</small></button>
          </section>
          <div v-if="!databases.length" class="empty-state">尚未连接数据库</div>
        </template>
        <template v-else>
          <button v-for="item in entries" :key="item.key" class="input-item" :class="{ active: current?.key === item.key }" :aria-pressed="current?.key === item.key" @click="selected = item.key"><span>{{ item.label }}</span><small>{{ item.label.split('.').pop()?.toUpperCase() }}</small></button>
          <div v-if="!localFiles.length" class="empty-state">上传 Excel 或 CSV</div>
        </template>
      </div>
      <div class="panel-footer"><span>{{ entries.length }} {{ kind === 'database' ? '张表' : '个文件' }}</span><button class="text-button" :disabled="locked || !sources.length" @click="emit('refresh')">刷新元数据</button></div>
      <details v-if="kind === 'file' && yamlFiles.length" class="config-files"><summary>配置文件 · {{ yamlFiles.length }}</summary><div v-for="file in yamlFiles" :key="file.file_id" class="file-row"><div class="file-info"><h3>{{ file.name }}</h3></div><button class="text-button" :disabled="locked" @click="emit('preview', file)">查看</button><button class="text-button" :disabled="locked" @click="emit('import', file)">导入</button></div></details>
    </section>
    <div class="input-metadata">
      <p v-if="error" class="note amber" role="alert">{{ error }}</p>
      <p v-if="current && !selectedTables.length && !pending" class="note amber">尚未读取此数据的元数据</p>
      <MetadataPreview :key="current?.key || 'empty'" :tables="selectedTables" :files="files" :loading="pending" :title="current?.label || '元数据'"/>
    </div>
  </div>
</template>
