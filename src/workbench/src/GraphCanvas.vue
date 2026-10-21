<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import type { Graph, GraphNode } from './types'

const props = defineProps<{ graph: Graph; layer: string; loading?: boolean }>()
const search = ref('')
const selectedKind = ref('')
const selected = ref<GraphNode | null>(null)
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
const compact = computed(() => visible.value.length <= 12)
const columns = computed(() => {
  const desired = Math.max(1, Math.min(visible.value.length, Math.ceil(Math.sqrt(visible.value.length * 1.7))))
  return compact.value ? Math.min(desired, Math.max(1, Math.floor(viewport.value.width / 190))) : desired
})
const width = computed(() => compact.value ? viewport.value.width : Math.max(viewport.value.width, columns.value * 190))
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
function move(event: PointerEvent) { if (drag) offset.value = { x: drag.ox + (event.clientX - drag.x) * drag.factor, y: drag.oy + (event.clientY - drag.y) * drag.factor } }
function start(event: PointerEvent) {
  if ((event.target as Element).closest('.graph-node')) return
  const rect = (event.currentTarget as Element).getBoundingClientRect()
  drag = { x: event.clientX, y: event.clientY, ox: offset.value.x, oy: offset.value.y, factor: Math.max(width.value / rect.width, height.value / rect.height) }
  ;(event.currentTarget as Element).setPointerCapture(event.pointerId)
}
watch(() => props.graph, () => { selected.value = null; reset() })
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
        <div class="actions"><button class="btn small" aria-label="缩小图谱" @click="zoom = Math.max(.3, zoom - .15)">−</button><span class="muted">{{ Math.round(zoom * 100) }}%</span><button class="btn small" aria-label="放大图谱" @click="zoom = Math.min(3, zoom + .15)">+</button><button class="btn small" @click="reset">复位</button></div>
      </div>
      <div v-if="loading" class="empty-state"><div class="spinner"></div>正在读取图谱</div>
      <div v-else-if="!graph.nodes.length" class="empty-state graph-empty"><div class="empty-graphic">◇</div><h3>{{ layer === 'metadata' ? '还没有元数据图' : '还没有业务图谱' }}</h3><p>{{ layer === 'metadata' ? '上传数据并读取元数据后，这里会展示真实的表、列及其关系。' : '完成抽取后，这里会展示已发布的实体与关系。现有图谱会在后续运行中保留并更新。' }}</p></div>
      <div v-else-if="!visible.length" class="empty-state">没有匹配的节点，请调整搜索或类型。</div>
      <div v-else ref="canvas" class="graph-canvas">
        <svg :viewBox="`0 0 ${width} ${height}`" role="group" :aria-label="layer === 'metadata' ? '元数据关联图' : '业务知识图谱'" @pointerdown="start" @pointermove="move" @pointerup="drag = null" @pointercancel="drag = null">
          <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#b6c4da"/></marker></defs>
          <g :transform="`translate(${offset.x + width / 2} ${offset.y + height / 2}) scale(${zoom}) translate(${-width / 2} ${-height / 2})`">
            <g v-for="(edge, index) in edges" :key="`${edge.source}-${edge.target}-${index}`" class="graph-edge" :class="{ emphasized: selected && (edge.source === selected.id || edge.target === selected.id) }">
              <path :d="`M ${points.get(edge.source)!.x} ${points.get(edge.source)!.y} L ${points.get(edge.target)!.x} ${points.get(edge.target)!.y}`" marker-end="url(#arrow)"/><title>{{ edge.relation || '关联' }}</title>
              <text v-if="selected && (edge.source === selected.id || edge.target === selected.id)" :x="(points.get(edge.source)!.x + points.get(edge.target)!.x) / 2" :y="(points.get(edge.source)!.y + points.get(edge.target)!.y) / 2 - 7">{{ edge.relation }}</text>
            </g>
            <g v-for="node in visible" :key="node.id" class="graph-node" :class="{ selected: selected?.id === node.id }" :transform="`translate(${points.get(node.id)!.x} ${points.get(node.id)!.y})`" tabindex="0" role="button" :aria-label="display(node)" @click="selected = node" @keydown.enter="selected = node" @keydown.space.prevent="selected = node">
              <circle r="22" :fill="kindColor(node) + '18'" :stroke="kindColor(node)"/><circle r="5" :fill="kindColor(node)"/>
              <text class="node-label" y="43">{{ shortLabel(node) }}</text><text class="node-kind" y="59">{{ node.kind || node.layer || '' }}</text><title>{{ display(node) }}</title>
            </g>
          </g>
        </svg>
      </div>
      <div class="panel-footer"><span>{{ graph.total_nodes ?? graph.nodes.length }} 个节点 · {{ graph.total_edges ?? graph.edges.length }} 条关系</span><span v-if="graph.truncated">服务端返回部分图谱（{{ graph.nodes.length }} 个节点），搜索仅覆盖已返回数据</span><span v-else-if="filtered.length > 240">当前展示前 240 个匹配节点，请搜索以缩小范围</span><span v-else>点击节点查看详情 · 拖动空白处平移</span></div>
    </section>
    <aside class="panel graph-detail">
      <div class="panel-head"><h2>节点详情</h2></div>
      <div v-if="!selected" class="empty-state">选择一个节点，查看属性和关联。</div>
      <div v-else class="panel-body"><span class="badge blue">{{ selected.kind || selected.layer || '节点' }}</span><h2 class="detail-name">{{ display(selected) }}</h2><p class="break muted small-text">{{ selected.id }}</p><div class="divider"></div><h3>节点属性</h3><pre class="code detail-code">{{ JSON.stringify(selected, null, 2) }}</pre><h3>关联关系 · {{ adjacent.length }}</h3><div v-for="(edge, i) in adjacent.slice(0, 40)" :key="i" class="edge-detail"><span class="badge">{{ edge.relation || '关联' }}</span><p>{{ edge.source }} → {{ edge.target }}</p></div><p v-if="adjacent.length > 40" class="muted small-text">仅展示前 40 条关系。</p></div>
    </aside>
  </div>
</template>
