<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import WorkflowNodeInputs from './WorkflowNodeInputs.vue'
import type {
  InheritedGraphRef,
  Metadata,
  Project,
  ProjectFile,
  SourceConfig,
  WorkflowConfig,
  WorkflowEdgeConfig,
  WorkflowMergeNodeConfig,
  WorkflowPosition,
  WorkflowUnitConfig,
} from './types'

const props = defineProps<{
  projectId: string
  projects: Project[]
  sources: SourceConfig[]
  tables: Metadata['tables']
  documents: ProjectFile[]
  scopes: string[]
  workflow: WorkflowConfig | null
  inheritedGraphs: InheritedGraphRef[]
  locked: boolean
  dirty: boolean
  runDisabled?: boolean
}>()

const emit = defineEmits<{
  'update:workflow': [value: WorkflowConfig | null]
  'update:inheritedGraphs': [value: InheritedGraphRef[]]
  save: []
  startRun: []
  refreshTables: []
}>()

function normalizeWorkflow(raw: WorkflowConfig | null): WorkflowConfig {
  if (!raw) {
    return {
      enabled: false,
      max_parallel_workers: 2,
      enable_co_projection: false,
      units: [],
      merge_nodes: [],
      edges: [],
      output_position: { x: 640, y: 180 },
    }
  }
  const units = (raw.units || []).map((u, idx) => ({
    ...u,
    source_ids: [...(u.source_ids || [])],
    source_table_overrides: { ...(u.source_table_overrides || {}) },
    document_ids: u.document_ids === undefined ? null : u.document_ids ? [...u.document_ids] : null,
    position: u.position ? { ...u.position } : { x: 60, y: 40 + idx * 140 },
  }))
  const mergeNodes = (raw.merge_nodes || []).map((m, idx) => ({
    ...m,
    merge_mode: m.merge_mode || 'deterministic',
    source_ids: [...(m.source_ids || [])],
    source_table_overrides: { ...(m.source_table_overrides || {}) },
    document_ids: m.document_ids === undefined ? null : m.document_ids ? [...m.document_ids] : null,
    position: m.position ? { ...m.position } : { x: 350, y: 120 + idx * 150 },
  }))
  return {
    ...raw,
    enabled: Boolean(raw.enabled),
    max_parallel_workers: raw.max_parallel_workers || 2,
    enable_co_projection: Boolean(raw.enable_co_projection),
    units,
    merge_nodes: mergeNodes,
    edges: (raw.edges || []).map(e => ({ ...e })),
    output_position: raw.output_position ? { ...raw.output_position } : { x: 640, y: 170 },
  }
}

const current = ref<WorkflowConfig>(normalizeWorkflow(props.workflow))
watch(
  () => props.workflow,
  val => {
    current.value = normalizeWorkflow(val)
  },
  { deep: true },
)

const editorError = ref('')
const selectedId = ref<string>(current.value.units[0]?.unit_id || 'output')
watch(
  () => [current.value.units.map(u => u.unit_id), (current.value.merge_nodes || []).map(m => m.node_id)],
  () => {
    const valid = new Set([
      ...current.value.units.map(u => u.unit_id),
      ...(current.value.merge_nodes || []).map(m => m.node_id),
      'output',
    ])
    if (!valid.has(selectedId.value)) {
      selectedId.value = current.value.units[0]?.unit_id || current.value.merge_nodes?.[0]?.node_id || 'output'
    }
  },
)

function commit(next: WorkflowConfig) {
  if (props.locked) return
  const cloned = JSON.parse(JSON.stringify(next)) as WorkflowConfig
  current.value = cloned
  emit('update:workflow', cloned)
}

function setEnabled(enabled: boolean) {
  const next = normalizeWorkflow(current.value)
  next.enabled = enabled
  if (enabled && next.units.length === 0) {
    const unit = makeExtractNode(next.units, next.merge_nodes || [], { x: 60, y: 140 }, '抽取节点 1')
    next.units.push(unit)
    next.edges = [{ edge_id: `e_${unit.unit_id}_out`, source: unit.unit_id, target: 'output' }]
    selectedId.value = unit.unit_id
  }
  commit(next)
}

function nextId(prefix: string, used: Set<string>): string {
  let n = 1
  while (used.has(`${prefix}_${n}`)) n++
  return `${prefix}_${n}`
}

function makeExtractNode(
  units: WorkflowUnitConfig[],
  merges: WorkflowMergeNodeConfig[],
  pos: WorkflowPosition,
  label?: string,
): WorkflowUnitConfig {
  const used = new Set([...units.map(u => u.unit_id), ...merges.map(m => m.node_id), 'output'])
  const uid = nextId('extract', used)
  return {
    unit_id: uid,
    label: label || `抽取节点 ${units.length + 1}`,
    source_ids: [],
    source_table_overrides: {},
    document_ids: null,
    goal_markdown: '',
    probe_filter_hint: '',
    max_calls: null,
    max_run_seconds: null,
    enable_co_projection: null,
    position: { x: Math.max(16, Math.round(pos.x)), y: Math.max(16, Math.round(pos.y)) },
  }
}

function makeMergeNode(
  units: WorkflowUnitConfig[],
  merges: WorkflowMergeNodeConfig[],
  pos: WorkflowPosition,
  label?: string,
): WorkflowMergeNodeConfig {
  const used = new Set([...units.map(u => u.unit_id), ...merges.map(m => m.node_id), 'output'])
  const mid = nextId('merge', used)
  return {
    node_id: mid,
    label: label || `合并节点 ${merges.length + 1}`,
    merge_mode: 'overlap_review',
    source_ids: [],
    source_table_overrides: {},
    document_ids: null,
    goal_markdown: '',
    probe_filter_hint: '',
    max_calls: null,
    max_run_seconds: null,
    enable_co_projection: null,
    position: { x: Math.max(16, Math.round(pos.x)), y: Math.max(16, Math.round(pos.y)) },
  }
}

function addExtractNode(pos?: WorkflowPosition) {
  const next = normalizeWorkflow(current.value)
  next.enabled = true
  const position = pos || { x: 60, y: 40 + next.units.length * 135 }
  const node = makeExtractNode(next.units, next.merge_nodes || [], position)
  next.units.push(node)
  // If user adds extract node and there are no merge nodes yet, wire to output only if edges already exist or it's first node.
  if (!(next.merge_nodes || []).length) {
    next.edges = [...(next.edges || []), { edge_id: `e_${node.unit_id}_out`, source: node.unit_id, target: 'output' }]
  } else if (next.merge_nodes && next.merge_nodes.length === 1) {
    const targetMerge = next.merge_nodes[0]!.node_id
    next.edges = [...(next.edges || []), { edge_id: `e_${node.unit_id}_${targetMerge}`, source: node.unit_id, target: targetMerge }]
  }
  selectedId.value = node.unit_id
  commit(next)
}

function addMergeNode(pos?: WorkflowPosition) {
  const next = normalizeWorkflow(current.value)
  next.enabled = true
  const merges = next.merge_nodes || []
  const position = pos || { x: 360, y: 120 + merges.length * 145 }
  const mnode = makeMergeNode(next.units, merges, position)
  next.merge_nodes = [...merges, mnode]
  // If this is the first merge node and existing extract nodes were wired straight to output, auto-route them through this merge node!
  if (merges.length === 0) {
    const existingEdges = next.edges || []
    const rerouted: WorkflowEdgeConfig[] = []
    for (const edge of existingEdges) {
      if (edge.target === 'output' && next.units.some(u => u.unit_id === edge.source)) {
        rerouted.push({ edge_id: `e_${edge.source}_${mnode.node_id}`, source: edge.source, target: mnode.node_id })
      } else {
        rerouted.push(edge)
      }
    }
    for (const u of next.units) {
      if (!rerouted.some(e => e.source === u.unit_id)) {
        rerouted.push({ edge_id: `e_${u.unit_id}_${mnode.node_id}`, source: u.unit_id, target: mnode.node_id })
      }
    }
    rerouted.push({ edge_id: `e_${mnode.node_id}_out`, source: mnode.node_id, target: 'output' })
    next.edges = rerouted
  } else {
    next.edges = [...(next.edges || []), { edge_id: `e_${mnode.node_id}_out`, source: mnode.node_id, target: 'output' }]
  }
  selectedId.value = mnode.node_id
  commit(next)
}

function removeNode(nodeId: string) {
  if (nodeId === 'output') return
  const next = normalizeWorkflow(current.value)
  next.units = next.units.filter(u => u.unit_id !== nodeId)
  next.merge_nodes = (next.merge_nodes || []).filter(m => m.node_id !== nodeId)
  next.edges = (next.edges || []).filter(e => e.source !== nodeId && e.target !== nodeId)
  if (selectedId.value === nodeId) {
    selectedId.value = next.units[0]?.unit_id || next.merge_nodes[0]?.node_id || 'output'
  }
  commit(next)
}

function buildPresetSplit(withMerge: boolean) {
  const next = normalizeWorkflow(current.value)
  next.enabled = true
  const units: WorkflowUnitConfig[] = []
  let idx = 0
  for (const src of props.sources) {
    const tbls = tablesForSource(src)
    if (tbls.length === 0) {
      units.push({
        ...makeExtractNode(units, [], { x: 50, y: 40 + idx * 130 }, `抽取：${src.id}`),
        source_ids: [src.id],
      })
      idx++
    } else {
      for (const tbl of tbls) {
        units.push({
          ...makeExtractNode(units, [], { x: 50, y: 40 + idx * 130 }, `抽取：${tbl}`),
          source_ids: [src.id],
          source_table_overrides: { [src.id]: [tbl] },
        })
        idx++
      }
    }
  }
  if (!units.length) return
  next.units = units
  next.max_parallel_workers = Math.min(4, Math.max(1, units.length))
  if (withMerge) {
    const m = makeMergeNode(units, [], { x: 360, y: Math.max(80, Math.round((units.length * 130) / 2 - 40)) }, '合并各表结果')
    next.merge_nodes = [m]
    next.edges = [
      ...units.map(u => ({ edge_id: `e_${u.unit_id}_${m.node_id}`, source: u.unit_id, target: m.node_id })),
      { edge_id: `e_${m.node_id}_out`, source: m.node_id, target: 'output' },
    ]
    next.output_position = { x: 660, y: m.position!.y }
    selectedId.value = m.node_id
  } else {
    next.merge_nodes = []
    next.edges = units.map(u => ({ edge_id: `e_${u.unit_id}_out`, source: u.unit_id, target: 'output' }))
    next.output_position = { x: 640, y: Math.max(80, Math.round((units.length * 130) / 2 - 40)) }
    selectedId.value = units[0]!.unit_id
  }
  commit(next)
}

// --- Drag & Drop (Palette -> Canvas, Move Nodes, Connect Ports) ---
const canvasRef = ref<HTMLElement | null>(null)
const zoom = ref(1)
const fitMode = ref(true)
const viewportSize = ref({ width: 0, height: 0 })
const MIN_ZOOM = 0.1
const MAX_ZOOM = 2
let resizeObserver: ResizeObserver | undefined
const panState = ref<{ x: number; y: number; left: number; top: number } | null>(null)
const pendingWireFrom = ref<string | null>(null)
const wirePointer = ref<WorkflowPosition | null>(null)
let wireDrag: { sourceId: string; x: number; y: number; moved: boolean } | null = null
let suppressPortClick = false
const dragState = ref<{
  nodeId: string
  startX: number
  startY: number
  origX: number
  origY: number
  scrollLeft: number
  scrollTop: number
} | null>(null)

const NODE_W = 216
const NODE_H = 98
const OUT_NODE_W = 160
const OUT_NODE_H = 86

function nodePosition(id: string): WorkflowPosition {
  if (id === 'output') return current.value.output_position || { x: 640, y: 170 }
  const u = current.value.units.find(x => x.unit_id === id)
  if (u?.position) return u.position
  const m = (current.value.merge_nodes || []).find(x => x.node_id === id)
  if (m?.position) return m.position
  return { x: 60, y: 60 }
}

function outputPortCoords(id: string): WorkflowPosition {
  const p = nodePosition(id)
  return { x: p.x + NODE_W, y: p.y + NODE_H / 2 }
}

function inputPortCoords(id: string): WorkflowPosition {
  const p = nodePosition(id)
  const h = id === 'output' ? OUT_NODE_H : NODE_H
  return { x: p.x, y: p.y + h / 2 }
}

function bezierPath(a: WorkflowPosition, b: WorkflowPosition): string {
  const dx = Math.max(48, Math.abs(b.x - a.x) * 0.5)
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`
}

const renderedEdges = computed(() => {
  const validSources = new Set([
    ...current.value.units.map(u => u.unit_id),
    ...(current.value.merge_nodes || []).map(m => m.node_id),
  ])
  const validTargets = new Set([
    ...(current.value.merge_nodes || []).map(m => m.node_id),
    'output',
  ])
  return (current.value.edges || [])
    .filter(e => validSources.has(e.source) && validTargets.has(e.target))
    .map(e => {
      const a = outputPortCoords(e.source)
      const b = inputPortCoords(e.target)
      return {
        ...e,
        path: bezierPath(a, b),
        midX: (a.x + b.x) / 2,
        midY: (a.y + b.y) / 2,
      }
    })
})

const canvasHeight = computed(() => {
  const maxY = Math.max(
    420,
    ...current.value.units.map(u => (u.position?.y ?? 60) + NODE_H + 50),
    ...(current.value.merge_nodes || []).map(m => (m.position?.y ?? 120) + NODE_H + 50),
    (current.value.output_position?.y ?? 170) + OUT_NODE_H + 50,
  )
  return maxY
})

const canvasWidth = computed(() => {
  const maxX = Math.max(
    840,
    ...current.value.units.map(u => (u.position?.x ?? 60) + NODE_W + 60),
    ...(current.value.merge_nodes || []).map(m => (m.position?.x ?? 350) + NODE_W + 60),
    (current.value.output_position?.x ?? 640) + OUT_NODE_W + 40,
  )
  return maxX
})

// Nodes and wires share logical coordinates. The scroll area reserves the
// scaled extent, while only the scene itself is transformed.
const sceneExtent = computed(() => ({
  width: `${Math.max(viewportSize.value.width, canvasWidth.value * zoom.value)}px`,
  height: `${Math.max(viewportSize.value.height, canvasHeight.value * zoom.value)}px`,
}))

function canvasPoint(clientX: number, clientY: number): WorkflowPosition {
  const canvas = canvasRef.value!
  const rect = canvas.getBoundingClientRect()
  return {
    x: (clientX - rect.left - canvas.clientLeft + canvas.scrollLeft) / zoom.value,
    y: (clientY - rect.top - canvas.clientTop + canvas.scrollTop) / zoom.value,
  }
}

async function setZoom(value: number, clientX?: number, clientY?: number) {
  const canvas = canvasRef.value
  if (!canvas || dragState.value || wireDrag || panState.value) return
  const rect = canvas.getBoundingClientRect()
  const x = clientX ?? rect.left + canvas.clientWidth / 2
  const y = clientY ?? rect.top + canvas.clientHeight / 2
  const anchor = canvasPoint(x, y)
  fitMode.value = false
  zoom.value = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value))
  await nextTick()
  canvas.scrollLeft = anchor.x * zoom.value - (x - rect.left - canvas.clientLeft)
  canvas.scrollTop = anchor.y * zoom.value - (y - rect.top - canvas.clientTop)
  if (pendingWireFrom.value) wirePointer.value = canvasPoint(x, y)
}

function fitCanvas() {
  const canvas = canvasRef.value
  if (!canvas) return
  fitMode.value = true
  if (!canvas.clientWidth || !canvas.clientHeight) return
  zoom.value = Math.max(MIN_ZOOM, Math.min(1,
    canvas.clientWidth / canvasWidth.value, canvas.clientHeight / canvasHeight.value))
  canvas.scrollLeft = 0
  canvas.scrollTop = 0
}

function onCanvasWheel(event: WheelEvent) {
  if (!event.ctrlKey && !event.metaKey) return
  event.preventDefault()
  const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1)
  void setZoom(zoom.value * Math.exp(-delta * 0.002), event.clientX, event.clientY)
}

function startCanvasPan(event: MouseEvent) {
  if (event.button !== 0 || !canvasRef.value || pendingWireFrom.value) return
  if ((event.target as Element).closest('.wf-node, .wf-edge-delete')) return
  panState.value = { x: event.clientX, y: event.clientY,
    left: canvasRef.value.scrollLeft, top: canvasRef.value.scrollTop }
  window.addEventListener('mousemove', onWindowMouseMove)
  window.addEventListener('mouseup', onWindowMouseUp)
  event.preventDefault()
}

onMounted(() => {
  const resize = () => {
    const canvas = canvasRef.value
    if (!canvas) return
    viewportSize.value = { width: canvas.clientWidth, height: canvas.clientHeight }
    if (fitMode.value) fitCanvas()
  }
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(resize)
    if (canvasRef.value) resizeObserver.observe(canvasRef.value)
  }
  resize()
})
watch([canvasWidth, canvasHeight], () => { if (fitMode.value) fitCanvas() })
onUnmounted(() => resizeObserver?.disconnect())

function onPaletteDragStart(kind: 'extract' | 'merge', event: DragEvent) {
  if (props.locked || !event.dataTransfer) return
  event.dataTransfer.setData('application/x-lightgraph-node', kind)
  event.dataTransfer.effectAllowed = 'copy'
}

function onCanvasDrop(event: DragEvent) {
  if (props.locked || !canvasRef.value) return
  const kind = event.dataTransfer?.getData('application/x-lightgraph-node')
  if (kind !== 'extract' && kind !== 'merge') return
  const point = canvasPoint(event.clientX, event.clientY)
  const pos = {
    x: Math.max(16, point.x - NODE_W / 2),
    y: Math.max(16, point.y - NODE_H / 2),
  }
  if (kind === 'extract') addExtractNode(pos)
  else addMergeNode(pos)
}

function startNodeDrag(nodeId: string, event: MouseEvent) {
  if (props.locked || event.button !== 0) return
  const target = event.target as HTMLElement
  if (target.closest('button') || target.closest('.wf-port')) return
  fitMode.value = false
  selectedId.value = nodeId
  const pos = nodePosition(nodeId)
  dragState.value = {
    nodeId,
    startX: event.clientX,
    startY: event.clientY,
    origX: pos.x,
    origY: pos.y,
    scrollLeft: canvasRef.value?.scrollLeft || 0,
    scrollTop: canvasRef.value?.scrollTop || 0,
  }
  window.addEventListener('mousemove', onWindowMouseMove)
  window.addEventListener('mouseup', onWindowMouseUp)
}

function onWindowMouseMove(event: MouseEvent) {
  if (panState.value && canvasRef.value) {
    canvasRef.value.scrollLeft = panState.value.left - (event.clientX - panState.value.x)
    canvasRef.value.scrollTop = panState.value.top - (event.clientY - panState.value.y)
    return
  }
  if (props.locked) return
  if (wireDrag && Math.hypot(event.clientX - wireDrag.x, event.clientY - wireDrag.y) > 3) {
    wireDrag.moved = true
    pendingWireFrom.value = wireDrag.sourceId
  }
  if (dragState.value) {
    const dx = (event.clientX - dragState.value.startX
      + (canvasRef.value?.scrollLeft || 0) - dragState.value.scrollLeft) / zoom.value
    const dy = (event.clientY - dragState.value.startY
      + (canvasRef.value?.scrollTop || 0) - dragState.value.scrollTop) / zoom.value
    const nx = Math.max(12, Math.round(dragState.value.origX + dx))
    const ny = Math.max(12, Math.round(dragState.value.origY + dy))
    const id = dragState.value.nodeId
    if (id === 'output') {
      current.value.output_position = { x: nx, y: ny }
    } else {
      const u = current.value.units.find(item => item.unit_id === id)
      if (u) u.position = { x: nx, y: ny }
      else {
        const m = (current.value.merge_nodes || []).find(item => item.node_id === id)
        if (m) m.position = { x: nx, y: ny }
      }
    }
  } else if (pendingWireFrom.value && canvasRef.value) {
    wirePointer.value = canvasPoint(event.clientX, event.clientY)
  }
}

function stopPointerListeners() {
  window.removeEventListener('mousemove', onWindowMouseMove)
  window.removeEventListener('mouseup', onWindowMouseUp)
}

function cancelWire() {
  pendingWireFrom.value = null
  wirePointer.value = null
  wireDrag = null
  if (!dragState.value) stopPointerListeners()
}

function onWindowMouseUp(event: MouseEvent) {
  panState.value = null
  if (dragState.value) {
    const { nodeId, origX, origY } = dragState.value
    dragState.value = null
    const pos = nodePosition(nodeId)
    if (pos.x !== origX || pos.y !== origY) commit(current.value)
  }
  if (wireDrag?.moved) {
    const target = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-wire-target]') : null
    suppressPortClick = true
    if (target?.dataset.wireTarget) completeWireTo(target.dataset.wireTarget)
    else cancelWire()
  }
  wireDrag = null
  if (!pendingWireFrom.value) stopPointerListeners()
}

onUnmounted(stopPointerListeners)
watch(() => props.locked, locked => { if (locked) { dragState.value = null; panState.value = null; cancelWire() } })

function startWireDrag(sourceId: string, event: MouseEvent) {
  if (props.locked || event.button !== 0) return
  suppressPortClick = false
  wireDrag = { sourceId, x: event.clientX, y: event.clientY, moved: false }
  window.addEventListener('mousemove', onWindowMouseMove)
  window.addEventListener('mouseup', onWindowMouseUp)
}

function startOrToggleWireFrom(sourceId: string) {
  if (props.locked) return
  if (suppressPortClick) { suppressPortClick = false; return }
  if (pendingWireFrom.value === sourceId) { cancelWire(); return }
  editorError.value = ''
  pendingWireFrom.value = sourceId
  wirePointer.value = null
  window.addEventListener('mousemove', onWindowMouseMove)
  window.addEventListener('mouseup', onWindowMouseUp)
}

function wouldCreateCycle(edges: WorkflowEdgeConfig[], source: string, target: string): boolean {
  if (source === target) return true
  if (target === 'output') return false
  const adj = new Map<string, string[]>()
  for (const e of edges) {
    adj.set(e.source, [...(adj.get(e.source) || []), e.target])
  }
  const stack = [target]
  const seen = new Set<string>()
  while (stack.length) {
    const cur = stack.pop()!
    if (cur === source) return true
    if (seen.has(cur)) continue
    seen.add(cur)
    for (const nxt of adj.get(cur) || []) stack.push(nxt)
  }
  return false
}

function completeWireTo(targetId: string) {
  if (props.locked || !pendingWireFrom.value) return
  const src = pendingWireFrom.value
  cancelWire()
  if (src === targetId) { editorError.value = '节点不能连接自身。'; return }
  const next = normalizeWorkflow(current.value)
  const edges = next.edges || []
  if (edges.some(e => e.source === src && e.target === targetId)) return
  if (wouldCreateCycle(edges, src, targetId)) { editorError.value = '这条连线会形成循环，请选择其他节点。'; return }
  editorError.value = ''
  next.edges = [...edges, { edge_id: `e_${src}_${targetId}_${Date.now().toString(36)}`, source: src, target: targetId }]
  commit(next)
}

function removeEdge(edgeId: string) {
  if (props.locked) return
  const next = normalizeWorkflow(current.value)
  next.edges = (next.edges || []).filter(e => e.edge_id !== edgeId)
  commit(next)
}

// --- Node Configuration Helpers ---
const selectedExtractIndex = computed(() =>
  current.value.units.findIndex(u => u.unit_id === selectedId.value),
)
const selectedMergeIndex = computed(() =>
  (current.value.merge_nodes || []).findIndex(m => m.node_id === selectedId.value),
)

function tablesForSource(src: SourceConfig): string[] {
  const allowed = Array.isArray(src.allowed_tables) ? src.allowed_tables.map(String) : []
  const metaTables = props.tables
    .filter(t => t.source_id === src.id)
    .map(t => `${t.schema}.${t.name}`)
  return [...new Set(allowed.length ? allowed : metaTables)]
}

type NodeInputs = Partial<Omit<WorkflowUnitConfig, 'unit_id' | 'label' | 'position'>>

function editNodeInputs(kind: 'extract' | 'merge', index: number, patch: NodeInputs) {
  const next = normalizeWorkflow(current.value)
  const node = kind === 'extract' ? next.units[index] : next.merge_nodes?.[index]
  if (!node) return
  Object.assign(node, patch)
  commit(next)
}

function updateExtractField<K extends keyof WorkflowUnitConfig>(index: number, key: K, val: WorkflowUnitConfig[K]) {
  const next = normalizeWorkflow(current.value)
  if (!next.units[index]) return
  next.units[index][key] = val
  commit(next)
}

function renameUnitId(index: number, newId: string) {
  const trimmed = newId.trim()
  if (!trimmed || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(trimmed) || trimmed === 'output') return
  const next = normalizeWorkflow(current.value)
  const oldId = next.units[index]?.unit_id
  if (!oldId || oldId === trimmed) return
  const used = new Set([...next.units.map(u => u.unit_id), ...(next.merge_nodes || []).map(m => m.node_id)])
  if (used.has(trimmed)) return
  next.units[index]!.unit_id = trimmed
  next.edges = (next.edges || []).map(e => ({
    ...e,
    source: e.source === oldId ? trimmed : e.source,
    target: e.target === oldId ? trimmed : e.target,
  }))
  if (selectedId.value === oldId) selectedId.value = trimmed
  commit(next)
}

function updateMergeNodeField<K extends keyof WorkflowMergeNodeConfig>(
  index: number,
  key: K,
  val: WorkflowMergeNodeConfig[K],
) {
  const next = normalizeWorkflow(current.value)
  if (!next.merge_nodes?.[index]) return
  next.merge_nodes[index][key] = val
  commit(next)
}

function updateWorkers(raw: string) {
  const parsed = Number(raw)
  const next = normalizeWorkflow(current.value)
  next.max_parallel_workers = Number.isFinite(parsed) ? Math.min(24, Math.max(1, Math.round(parsed))) : 2
  commit(next)
}

// --- Inherited Base Graphs ---
const selectedInheritProject = ref('')
const customInheritId = ref('')
const customInheritScopes = ref('')
const otherProjects = computed(() => props.projects.filter(p => p.project_id !== props.projectId))

function addInheritedGraph() {
  const pid = selectedInheritProject.value.trim()
  if (!pid) return
  const baseId = (customInheritId.value.trim() || pid || 'base_graph').replace(/[^A-Za-z0-9_-]/g, '_')
  const existingIds = new Set(props.inheritedGraphs.map(g => g.inherit_id))
  let uniqueId = baseId || 'base_graph'
  let n = 2
  while (existingIds.has(uniqueId)) uniqueId = `${baseId}_${n++}`
  const scopesList = customInheritScopes.value.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean)
  emit('update:inheritedGraphs', [
    ...props.inheritedGraphs,
    {
      inherit_id: uniqueId,
      source_project_id: pid,
      snapshot_path: null,
      allowed_scopes: scopesList.length ? scopesList : [...props.scopes],
      expected_graph_digest: null,
    },
  ])
  selectedInheritProject.value = ''
  customInheritId.value = ''
}

function removeInheritedGraph(index: number) {
  emit('update:inheritedGraphs', props.inheritedGraphs.filter((_, i) => i !== index))
}

function nodeTableChip(node: { source_ids: string[]; source_table_overrides: Record<string, string[]> }): string {
  if (!node.source_ids.length) return '全部项目来源'
  const count = node.source_ids.reduce((acc, sid) => {
    const source = props.sources.find(item => item.id === sid)
    const tbls = node.source_table_overrides?.[sid] || (source ? tablesForSource(source) : [])
    return acc + tbls.length
  }, 0)
  return count > 0 ? `${node.source_ids.length} 个来源 · ${count} 张表` : `${node.source_ids.length} 个来源全表`
}

function nodeDocChip(node: { document_ids: string[] | null }): string {
  if (node.document_ids === null) return '全部知识文档'
  return `${node.document_ids.length} 篇知识文档`
}

// Match the run planner: explicit output wires select their ancestors. Otherwise all sinks are outputs.
const outputRoute = computed(() => {
  const wf = current.value
  const edges = wf.edges || []
  const merges = wf.merge_nodes || []
  const nodes = [...wf.units.map(unit => unit.unit_id), ...merges.map(node => node.node_id)]
  const inputs = new Map<string, string[]>()
  const hasOutgoing = new Set<string>()
  for (const edge of edges) {
    inputs.set(edge.target, [...(inputs.get(edge.target) || []), edge.source])
    hasOutgoing.add(edge.source)
  }
  const pending = [...(inputs.get('output') || nodes.filter(id => !hasOutgoing.has(id)))]
  const active = new Set<string>()
  while (pending.length) {
    const node = pending.pop()!
    if (active.has(node)) continue
    active.add(node)
    pending.push(...(inputs.get(node) || []))
  }
  const disconnectedMerge = merges.find(node => active.has(node.node_id) && !inputs.has(node.node_id))
  const problem = !wf.units.length ? '请添加至少一个抽取节点。' : disconnectedMerge ? `请给合并节点「${disconnectedMerge.label || disconnectedMerge.node_id}」连接上游输入。` : ''
  return { active, skipped: nodes.filter(id => !active.has(id)), problem }
})
const topologySummary = computed(() => {
  const wf = current.value
  if (!wf.enabled) return '当前按常规模式运行。启用工作流后按画布编排运行。'
  const { active, skipped, problem } = outputRoute.value
  if (problem) return problem
  const units = wf.units.filter(node => active.has(node.unit_id)).length
  const merges = (wf.merge_nodes || []).filter(node => active.has(node.node_id)).length
  const summary = merges ? `本次运行包含 ${units} 个抽取节点、${merges} 个合并节点。`
    : wf.legacy_merge && !wf.edges?.length && units > 1 ? '当前配置沿用旧版全局归并。'
    : units === 1 ? '本次运行由 1 个抽取节点直接输出。'
    : `未放置合并节点的输出路径包含 ${units} 个抽取节点，各分支独立输出。`
  return summary + (skipped.length ? ` 未连接到输出的节点将跳过：${skipped.join('、')}。` : '')
})
</script>

<template>
  <div class="workflow-page">
    <!-- 顶部控制条 -->
    <section class="panel wf-toolbar-panel" aria-label="工作流设置">
      <div class="wf-toolbar">
        <label class="checkbox-line">
          <input
            type="checkbox"
            :checked="current.enabled"
            :disabled="locked"
            @change="setEnabled(($event.target as HTMLInputElement).checked)"
          />
          <span><strong>启用并行工作流</strong></span>
        </label>
        <details class="wf-presets">
          <summary>按表编排</summary>
          <div class="actions">
            <button class="btn small" :disabled="locked || !sources.length" @click="emit('refreshTables')">读取数据表</button>
            <button class="btn small" :disabled="locked || !sources.length" @click="buildPresetSplit(false)">按表拆分直出</button>
            <button class="btn small" :disabled="locked || !sources.length" @click="buildPresetSplit(true)">按表拆分并合并</button>
          </div>
        </details>
        <div class="wf-toolbar-right">
          <label class="wf-inline-field">
            <span>最大并行数</span>
            <input
              id="wf-workers"
              type="number"
              min="1"
              max="24"
              step="1"
              :value="current.max_parallel_workers"
              :disabled="locked"
              @input="updateWorkers(($event.target as HTMLInputElement).value)"
            />
          </label>
          <label class="checkbox-line"><input type="checkbox" :checked="current.enable_co_projection" :disabled="locked" @change="commit({ ...current, enable_co_projection: ($event.target as HTMLInputElement).checked })" /><span>全局联合投影</span></label>
          <button class="btn" :disabled="locked" @click="emit('save')">保存工作流配置</button>
          <button class="btn primary" :disabled="locked || runDisabled || (current.enabled && !!outputRoute.problem)" :title="current.enabled ? outputRoute.problem : undefined" @click="emit('startRun')">保存并开始运行</button>
        </div>
      </div>
    </section>

    <p v-if="editorError" class="note amber" role="alert">{{ editorError }}</p>
    <div class="wf-studio">
      <!-- 中央可拖拉拽编排画布 -->
      <section class="panel wf-canvas-panel">
        <div class="panel-head">
          <div>
            <h2>画布</h2>
          </div>
          <div class="actions wf-palette-body">
            <span class="palette-item extract" :draggable="!locked" @dragstart="onPaletteDragStart('extract', $event)">
              <button class="btn small" type="button" :disabled="locked" @click="addExtractNode()">＋ 抽取节点</button>
            </span>
            <span class="palette-item merge" :draggable="!locked" @dragstart="onPaletteDragStart('merge', $event)">
              <button class="btn small" type="button" :disabled="locked" @click="addMergeNode()">＋ 合并节点</button>
            </span>
          </div>
        </div>

        <div class="wf-viewport-toolbar">
          <div class="actions" role="group" aria-label="画布缩放">
            <button class="btn small" type="button" aria-label="缩小画布" :disabled="zoom <= MIN_ZOOM" @click="setZoom(zoom / 1.2)">−</button>
            <button class="btn small wf-zoom-value" type="button" aria-label="恢复 100% 缩放" @click="setZoom(1)">{{ Math.round(zoom * 100) }}%</button>
            <button class="btn small" type="button" aria-label="放大画布" :disabled="zoom >= MAX_ZOOM" @click="setZoom(zoom * 1.2)">＋</button>
            <button class="btn small" type="button" :aria-pressed="fitMode" @click="fitCanvas">适应画布</button>
          </div>
          <button v-if="pendingWireFrom" type="button" class="text-button" @click="cancelWire">取消从 {{ pendingWireFrom }} 连线</button>
          <details v-else class="wf-help"><summary>操作说明</summary><p>拖动节点标题可移动。依次点击输出端口和输入端口可连线。拖动画布可平移，按住 Ctrl 或 ⌘ 滚动滚轮可缩放。</p></details>
        </div>
        <div
          ref="canvasRef"
          class="wf-canvas"
          :class="{ panning: !!panState }"
          @dragover.prevent
          @drop="onCanvasDrop"
          @wheel="onCanvasWheel"
          @mousedown="startCanvasPan"
        >
          <div class="wf-scene-extent" :style="sceneExtent">
            <div class="wf-scene" :style="{ width: `${canvasWidth}px`, height: `${canvasHeight}px`, transform: `scale(${zoom})` }">
              <svg class="wf-edges-svg" :width="canvasWidth" :height="canvasHeight">
                <defs>
                  <marker id="wf-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                    <path d="M0,0 L8,4 L0,8 Z" fill="#5b84d7" />
                  </marker>
                </defs>
                <g v-for="edge in renderedEdges" :key="edge.edge_id" class="wf-edge-group">
                  <path :d="edge.path" class="wf-edge-path" marker-end="url(#wf-arrow)" />
                  <g class="wf-edge-delete" :transform="`translate(${edge.midX}, ${edge.midY})`" @click.stop="removeEdge(edge.edge_id)">
                    <circle r="9" />
                    <text dy="3.5">×</text>
                  </g>
                </g>
                <path
                  v-if="pendingWireFrom && wirePointer"
                  :d="bezierPath(outputPortCoords(pendingWireFrom), wirePointer)"
                  class="wf-edge-preview"
                />
              </svg>

              <!-- 抽取节点卡片 -->
              <div
                v-for="unit in current.units"
                :key="unit.unit_id"
                class="wf-node wf-node-extract"
                :class="{ selected: selectedId === unit.unit_id }"
                :style="{ left: `${nodePosition(unit.unit_id).x}px`, top: `${nodePosition(unit.unit_id).y}px` }"
                @mousedown="startNodeDrag(unit.unit_id, $event)"
                @click.stop="selectedId = unit.unit_id"
              >
                <div class="wf-node-head">
                  <span class="badge blue">抽取</span>
                  <strong>{{ unit.label || unit.unit_id }}</strong>
                </div>
                <div class="wf-node-body">
                  <span class="wf-chip">{{ nodeTableChip(unit) }}</span>
                  <span class="wf-chip">{{ nodeDocChip(unit) }}</span>
                </div>
                <button
                  type="button"
                  class="wf-port wf-port-out"
                  :class="{ active: pendingWireFrom === unit.unit_id }"
                  :title="`从 ${unit.label || unit.unit_id} 连线输出`"
                  @mousedown.stop.prevent="startWireDrag(unit.unit_id, $event)"
                  @click.stop="startOrToggleWireFrom(unit.unit_id)"
                >●</button>
              </div>

              <!-- 合并节点卡片 -->
              <div
                v-for="mnode in (current.merge_nodes || [])"
                :key="mnode.node_id"
                class="wf-node wf-node-merge"
                :class="{ selected: selectedId === mnode.node_id }"
                :style="{ left: `${nodePosition(mnode.node_id).x}px`, top: `${nodePosition(mnode.node_id).y}px` }"
                @mousedown="startNodeDrag(mnode.node_id, $event)"
                @click.stop="selectedId = mnode.node_id"
              >
                <button
                  type="button"
                  class="wf-port wf-port-in"
                  :class="{ ready: !!pendingWireFrom && pendingWireFrom !== mnode.node_id }"
                  :title="`连接到 ${mnode.label || mnode.node_id}`"
                  :data-wire-target="mnode.node_id"
                  @click.stop="completeWireTo(mnode.node_id)"
                >○</button>
                <div class="wf-node-head">
                  <span class="badge amber">{{ mnode.merge_mode === 'merge_and_extract' ? '合并抽取' : '合并' }}</span>
                  <strong>{{ mnode.label || mnode.node_id }}</strong>
                </div>
                <div class="wf-node-body">
                  <span class="wf-chip">{{ mnode.merge_mode === 'merge_and_extract' ? '补充跨分支关系' : mnode.merge_mode === 'overlap_review' ? '审查分支间的重叠模式' : '按已有规则合并' }}</span>
                </div>
                <button
                  type="button"
                  class="wf-port wf-port-out"
                  :class="{ active: pendingWireFrom === mnode.node_id }"
                  :title="`从 ${mnode.label || mnode.node_id} 连线输出`"
                  @mousedown.stop.prevent="startWireDrag(mnode.node_id, $event)"
                  @click.stop="startOrToggleWireFrom(mnode.node_id)"
                >●</button>
              </div>

              <!-- 最终输出节点 -->
              <div
                class="wf-node wf-node-output"
                :class="{ selected: selectedId === 'output' }"
                :style="{ left: `${nodePosition('output').x}px`, top: `${nodePosition('output').y}px` }"
                @mousedown="startNodeDrag('output', $event)"
                @click.stop="selectedId = 'output'"
              >
                <button
                  type="button"
                  class="wf-port wf-port-in"
                  :class="{ ready: !!pendingWireFrom }"
                  title="连接到最终图谱输出"
                  data-wire-target="output"
                  @click.stop="completeWireTo('output')"
                >○</button>
                <div class="wf-node-head">
                  <span class="badge green">输出</span>
                  <strong>最终图谱</strong>
                </div>
                <div class="wf-node-body">
                  <span class="wf-chip">本次运行结果</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="panel-footer wf-canvas-footer">
          <span>{{ topologySummary }}</span>
          <span v-if="dirty" class="badge amber">有未保存修改</span>
        </div>
      </section>

      <!-- 右侧节点配置抽屉 -->
      <aside class="panel wf-inspector">
        <!-- 选中：抽取节点 -->
        <template v-if="selectedExtractIndex >= 0 && current.units[selectedExtractIndex]">
          <div class="panel-head">
            <div>
              <h2>抽取节点配置</h2>
              <small class="mono muted">{{ current.units[selectedExtractIndex]!.unit_id }}</small>
            </div>
            <button
              class="btn small danger-text"
              type="button"
              :disabled="locked"
              @click="removeNode(current.units[selectedExtractIndex]!.unit_id)"
            >删除节点</button>
          </div>
          <div class="panel-body">
            <div class="two-fields">
              <div class="field">
                <label :for="`inspector-ext-label-${selectedExtractIndex}`">节点名称</label>
                <input
                  :id="`inspector-ext-label-${selectedExtractIndex}`"
                  :value="current.units[selectedExtractIndex]!.label || ''"
                  :disabled="locked"
                  placeholder="例如：分支 A+B 表"
                  @input="updateExtractField(selectedExtractIndex, 'label', ($event.target as HTMLInputElement).value)"
                />
              </div>
              <div class="field">
                <label :for="`inspector-ext-id-${selectedExtractIndex}`">节点编号</label>
                <input
                  :id="`inspector-ext-id-${selectedExtractIndex}`"
                  :value="current.units[selectedExtractIndex]!.unit_id"
                  :disabled="locked"
                  @change="renameUnitId(selectedExtractIndex, ($event.target as HTMLInputElement).value)"
                />
              </div>
            </div>

            <WorkflowNodeInputs
              :key="selectedId" :node="current.units[selectedExtractIndex]!" :prefix="`inspector-ext-${selectedExtractIndex}`"
              :sources="sources" :tables-for-source="tablesForSource" :documents="documents" :locked="locked"
              @update="editNodeInputs('extract', selectedExtractIndex, $event)" />
          </div>
        </template>

        <!-- 选中：合并节点 -->
        <template v-else-if="selectedMergeIndex >= 0 && current.merge_nodes?.[selectedMergeIndex]">
          <div class="panel-head">
            <div>
              <h2>合并节点配置</h2>
              <small class="mono muted">{{ current.merge_nodes[selectedMergeIndex]!.node_id }}</small>
            </div>
            <button
              class="btn small danger-text"
              type="button"
              :disabled="locked"
              @click="removeNode(current.merge_nodes[selectedMergeIndex]!.node_id)"
            >删除节点</button>
          </div>
          <div class="panel-body">
            <div class="field">
              <label :for="`inspector-merge-label-${selectedMergeIndex}`">合并节点名称</label>
              <input
                :id="`inspector-merge-label-${selectedMergeIndex}`"
                :value="current.merge_nodes[selectedMergeIndex]!.label || ''"
                :disabled="locked"
                placeholder="合并各分支结果"
                @input="updateMergeNodeField(selectedMergeIndex, 'label', ($event.target as HTMLInputElement).value)"
              />
            </div>

            <div class="field">
              <label :for="`inspector-merge-mode-${selectedMergeIndex}`">合并执行方式</label>
              <select
                :id="`inspector-merge-mode-${selectedMergeIndex}`"
                :value="current.merge_nodes[selectedMergeIndex]!.merge_mode"
                :disabled="locked"
                @change="updateMergeNodeField(selectedMergeIndex, 'merge_mode', ($event.target as HTMLSelectElement).value as WorkflowMergeNodeConfig['merge_mode'])"
              >
                <option value="overlap_review">由模型审查重叠模式，再按证据合并</option>
                <option value="deterministic">按已有规则合并</option>
                <option value="merge_and_extract">合并后用模型补充跨分支关系</option>
              </select>
            </div>

            <template v-if="current.merge_nodes[selectedMergeIndex]!.merge_mode === 'merge_and_extract'">
            <WorkflowNodeInputs
              :key="selectedId" :node="current.merge_nodes[selectedMergeIndex]!" :prefix="`inspector-merge-${selectedMergeIndex}`"
              :sources="sources" :tables-for-source="tablesForSource" :documents="documents" :locked="locked"
              @update="editNodeInputs('merge', selectedMergeIndex, $event)" />
            </template>


          </div>
        </template>

        <!-- 选中：输出节点 / 全局底座继承 -->
        <template v-else>
          <div class="panel-head">
            <h2>输出与基准继承</h2>
            <span class="badge green">Output</span>
          </div>
          <div class="panel-body">
            <p class="field-hint">{{ topologySummary }}</p>
            <div class="unit-section">
              <div class="unit-section-head">
                <h3>跨项目继承基准图谱 ({{ inheritedGraphs.length }})</h3>
              </div>
              <p class="field-hint">可把其他已发布项目的图谱作为只读底座接入本工作流。</p>
              <div v-if="inheritedGraphs.length" class="inherited-list">
                <div v-for="(item, i) in inheritedGraphs" :key="item.inherit_id + i" class="inherited-item">
                  <div>
                    <strong>{{ item.inherit_id }}</strong>
                    <p class="field-hint">来源：{{ item.source_project_id || item.snapshot_path }}</p>
                  </div>
                  <button class="btn small danger-text" type="button" :disabled="locked" @click="removeInheritedGraph(i)">移除</button>
                </div>
              </div>
              <div class="field">
                <label for="inherit-project">选择已有项目</label>
                <select id="inherit-project" v-model="selectedInheritProject" :disabled="locked">
                  <option value="">不从项目选择</option>
                  <option v-for="p in otherProjects" :key="p.project_id" :value="p.project_id">
                    {{ p.name }} ({{ p.project_id }})
                  </option>
                </select>
              </div>
              <div class="two-fields">
                <div class="field">
                  <label for="inherit-id">别名 ID</label>
                  <input id="inherit-id" v-model="customInheritId" :disabled="locked" placeholder="自动" />
                </div>
                <div class="field">
                  <label for="inherit-scopes">作用域</label>
                  <input id="inherit-scopes" v-model="customInheritScopes" :disabled="locked" :placeholder="scopes.join('，')" />
                </div>
              </div>
              <button
                class="btn"
                type="button"
                :disabled="locked || !selectedInheritProject"
                @click="addInheritedGraph"
              >＋ 添加基准图谱</button>
            </div>
          </div>
        </template>
      </aside>
    </div>
  </div>
</template>
