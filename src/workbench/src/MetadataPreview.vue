<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Table } from './types'
const props = defineProps<{ tables: Table[]; files: Array<{ name: string; content: string }>; loading: boolean; title?: string }>()
const view = ref<'tables' | 'yaml'>('tables')
const selectedFile = ref('')
watch(() => props.files, files => { if (!files.some(file => file.name === selectedFile.value)) selectedFile.value = files[0]?.name || '' }, { immediate: true })
const yaml = computed(() => props.files.find(file => file.name === selectedFile.value))
function download() {
  if (!yaml.value) return
  const url = URL.createObjectURL(new Blob([yaml.value.content], { type: 'text/yaml;charset=utf-8' }))
  const link = document.createElement('a'); link.href = url; link.download = yaml.value.name; link.click(); URL.revokeObjectURL(url)
}
</script>
<template>
  <section class="panel schema-preview">
    <div class="panel-head"><h2>{{ title || '元数据预览' }}</h2><span class="badge">{{ tables.length }} 张表</span></div>
    <div class="preview-switch" role="tablist" aria-label="元数据预览方式"><button role="tab" :aria-selected="view === 'tables'" :class="{ active: view === 'tables' }" @click="view = 'tables'">表与列</button><button role="tab" :aria-selected="view === 'yaml'" :class="{ active: view === 'yaml' }" @click="view = 'yaml'">YAML</button></div>
    <div v-if="loading" class="empty-state">正在读取元数据…</div>
    <div v-else-if="!tables.length && !files.length" class="empty-state"><h3>先接入一份数据</h3><p>上传 Excel / CSV 或连接数据库后，<br/>这里会显示表结构与 YAML。</p><p>仅预览元数据，不展示业务样本。</p></div>
    <template v-else-if="view === 'tables'">
      <div class="schema-table-list"><details v-for="table in tables" :key="`${table.source_id}.${table.schema}.${table.name}`" :open="tables.length === 1"><summary>{{ table.schema }}.{{ table.name }} <span class="muted">{{ table.columns.length }} 列</span></summary><p class="schema-source">{{ table.source_id }}</p><div class="table-scroll"><table><thead><tr><th>字段</th><th>类型</th><th>注释</th></tr></thead><tbody><tr v-for="column in table.columns" :key="column.name"><td>{{ column.name }}</td><td>{{ column.data_type || '—' }}</td><td>{{ column.comment || '—' }}</td></tr></tbody></table></div></details></div>
      <div class="panel-footer"><span>列、约束与外键的完整声明可在 YAML 中查看</span></div>
    </template>
    <template v-else><div class="yaml-toolbar"><select v-model="selectedFile" aria-label="元数据 YAML 文件"><option v-for="file in files" :key="file.name" :value="file.name">{{ file.name }}</option></select><button class="btn small" :disabled="!yaml" @click="download">下载</button></div><pre class="code yaml-preview">{{ yaml?.content || '此来源尚未生成 YAML' }}</pre><div class="panel-footer"><span>当前为只读预览；可下载后修订，不会直接改写源表。</span></div></template>
  </section>
</template>
