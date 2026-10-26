<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { request, projectPath } from './api'
import type { Graph, GraphNode, GraphEdge } from './types'
import { fitCamera, zoomCamera } from './graphViewport'

const props = defineProps<{ graph: Graph; layer: string; loading?: boolean; projectId?: string; includeCandidates?: boolean }>()
const search = ref('')
const selectedKind = ref('')
const selected = ref<GraphNode | null>(null)
const selectedEdge = ref<GraphEdge | null>(null)
type Provenance = { facts: Array<{ id: string; fact_kind: string; status: string; payload: Record<string, unknown> }>; supports: Array<{ id: string; fact_id: string; status: string; origin: string; source_binding: string; rule_id: string; rule_revision: number; publication_id: string; document_ref?: string; scope_ref: string }>; rules: unknown[]; next_offset: number | null; supports_truncated: boolean }
const details = ref<Provenance | null>(null), detailsLoading = ref(false), detailsError = ref('')
let detailGeneration = 0
const statusName = (value?: unknown) => ({ accepted: '已接受', candidate: '候选', unresolved: '未决', conflict: '冲突' }[String(value)] || '状态未提供')
async function loadDetails(more = false) {
  const selector = selectedEdge.value?.id ? `fact_id=${encodeURIComponent(String(selectedEdge.value.id))}` : selected.value ? `entity_id=${encodeURIComponent(selected.value.id)}` : ''
  const epoch = ++detailGeneration
  if (!props.projectId || !selector) { details.value = null; return }
  const offset = more ? details.value?.next_offset : 0
  detailsLoading.value = true; detailsError.value = ''
  if (!more) details.value = null
  try {
    const value = await request<Provenance>(`${projectPath(props.projectId)}/graph/details?${selector}&include_candidates=${props.includeCandidates === true}&offset=${offset || 0}`)
    if (epoch !== detailGeneration) return
    details.value = more && details.value ? { ...value, facts: [...details.value.facts, ...value.facts], supports: [...details.value.supports, ...value.supports], rules: [...details.value.rules, ...value.rules] } : value
  } catch (error) { if (epoch === detailGeneration) detailsError.value = error instanceof Error ? error.message : '来源读取失败' }
  finally { if (epoch === detailGeneration) detailsLoading.value = false }
}
function selectNode(node: GraphNode) { selected.value = node; selectedEdge.value = null; void loadDetails() }
function selectEdge(edge: GraphEdge) { selectedEdge.value = edge; void loadDetails() }
function clearSelection() { detailGeneration++; selected.value = null; selectedEdge.value = null; details.value = null; detailsError.value = ''; detailsLoading.value = false }

const zoom = ref(1)
const offset = ref({ x: 0, y: 0 })
const canvas = ref<HTMLElement | null>(null)
const viewport = ref({ width: 820, height: 480 })
let observer: ResizeObserver | undefined
let drag: { x: number; y: number; ox: number; oy: number; factor: number } | null = null
const kinds = computed(() => [...new Set(props.graph.nodes.map(n => String(n.kind || n.layer || '节点')))].sort())
const filtered = computed(() => props.graph.nodes.filter(n => (!selectedKind.value || (n.kind || n.layer || '节点') === selectedKind.value)
  && (!search.value || `${n.label || n.name || ''} ${n.id}`.toLowerCase().includes(search.value.toLowerCase()))))
const visible = computed(() => filtered.value.slice(0, 240))
const columns = computed(() => {
  const desired = Math.max(1, Math.min(visible.value.length, Math.ceil(Math.sqrt(visible.value.length * 1.7))))
  return Math.min(desired, Math.max(1, Math.floor(viewport.value.width / 190)))
})
const width = computed(() => viewport.value.width)
const height = computed(() => Math.max(viewport.value.height, Math.ceil(visible.value.length / columns.value) * 118 + 50))
const points = computed(() => {
  const rows = Math.ceil(visible.value.length / columns.value)
  const top = Math.max(70, (height.value - (rows - 1) * 118) / 2 - 18)
  return new Map(visible.value.map((node, i) => {
    const row = Math.floor(i / columns.value)
    const inRow = Math.min(columns.value, visible.value.length - row * columns.value)
    return [node.id, { x: (width.value - (inRow - 1) * 190) / 2 + i % columns.value * 190, y: top + row * 118 }]
  }))
})
const edges = computed(() => props.graph.edges.filter(edge => points.value.has(edge.source) && points.value.has(edge.target)))
const adjacent = computed(() => selected.value ? props.graph.edges.filter(edge => edge.source === selected.value!.id || edge.target === selected.value!.id) : [])
const display = (node: GraphNode) => String(node.label || node.name || node.id)
const shortLabel = (node: GraphNode) => { const text = display(node); return text.length > 15 ? text.slice(0, 14) + '…' : text }
const kindColor = (node: GraphNode) => ['#407ff2', '#20a27e', '#9a6add', '#da9b37', '#54a6bc'][Math.max(0, kinds.value.indexOf(String(node.kind || node.layer || '节点'))) % 5]
function reset() { zoom.value = 1; offset.value = { x: 0, y: 0 } }
function setZoom(value: number) {
  const camera = zoomCamera({ zoom: zoom.value, offset: offset.value }, Math.max(.05, Math.min(3, value)), viewport.value)
  zoom.value = camera.zoom; offset.value = camera.offset
}
function fit() {
  const camera = fitCamera({ width: width.value, height: height.value }, viewport.value)
  zoom.value = camera.zoom; offset.value = camera.offset
}
function move(event: PointerEvent) { if (drag) offset.value = { x: drag.ox + (event.clientX - drag.x) * drag.factor, y: drag.oy + (event.clientY - drag.y) * drag.factor } }
function start(event: PointerEvent) {
  if (event.button !== 0 || (event.target as Element).closest('.graph-node, .graph-edge')) return
  const rect = (event.currentTarget as Element).getBoundingClientRect()
  drag = { x: event.clientX, y: event.clientY, ox: offset.value.x, oy: offset.value.y, factor: Math.max(viewport.value.width / rect.width, viewport.value.height / rect.height) }
  ;(event.currentTarget as Element).setPointerCapture(event.pointerId)
}
watch(() => props.graph, () => { clearSelection(); reset() })
watch([search, selectedKind], () => { clearSelection(); reset() })
watch(canvas, element => {
  observer?.disconnect()
  if (!element) return
  observer = new ResizeObserver(entries => {
    const rect = entries[0]?.contentRect
    if (rect?.width && rect.height) viewport.value = { width: Math.max(180, rect.width), height: rect.height }
  })
  observer.observe(element)
}, { flush: 'post' })
onUnmounted(() => observer?.disconnect())
</script>

<template>
  <div class="graph-layout">
    <section class="panel graph-panel">
      <div class="graph-toolbar">
        <div class="actions"><input v-model="search" class="graph-search" aria-label="搜索图节点" placeholder="搜索名称或编号"/><select v-model="selectedKind" aria-label="筛选节点类型"><option value="">全部类型</option><option v-for="kind in kinds" :key="kind">{{ kind }}</option></select></div>
        <div class="actions"><button class="btn small" aria-label="缩小图谱" @click="setZoom(zoom / 1.2)">−</button><span class="muted">{{ Math.round(zoom * 100) }}%</span><button class="btn small" aria-label="放大图谱" @click="setZoom(zoom * 1.2)">+</button><button class="btn small" @click="fit">适配全图</button><button class="btn small" @click="reset">原始大小</button></div>
      </div>
      <div v-if="loading" class="empty-state"><div class="spinner"></div>正在读取图谱</div>
      <div v-else-if="!graph.nodes.length" class="empty-state graph-empty"><div class="empty-graphic">◇</div><h3>{{ layer === 'metadata' ? '还没有元数据图' : '还没有业务图谱' }}</h3><p>{{ layer === 'metadata' ? '上传数据并读取元数据后，这里会展示真实的表、列及其关系。' : '完成抽取后，这里会展示已发布的实体与关系。现有图谱会在后续运行中保留并更新。' }}</p></div>
      <div v-else-if="!visible.length" class="empty-state">没有匹配的节点，请调整搜索或类型。</div>
      <div v-else ref="canvas" class="graph-canvas">
        <svg :viewBox="`0 0 ${viewport.width} ${viewport.height}`" role="group" :aria-label="layer === 'metadata' ? '元数据关联图' : '业务知识图谱'" @pointerdown="start" @pointermove="move" @pointerup="drag = null" @pointercancel="drag = null" @lostpointercapture="drag = null">
          <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#b6c4da"/></marker></defs>
          <g :transform="`translate(${offset.x} ${offset.y}) scale(${zoom})`">
            <g v-for="(edge, index) in edges" :key="`${edge.source}-${edge.target}-${index}`" class="graph-edge" :class="{ emphasized: selected && (edge.source === selected.id || edge.target === selected.id), candidate: edge.status === 'candidate', unresolved: edge.status === 'unresolved' }" tabindex="0" role="button" :aria-label="`${edge.relation || '关联'} · ${statusName(edge.status)}`" @click="selectEdge(edge)" @keydown.enter="selectEdge(edge)" @keydown.space.prevent="selectEdge(edge)">
              <path :d="`M ${points.get(edge.source)!.x} ${points.get(edge.source)!.y} L ${points.get(edge.target)!.x} ${points.get(edge.target)!.y}`" marker-end="url(#arrow)"/><title>{{ edge.relation || '关联' }} · {{ statusName(edge.status) }}</title>
              <text v-if="selected && (edge.source === selected.id || edge.target === selected.id)" :x="(points.get(edge.source)!.x + points.get(edge.target)!.x) / 2" :y="(points.get(edge.source)!.y + points.get(edge.target)!.y) / 2 - 7">{{ edge.relation }}</text>
            </g>
            <g v-for="node in visible" :key="node.id" class="graph-node" :class="{ selected: selected?.id === node.id, candidate: node.status === 'candidate', unresolved: node.status === 'unresolved' }" :transform="`translate(${points.get(node.id)!.x} ${points.get(node.id)!.y})`" tabindex="0" role="button" :aria-label="display(node)" @click="selectNode(node)" @keydown.enter="selectNode(node)" @keydown.space.prevent="selectNode(node)">
              <circle r="22" :fill="kindColor(node) + '18'" :stroke="kindColor(node)"/><circle r="5" :fill="kindColor(node)"/>
              <text class="node-label" y="43">{{ shortLabel(node) }}</text><text class="node-kind" y="59">{{ node.kind || node.layer || '' }} · {{ statusName(node.status) }}</text><title>{{ display(node) }} · {{ statusName(node.status) }}</title>
            </g>
          </g>
        </svg>
      </div>
      <div class="panel-footer"><span>{{ graph.total_nodes ?? graph.nodes.length }} 个节点 · {{ graph.total_edges ?? graph.edges.length }} 条关系</span><span v-if="graph.truncated">服务端返回部分图谱（{{ graph.nodes.length }} 个节点），搜索仅覆盖已返回数据</span><span v-else-if="filtered.length > 240">当前展示前 240 个匹配节点，请搜索以缩小范围</span><span v-else>点击节点查看详情 · 拖动空白处平移</span></div>
    </section>
    <aside class="panel graph-detail">
      <div class="panel-head"><h2>{{ selectedEdge ? '关系详情' : '节点详情' }}</h2></div>
      <div v-if="!selected && !selectedEdge" class="empty-state">选择节点或关系，查看属性和来源支持。</div>
      <div v-else class="panel-body">
        <template v-if="selectedEdge"><span class="badge" :class="selectedEdge.status === 'accepted' ? 'green' : 'amber'">{{ statusName(selectedEdge.status) }}</span><h2>{{ selectedEdge.relation }}</h2><p class="break">{{ selectedEdge.source }} → {{ selectedEdge.target }}</p><pre class="code detail-code">{{ JSON.stringify(selectedEdge.qualifiers || {}, null, 2) }}</pre></template>
        <template v-else-if="selected"><span class="badge blue">{{ selected.kind || selected.layer || '节点' }}</span> <span class="badge" :class="selected.status === 'accepted' ? 'green' : 'amber'">{{ statusName(selected.status) }}</span><h2 class="detail-name">{{ display(selected) }}</h2><p class="break muted small-text">{{ selected.id }}</p><div class="divider"></div><h3>节点属性</h3><pre class="code detail-code">{{ JSON.stringify(selected, null, 2) }}</pre><h3>关联关系 · {{ adjacent.length }}</h3><button v-for="(edge, i) in adjacent.slice(0, 40)" :key="i" type="button" class="edge-detail relation-selection" @click="selectEdge(edge)"><span class="badge">{{ edge.relation || '关联' }} · {{ statusName(edge.status) }}</span><p>{{ edge.source }} → {{ edge.target }}</p></button><p v-if="adjacent.length > 40" class="muted small-text">仅展示前 40 条关系；来源详情按事实分页读取。</p></template>
        <section v-if="projectId" aria-label="来源支持">
          <div class="divider"></div><h3>来源支持</h3>
          <p v-if="detailsLoading" role="status">正在读取来源…</p><p v-if="detailsError" role="alert">{{ detailsError }}</p>
          <template v-if="details"><article v-for="fact in details.facts" :key="fact.id" class="fact-detail"><strong>{{ fact.fact_kind === 'PropertyValue' ? fact.payload.name : fact.fact_kind === 'Edge' ? fact.payload.relation : '实体存在' }}</strong> <span class="badge">{{ statusName(fact.status) }}</span><pre class="code detail-code">{{ JSON.stringify(fact.payload, null, 2) }}</pre><ul><li v-for="support in details.supports.filter(item => item.fact_id === fact.id)" :key="`${support.publication_id}:${support.id}`"><span>{{ statusName(support.status) }} · {{ support.origin }}</span><p class="break">来源绑定：{{ support.source_binding }}<br/>规则：{{ support.rule_id }} @ {{ support.rule_revision }}<br/>范围：{{ support.scope_ref }}<br/>发布：{{ support.publication_id }}<template v-if="support.document_ref"><br/>文档：{{ support.document_ref }}</template></p></li></ul></article><details v-if="details.rules.length"><summary>字段映射与语义依据</summary><pre class="code detail-code">{{ JSON.stringify(details.rules, null, 2) }}</pre></details><p v-if="details.supports_truncated" class="note amber">本页支持超过 500 条，仅展示前 500 条。</p><button v-if="details.next_offset !== null" class="btn small" :disabled="detailsLoading" @click="loadDetails(true)">读取更多事实支持</button></template>
        </section>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.graph-edge { cursor: pointer; }
.graph-edge.candidate path, .graph-node.candidate > circle:first-child { stroke-dasharray: 6 4; }
.graph-edge.unresolved path, .graph-node.unresolved > circle:first-child { stroke-dasharray: 2 4; }
.relation-selection { display: block; width: 100%; text-align: left; background: transparent; color: inherit; cursor: pointer; border: 0; }
.fact-detail { padding: 12px 0; border-bottom: 1px solid var(--border, #e2e8f0); }
.fact-detail ul { padding-left: 18px; font-size: 12px; }
</style>
