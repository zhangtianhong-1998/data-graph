<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { request, projectPath } from './api'
import type { Graph, GraphNode, GraphEdge } from './types'
import { fitCamera, zoomCamera } from './graphViewport'
import { SCENE_LIMIT, SCENE_EDGE_LIMIT, mergeScene, placeNodes, sceneBounds, hitNode } from './graphScene'
import type { Point } from './graphScene'

const props = defineProps<{ graph: Graph; layer: string; loading?: boolean; projectId?: string; includeCandidates?: boolean }>()
const search = ref('')
const selectedKind = ref('')
const remote = ref<Graph | null>(null), pageLoading = ref(false), pageError = ref('')
const graph = computed(() => remote.value ?? props.graph)
const serverPaging = computed(() => !!props.projectId && props.graph.page_size !== undefined)
let pageGeneration = 0, searchTimer: ReturnType<typeof setTimeout> | undefined
const searchMatches = ref<string[] | null>(null)
const expansionOffsets = ref(new Map<string, number | null>())
let sceneController: AbortController | undefined
const matchedCount = computed(() => searchMatches.value ? Number(graph.value.matched_nodes ?? searchMatches.value.length) : filtered.value.length)
const overview = computed(() => graph.value.kind_counts as Record<string, number> | undefined)
const atCapacity = computed(() => graph.value.nodes.length >= SCENE_LIMIT || graph.value.edges.length >= SCENE_EDGE_LIMIT)
async function loadScene(options: { focus?: string; q?: string; kind?: string; more?: boolean; replace?: boolean } = {}) {
  const epoch = ++pageGeneration
  sceneController?.abort(); sceneController = new AbortController()
  pageLoading.value = true; pageError.value = ''
  const focus = options.focus || ''
  const start = options.more ? expansionOffsets.value.get(focus) || 0 : 0
  const params = new URLSearchParams({ layer: props.layer, include_candidates: String(props.includeCandidates === true), offset: String(start), limit: focus ? '240' : '1000', q: options.q || '', kind: options.kind || '', focus })
  try {
    let value = await request<Graph>(`${projectPath(props.projectId!)}/graph?${params}`, { signal: sceneController.signal })
    if (epoch !== pageGeneration) return
    if (graph.value.generation && value.generation !== graph.value.generation) {
      // A new publication cannot be merged into the old scene or provenance.
      params.set('offset', '0'); value = await request<Graph>(`${projectPath(props.projectId!)}/graph?${params}`, { signal: sceneController.signal })
      if (epoch !== pageGeneration) return
      clearSelection(); positions.value = placeNodes(value); expansionOffsets.value.clear()
      remote.value = value
    } else remote.value = options.replace ? value : mergeScene(graph.value, value)
    if (options.replace) { clearSelection(); positions.value = placeNodes(value); expansionOffsets.value.clear() }
    if (focus) expansionOffsets.value.set(focus, value.next_offset as number | null)
    if (options.q || options.kind) searchMatches.value = value.nodes.map(n => n.id)
    if (options.q && value.nodes[0]) { if (!graph.value.nodes.some(n => n.id === value.nodes[0]!.id)) { remote.value = value; positions.value = placeNodes(value); expansionOffsets.value.clear() }; selectNode(value.nodes[0]); await nextTick(); focusNode(value.nodes[0]) }
    else if (!focus) { await nextTick(); fit() }
  } catch (e) { if (epoch === pageGeneration) pageError.value = e instanceof Error ? e.message : '图谱读取失败' }
  finally { if (epoch === pageGeneration) pageLoading.value = false }
}
async function expandNode() {
  if (!selected.value || atCapacity.value || !serverPaging.value) return
  const id = selected.value.id
  await loadScene({ focus: id, more: expansionOffsets.value.has(id) })
}
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
const kinds = computed(() => serverPaging.value && Array.isArray(graph.value.kinds) ? graph.value.kinds as string[] : [...new Set(graph.value.nodes.map(n => String(n.kind || n.layer || '节点')))].sort())
const filtered = computed(() => graph.value.nodes.filter(n =>
  (!selectedKind.value || (n.kind || n.layer || '节点') === selectedKind.value) &&
  (!search.value || (serverPaging.value && searchMatches.value ? searchMatches.value.includes(n.id) :
    `${n.label || n.name || ''} ${n.id}`.toLowerCase().includes(search.value.toLowerCase())))))
// Searching highlights and locates without removing the surrounding graph.
const visible = computed(() => graph.value.nodes.slice(0, SCENE_LIMIT))
const positions = ref<Map<string, Point>>(new Map())
const points = computed(() => positions.value)
const bounds = computed(() => sceneBounds(points.value))
const largeScene = computed(() => visible.value.length > 300)
const bitmap = ref<HTMLCanvasElement | null>(null)
const edges = computed(() => graph.value.edges.filter(edge => points.value.has(edge.source) && points.value.has(edge.target)).slice(0, SCENE_EDGE_LIMIT))
const accessibleNodes = computed(() => (search.value || selectedKind.value ? filtered.value : visible.value).slice(0, 40))
watch(() => graph.value, value => { positions.value = placeNodes({ ...value, nodes: value.nodes.slice(0, SCENE_LIMIT) }, positions.value) }, { immediate: true, flush: 'sync' })
const adjacent = computed(() => selected.value ? graph.value.edges.filter(edge => edge.source === selected.value!.id || edge.target === selected.value!.id) : [])
const display = (node: GraphNode) => String(node.label || node.name || node.id)
const scopeLabel = (node: GraphNode) => Array.isArray(node.scopes) ? node.scopes.join('、') : ''
const shortScope = (node: GraphNode) => { const text = scopeLabel(node); return text.length > 18 ? text.slice(0, 17) + '…' : text }
const shortLabel = (node: GraphNode) => { const text = display(node); return text.length > 15 ? text.slice(0, 14) + '…' : text }
const kindColor = (node: GraphNode) => ['#407ff2', '#20a27e', '#9a6add', '#da9b37', '#54a6bc'][Math.max(0, kinds.value.indexOf(String(node.kind || node.layer || '节点'))) % 5]
function reset() { zoom.value = 1; offset.value = { x: 0, y: 0 } }
function setZoom(value: number) {
  const camera = zoomCamera({ zoom: zoom.value, offset: offset.value }, Math.max(.001, Math.min(3, value)), viewport.value)
  zoom.value = camera.zoom; offset.value = camera.offset
}
function fit() {
  const camera = fitCamera(bounds.value, viewport.value)
  zoom.value = camera.zoom; offset.value = camera.offset
}
function move(event: PointerEvent) { if (drag) offset.value = { x: drag.ox + (event.clientX - drag.x) * drag.factor, y: drag.oy + (event.clientY - drag.y) * drag.factor } }
function start(event: PointerEvent) {
  if (event.button !== 0 || (event.target as Element).closest('.graph-node, .graph-edge')) return
  const rect = (event.currentTarget as Element).getBoundingClientRect()
  drag = { x: event.clientX, y: event.clientY, ox: offset.value.x, oy: offset.value.y, factor: Math.max(viewport.value.width / rect.width, viewport.value.height / rect.height) }
  ;(event.currentTarget as Element).setPointerCapture(event.pointerId)
}
function focusNode(node: GraphNode) {
  const p = points.value.get(node.id)
  if (p) { zoom.value = 1; offset.value = { x: viewport.value.width / 2 - p.x, y: viewport.value.height / 2 - p.y } }
}
function wheel(event: WheelEvent) { setZoom(zoom.value * (event.deltaY > 0 ? .9 : 1.1)) }
let pointerStart: { x: number; y: number } | null = null
function bitmapDown(event: PointerEvent) { pointerStart = { x: event.clientX, y: event.clientY }; start(event) }
function bitmapUp(event: PointerEvent) {
  drag = null
  if (!pointerStart || Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 5) return
  const rect = bitmap.value!.getBoundingClientRect()
  const x = ((event.clientX - rect.left) * viewport.value.width / rect.width - offset.value.x) / zoom.value
  const y = ((event.clientY - rect.top) * viewport.value.height / rect.height - offset.value.y) / zoom.value
  const node = hitNode(visible.value, points.value, x, y, Math.max(22, 6 / zoom.value))
  if (node) selectNode(node)
  pointerStart = null
}
function paint() {
  const element = bitmap.value
  if (!element) return
  const ctx = element.getContext('2d')
  if (!ctx) return
  const ratio = window.devicePixelRatio || 1
  element.width = viewport.value.width * ratio; element.height = viewport.value.height * ratio
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, viewport.value.width, viewport.value.height)
  ctx.translate(offset.value.x, offset.value.y); ctx.scale(zoom.value, zoom.value)
  const left = -offset.value.x / zoom.value, top = -offset.value.y / zoom.value
  const right = left + viewport.value.width / zoom.value, bottom = top + viewport.value.height / zoom.value
  for (const edge of edges.value) {
    const a = points.value.get(edge.source)!, b = points.value.get(edge.target)!
    if (Math.max(a.x, b.x) < left || Math.min(a.x, b.x) > right || Math.max(a.y, b.y) < top || Math.min(a.y, b.y) > bottom) continue
    ctx.strokeStyle = (selectedEdge.value === edge || (selected.value && [edge.source, edge.target].includes(selected.value.id))) ? '#407ff2' : '#b6c4da'
    ctx.lineWidth = 1 / zoom.value; ctx.setLineDash(edge.status === 'candidate' ? [6, 4] : edge.status === 'unresolved' ? [2, 4] : [])
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke()
    const angle = Math.atan2(b.y-a.y, b.x-a.x), tx=b.x-23*Math.cos(angle), ty=b.y-23*Math.sin(angle)
    ctx.beginPath(); ctx.moveTo(tx,ty); ctx.lineTo(tx-8*Math.cos(angle-.5),ty-8*Math.sin(angle-.5)); ctx.moveTo(tx,ty); ctx.lineTo(tx-8*Math.cos(angle+.5),ty-8*Math.sin(angle+.5)); ctx.stroke()
  }
  const matches = new Set(filtered.value.map(n => n.id))
  for (const node of visible.value) {
    const p = points.value.get(node.id)!
    if (p.x < left-110 || p.x > right+110 || p.y < top-90 || p.y > bottom+90) continue
    ctx.globalAlpha = (search.value || selectedKind.value) && !matches.has(node.id) ? .25 : 1
    ctx.fillStyle = kindColor(node); ctx.strokeStyle = selected.value?.id === node.id ? '#172744' : kindColor(node)
    ctx.setLineDash(node.status === 'candidate' ? [6,4] : node.status === 'unresolved' ? [2,4] : [])
    ctx.beginPath(); ctx.arc(p.x,p.y,zoom.value < .3 ? 12 : 22,0,Math.PI*2); ctx.fill(); ctx.stroke()
    if (zoom.value >= .4 || selected.value?.id === node.id) {
      ctx.fillStyle = '#26364f'; ctx.textAlign = 'center'; ctx.font = '13px sans-serif'; ctx.fillText(shortLabel(node),p.x,p.y+43)
      ctx.font = '11px sans-serif'; ctx.fillText(`${node.kind || node.layer || ''} · ${statusName(node.status)}`,p.x,p.y+59)
      if (scopeLabel(node)) ctx.fillText(shortScope(node),p.x,p.y+75)
    }
  }
  ctx.globalAlpha = 1
}
watch([bitmap, positions, zoom, offset, viewport, selected, selectedEdge, search, selectedKind], paint, { flush: 'post' })
watch(() => props.graph, () => {
  pageGeneration++; sceneController?.abort(); remote.value = null; pageLoading.value = false; positions.value = placeNodes({ ...props.graph, nodes: props.graph.nodes.slice(0, SCENE_LIMIT) })
  search.value = ''; searchMatches.value = null; selectedKind.value = ''; expansionOffsets.value.clear(); clearSelection(); reset()
  if (serverPaging.value && Number(props.graph.total_nodes) <= 1000 && props.graph.next_offset != null) void loadScene()
}, { immediate: true })
watch([search, selectedKind], () => {
  pageGeneration++; sceneController?.abort(); clearTimeout(searchTimer); pageLoading.value = false; searchMatches.value = null
  if (serverPaging.value && (search.value || selectedKind.value)) searchTimer = setTimeout(() => { void loadScene({ q: search.value, kind: selectedKind.value }) }, 200)
  else if (filtered.value[0]) focusNode(filtered.value[0])
})
watch(canvas, element => {
  observer?.disconnect()
  if (!element) return
  let initialSize = true
  observer = new ResizeObserver(entries => {
    const rect = entries[0]?.contentRect
    if (rect?.width && rect.height) { viewport.value = { width: Math.max(180, rect.width), height: rect.height }; if (initialSize) { initialSize = false; fit() } }
  })
  observer.observe(element); void nextTick(fit)
}, { flush: 'post' })
onUnmounted(() => { pageGeneration++; sceneController?.abort(); clearTimeout(searchTimer); observer?.disconnect() })
</script>

<template>
  <div class="graph-layout">
    <section class="panel graph-panel">
      <div class="graph-toolbar">
        <div class="actions"><input v-model="search" class="graph-search" aria-label="搜索图节点" placeholder="搜索名称或编号"/><select v-model="selectedKind" aria-label="筛选节点类型"><option value="">全部类型</option><option v-for="kind in kinds" :key="kind">{{ kind }}</option></select></div>
        <div class="actions"><button class="btn small" aria-label="缩小图谱" @click="setZoom(zoom / 1.2)">−</button><span class="muted">{{ Math.round(zoom * 100) }}%</span><button class="btn small" aria-label="放大图谱" @click="setZoom(zoom * 1.2)">+</button><button class="btn small" @click="fit">适配画布</button><button class="btn small" @click="reset">原始大小</button></div>
      </div>
      <div v-if="overview" class="kind-overview" aria-label="全图类型概览"><button v-for="(count, kind) in overview" :key="kind" class="btn small" @click="selectedKind = String(kind)">{{ kind }} · {{ count }}</button></div>
      <p v-if="pageLoading" role="status">正在扩展画布…</p>
      <p v-if="pageError" role="alert">{{ pageError }}</p>
      <div v-if="loading" class="empty-state"><div class="spinner"></div>正在读取图谱</div>
      <div v-else-if="!graph.nodes.length && !search && !selectedKind" class="empty-state graph-empty"><div class="empty-graphic">◇</div><h3>{{ layer === 'metadata' ? '还没有元数据图' : '还没有业务图谱' }}</h3><p>{{ layer === 'metadata' ? '上传数据并读取元数据后，这里会展示真实的表、列及其关系。' : '完成抽取后，这里会展示已发布的实体与关系。现有图谱会在后续运行中保留并更新。' }}</p></div>
      <div v-else-if="!visible.length" class="empty-state">没有匹配的节点，请调整搜索或类型。</div>
      <div v-else ref="canvas" class="graph-canvas" @wheel.prevent="wheel">
        <canvas v-if="largeScene" ref="bitmap" class="graph-bitmap" role="img" :aria-label="`连续图谱画布，${visible.length} 个节点；使用节点列表选择对象`" @pointerdown="bitmapDown" @pointermove="move" @pointerup="bitmapUp" @pointercancel="drag = null" @lostpointercapture="drag = null"/>
        <svg v-else :viewBox="`0 0 ${viewport.width} ${viewport.height}`" role="group" :aria-label="layer === 'metadata' ? '元数据关联图' : '业务知识图谱'" @pointerdown="start" @pointermove="move" @pointerup="drag = null" @pointercancel="drag = null" @lostpointercapture="drag = null">
          <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#b6c4da"/></marker></defs>
          <g :transform="`translate(${offset.x} ${offset.y}) scale(${zoom})`">
            <g v-for="(edge, index) in edges" :key="`${edge.source}-${edge.target}-${index}`" class="graph-edge" :class="{ emphasized: selected && (edge.source === selected.id || edge.target === selected.id), candidate: edge.status === 'candidate', unresolved: edge.status === 'unresolved' }" tabindex="0" role="button" :aria-label="`${edge.relation || '关联'} · ${statusName(edge.status)}`" @click="selectEdge(edge)" @keydown.enter="selectEdge(edge)" @keydown.space.prevent="selectEdge(edge)">
              <path :d="`M ${points.get(edge.source)!.x} ${points.get(edge.source)!.y} L ${points.get(edge.target)!.x} ${points.get(edge.target)!.y}`" marker-end="url(#arrow)"/><title>{{ edge.relation || '关联' }} · {{ statusName(edge.status) }}</title>
              <text v-if="selected && (edge.source === selected.id || edge.target === selected.id)" :x="(points.get(edge.source)!.x + points.get(edge.target)!.x) / 2" :y="(points.get(edge.source)!.y + points.get(edge.target)!.y) / 2 - 7">{{ edge.relation }}</text>
            </g>
            <g v-for="node in visible" :key="node.id" class="graph-node" :class="{ dimmed: (search || selectedKind) && !filtered.includes(node), selected: selected?.id === node.id, candidate: node.status === 'candidate', unresolved: node.status === 'unresolved' }" :transform="`translate(${points.get(node.id)!.x} ${points.get(node.id)!.y})`" tabindex="0" role="button" :aria-label="display(node)" @click="selectNode(node)" @keydown.enter="selectNode(node)" @keydown.space.prevent="selectNode(node)">
              <circle r="22" :fill="kindColor(node) + '18'" :stroke="kindColor(node)"/><circle r="5" :fill="kindColor(node)"/>
              <text class="node-label" y="43">{{ shortLabel(node) }}</text><text class="node-kind" y="59">{{ node.kind || node.layer || '' }} · {{ statusName(node.status) }}</text><text v-if="scopeLabel(node)" class="node-kind node-scope" y="75">{{ shortScope(node) }}</text><title>{{ display(node) }} · {{ statusName(node.status) }} · {{ scopeLabel(node) }}</title>
            </g>
          </g>
        </svg>
      </div>
      <div class="panel-footer"><span>画布已加载 {{ visible.length }} / 全图 {{ graph.total_nodes ?? graph.nodes.length }} 个节点 · 已加载 {{ edges.length }} / 全图 {{ graph.total_edges ?? graph.edges.length }} 条关系</span><span v-if="search || selectedKind">匹配 {{ matchedCount }} 个节点（结果最多 1000 个）</span><span v-if="graph.truncated">来源返回部分图谱，尚未加载的节点和边不表示不存在</span><span v-else-if="serverPaging && visible.length < Number(graph.total_nodes)">全图搜索或选择节点展开关联；画布保留已加载内容</span><span v-else>拖动平移 · 滚轮缩放 · 点击节点查看来源</span><span v-if="atCapacity">画布已达上限（{{ SCENE_LIMIT }} 个节点 / {{ SCENE_EDGE_LIMIT }} 条关系），请清空后定位新的对象</span><button v-if="serverPaging && (atCapacity || visible.length > 1000)" class="btn small" @click="loadScene({ q: search, kind: selectedKind, replace: true })">清空并聚焦当前检索</button></div>
      <details v-if="largeScene || search || selectedKind" class="scene-node-list"><summary>定位节点 · {{ filtered.length }} 个匹配</summary><button v-for="node in accessibleNodes" :key="node.id" class="btn small" @click="selectNode(node); focusNode(node)">{{ display(node) }} · {{ statusName(node.status) }} · {{ scopeLabel(node) }}</button><p v-if="filtered.length > 40">列表展示前 40 个匹配，请搜索缩小范围；画布仍保留已加载节点。</p></details>
    </section>
    <aside class="panel graph-detail">
      <div class="panel-head"><h2>{{ selectedEdge ? '关系详情' : '节点详情' }}</h2></div>
      <div v-if="!selected && !selectedEdge" class="empty-state">选择节点或关系，查看属性和来源支持。</div>
      <div v-else class="panel-body">
        <template v-if="selectedEdge"><span class="badge" :class="selectedEdge.status === 'accepted' ? 'green' : 'amber'">{{ statusName(selectedEdge.status) }}</span><h2>{{ selectedEdge.relation }}</h2><p class="break">{{ selectedEdge.source }} → {{ selectedEdge.target }}</p><pre class="code detail-code">{{ JSON.stringify(selectedEdge.qualifiers || {}, null, 2) }}</pre></template>
        <template v-else-if="selected"><span class="badge blue">{{ selected.kind || selected.layer || '节点' }}</span> <span class="badge" :class="selected.status === 'accepted' ? 'green' : 'amber'">{{ statusName(selected.status) }}</span><h2 class="detail-name">{{ display(selected) }}</h2><p class="break muted small-text">{{ selected.id }}</p><div class="divider"></div><h3>节点属性</h3><pre class="code detail-code">{{ JSON.stringify(selected, null, 2) }}</pre><h3>已加载关联 · {{ adjacent.length }}</h3><button v-if="serverPaging" class="btn small" :disabled="pageLoading || atCapacity || expansionOffsets.get(selected.id) === null" @click="expandNode">{{ expansionOffsets.has(selected.id) ? '继续展开关联' : '展开相邻节点' }}</button><button v-for="(edge, i) in adjacent.slice(0, 40)" :key="i" type="button" class="edge-detail relation-selection" @click="selectEdge(edge)"><span class="badge">{{ edge.relation || '关联' }} · {{ statusName(edge.status) }}</span><p>{{ edge.source }} → {{ edge.target }}</p></button><p v-if="adjacent.length > 40" class="muted small-text">仅展示前 40 条关系；来源详情按事实分页读取。</p></template>
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
.graph-bitmap { display: block; width: 100%; height: 480px; touch-action: none; }
.graph-node.dimmed { opacity: .25; }
.kind-overview, .scene-node-list { padding: 8px 16px; display: flex; flex-wrap: wrap; gap: 8px; }
.graph-edge { cursor: pointer; }
.graph-edge.candidate path, .graph-node.candidate > circle:first-child { stroke-dasharray: 6 4; }
.graph-edge.unresolved path, .graph-node.unresolved > circle:first-child { stroke-dasharray: 2 4; }
.relation-selection { display: block; width: 100%; text-align: left; background: transparent; color: inherit; cursor: pointer; border: 0; }
.fact-detail { padding: 12px 0; border-bottom: 1px solid var(--border, #e2e8f0); }
.fact-detail ul { padding-left: 18px; font-size: 12px; }
</style>
