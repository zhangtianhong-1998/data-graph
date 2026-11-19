<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ensureSession, projectPath, request } from './api'
import type { BusinessContext, Graph } from './types'

interface SnapshotAnalysis {
  version_id?: string
  status: string
  counts?: { nodes: number; edges: number; communities: number; isolated_nodes: number }
  algorithms?: { community?: { method: string }; pagerank?: { converged: boolean } }
  nodes?: Array<{ id: string; display_name?: string; community_id: string; degree_centrality: number; pagerank: number }>
  communities?: Array<{ id: string; size: number }>
}
const props = defineProps<{projectId: string; version: string; businessContext: BusinessContext | null; graph: Graph}>()
const data = ref<SnapshotAnalysis | null>(null)
const error = ref('')
const action = ref('')
const open = ref(false)
const statusText: Record<string, string> = {
  not_built: '尚未计算', queued: '排队中', running: '正在计算', failed: '计算失败',
  completed: '已完成', completed_with_gaps: '已完成，含缺口',
}
const pending = computed(() => data.value?.status === 'queued' || data.value?.status === 'running')
const labels = computed(() => new Map(props.graph.nodes.map(node => [node.id, node.label || node.id])))
const params = computed(() => {
  const value = new URLSearchParams({version: props.version})
  if (props.businessContext !== null) value.set('business_context', JSON.stringify(props.businessContext))
  return value
})
const graphPath = computed(() => projectPath(props.projectId) + '/graph')
const versionKey = computed(() => graphPath.value + '?' + params.value)
let epoch = 0
let timer: ReturnType<typeof setTimeout>
let controller: AbortController

function poll(id: number) {
  clearTimeout(timer)
  if (id === epoch && pending.value) timer = setTimeout(() => { void load(id) }, 2000)
}
async function load(id: number, start = false) {
  clearTimeout(timer)
  if (!props.version) return
  const project = props.projectId
  const query = new URLSearchParams(params.value)
  if (!start) query.set('limit', '10')
  else if (data.value?.counts) query.set('force', 'true')
  const url = graphPath.value + '/analytics?' + query
  error.value = ''
  try {
    await ensureSession(project)
    if (id !== epoch) return
    const result = await request<SnapshotAnalysis>(url, {method: start ? 'POST' : 'GET', signal: controller.signal})
    if (id !== epoch) return
    if (result.version_id && result.version_id !== props.version) throw new Error('图谱版本已变化，请重新加载。')
    data.value = result
    if (start && !pending.value) await load(id)
    else poll(id)
  } catch (err) {
    if (id === epoch) error.value = err instanceof Error ? err.message : '读取离线分析失败'
  }
}
function recompute() {
  if (pending.value || action.value) return
  const id = ++epoch
  action.value = 'compute'
  void load(id, true).finally(() => { if (id === epoch) action.value = '' })
}
async function download(format: 'semantica-json' | 'graphml') {
  if (action.value) return
  const id = epoch
  const project = props.projectId
  const query = new URLSearchParams(params.value)
  query.set('format', format)
  const url = graphPath.value + '/export?' + query
  action.value = 'download'
  error.value = ''
  try {
    await ensureSession(project)
    if (id !== epoch) return
    const blob = await request<Blob>(url, {responseType: 'blob', signal: controller.signal})
    if (id !== epoch) return
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = project + '-' + props.version + (format === 'graphml' ? '.graphml' : '.json')
    link.click()
    setTimeout(() => URL.revokeObjectURL(link.href), 1000)
  } catch (err) {
    if (id === epoch) error.value = err instanceof Error ? err.message : '导出失败'
  } finally {
    if (id === epoch) action.value = ''
  }
}
watch(versionKey, () => {
  controller?.abort()
  controller = new AbortController()
  data.value = null
  action.value = error.value = ''
  void load(++epoch)
}, {immediate: true})
onBeforeUnmount(() => { epoch++; controller?.abort(); clearTimeout(timer) })
</script>

<template>
  <section class="analysis-panel">
    <div class="analysis-tools">
      <button type="button" class="btn small" :aria-expanded="open" @click="open = !open">全图分析 · {{ statusText[data?.status || ''] || (error ? '暂不可用' : '读取中') }}</button>
      <details class="analysis-download">
        <summary>导出图谱</summary>
        <div class="actions">
          <button type="button" class="btn small" :disabled="Boolean(action)" @click="download('semantica-json')">Semantica JSON</button>
          <button type="button" class="btn small" :disabled="Boolean(action)" @click="download('graphml')">GraphML</button>
        </div>
      </details>
    </div>
    <p v-if="error" class="field-error" role="alert">{{ error }} <button type="button" class="text-button" @click="load(epoch)">重试</button></p>
    <div v-if="open" class="analysis-content">
      <div class="actions">
        <span v-if="data?.counts">{{ data.counts.nodes }} 个节点 · {{ data.counts.edges }} 条关系 · {{ data.counts.communities }} 个社区 · {{ data.counts.isolated_nodes }} 个孤立节点</span>
        <button type="button" class="btn small" :disabled="pending || Boolean(action)" @click="recompute">{{ data?.counts ? '重新计算' : '计算当前范围' }}</button>
      </div>
      <div v-if="data?.nodes?.length" class="table-scroll">
        <table><thead><tr><th>节点</th><th>PageRank</th><th>度中心度</th><th>所属社区</th></tr></thead>
          <tbody><tr v-for="node in data.nodes" :key="node.id">
            <td>{{ node.display_name || labels.get(node.id) || node.id }}</td><td>{{ node.pagerank.toPrecision(4) }}</td><td>{{ node.degree_centrality.toPrecision(4) }}</td><td :title="node.community_id">{{ node.community_id.slice(-10) }}</td>
          </tr></tbody>
        </table>
      </div>
      <details v-if="data?.communities?.length"><summary>按规模查看社区</summary>
        <ul><li v-for="community in data.communities" :key="community.id"><code :title="community.id">{{ community.id.slice(-10) }}</code> · {{ community.size }} 个节点</li></ul>
      </details>
    </div>
  </section>
</template>

<style scoped>
.analysis-panel { margin-block: 8px; overflow-wrap: anywhere }
.analysis-tools, .analysis-download .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px }
.analysis-download summary, .analysis-content summary { cursor: pointer; font-size: 13px }
.analysis-download[open] .actions { padding-block: 6px }
.analysis-content { display: grid; gap: 12px; margin-top: 10px; padding: 14px; border: 1px solid var(--line); border-radius: 8px }
.analysis-content ul { margin-block: 8px; list-style: none; padding: 0 }
</style>
