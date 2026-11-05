<script setup lang="ts">
import { computed, onUnmounted, ref, useId, watch } from 'vue'
import { ApiError, projectPath, request } from './api'
import TechnicalDetails from './TechnicalDetails.vue'

type Plan = { draft_id: string; revision: number; validation_id: string; checked_at: string; passed: boolean; historical: boolean; is_current_revision: boolean; entity_type_count: number; relation_type_count: number; mapping_count: number }
type Entity = { type: string; description?: string; key_names: string[]; mappings: Array<Record<string, unknown>> }
type Relation = { type: string; description?: string; source_type: string; target_type: string; mappings: Array<Record<string, unknown>> }
type Selected = Plan & { draft: { purpose?: string; entities: Entity[]; relations: Relation[]; unresolved?: string[] }; bindings: Array<{ source_id: string; schema: string; table: string; columns: string[] }>; diagnostics: Array<{ code: string; path?: Array<string | number> }> }
type Preview = { target: 'graph_structure'; plans: Plan[]; selected: Selected | null; data_validation: 'not_run'; instance_enumeration: 'not_run' }
type Point = { x: number; y: number }
type Box = Point & { width: number; height: number }
function curvePoint(points: Point[], t: number): Point {
  let row = points
  while (row.length > 1) row = row.slice(1).map((point, index) => ({ x: row[index]!.x * (1 - t) + point.x * t, y: row[index]!.y * (1 - t) + point.y * t }))
  return row[0]!
}
function overlaps(left: Box, right: Box) {
  const gap = 6
  return left.x < right.x + right.width + gap && left.x + left.width + gap > right.x
    && left.y < right.y + right.height + gap && left.y + left.height + gap > right.y
}
const props = defineProps<{ projectId: string; configRevision?: string }>()
const preview = ref<Preview | null>(null), loading = ref(false), error = ref(''), zoom = ref(1)
const selected = computed(() => preview.value?.selected)
const arrowId = `structure-arrow-${useId()}`
let generation = 0
let controller: AbortController | undefined

const scene = computed(() => {
  const entities = selected.value?.draft.entities || [], count = entities.length
  const radius = Math.max(230, count * 34), width = 2 * radius + 250, height = radius * 1.5 + 150
  const nodes = entities.map((entity, index) => {
    const angle = index * 2 * Math.PI / Math.max(1, count) - Math.PI / 2
    return { ...entity, x: count === 1 ? width / 2 : width / 2 + radius * Math.cos(angle), y: count === 1 ? height / 2 : height / 2 + radius * .75 * Math.sin(angle) }
  })
  const byType = new Map(nodes.map(node => [node.type, node]))
  const curves = (selected.value?.draft.relations || []).flatMap((relation, index) => {
    const source = byType.get(relation.source_type), target = byType.get(relation.target_type)
    if (!source || !target) return []
    if (source === target) {
      // Keep the full Bezier control hull inside the viewBox, including loops
      // on the topmost or leftmost type. Labels have their own bounds checks.
      const top = Math.max(16, source.y - 110), left = Math.max(16, source.x - 150), right = Math.min(width - 16, source.x + 150)
      return [{ ...relation, reference: index + 1, path: `M ${source.x + 65} ${source.y - 34} C ${right} ${top}, ${left} ${top}, ${source.x - 65} ${source.y - 34}`, points: [{ x: source.x + 65, y: source.y - 34 }, { x: right, y: top }, { x: left, y: top }, { x: source.x - 65, y: source.y - 34 }] }]
    }
    const dx = target.x - source.x, dy = target.y - source.y, length = Math.hypot(dx, dy)
    const extent = Math.max(Math.abs(dx) / 92, Math.abs(dy) / 32)
    const sx = source.x + dx / extent, sy = source.y + dy / extent, tx = target.x - dx / extent, ty = target.y - dy / extent
    const bend = 24 + index % 3 * 18, x = (sx + tx) / 2 - dy / length * bend, y = (sy + ty) / 2 + dx / length * bend
    return [{ ...relation, reference: index + 1, path: `M ${sx} ${sy} Q ${x} ${y} ${tx} ${ty}`, points: [{ x: sx, y: sy }, { x, y }, { x: tx, y: ty }] }]
  })
  const occupied: Box[] = nodes.map(node => ({ x: node.x - 92, y: node.y - 32, width: 184, height: 64 }))
  let canvasHeight = height
  const edges = curves.map(edge => {
    // Test multiple locations along each curve. Conservative glyph widths keep
    // this deterministic before browser fonts load, including CJK labels.
    const labelBox = (text: string, x: number, y: number): Box => {
      const textWidth = [...text].reduce((sum, character) => sum + (character.codePointAt(0)! < 128 ? 9 : 13), 12)
      return { x: x - textWidth / 2, y: y - 16, width: textWidth, height: 22 }
    }
    const fits = (box: Box) => box.x >= 8 && box.y >= 8 && box.x + box.width <= width - 8
      && box.y + box.height <= canvasHeight - 8 && !occupied.some(other => overlaps(box, other))
    const alongCurve = (text: string) => {
      for (const offset of [-7, -29, 15, -51, 37]) for (const t of [.5, .32, .68, .2, .8, .12, .88, .42, .58]) {
        const anchor = curvePoint(edge.points, t), x = anchor.x, y = anchor.y + offset, box = labelBox(text, x, y)
        if (fits(box)) return { x, y, box, anchor }
      }
    }
    let text = edge.type, position = alongCurve(text)
    const numbered = !position
    if (!position) { text = `#${edge.reference}`; position = alongCurve(text) }
    if (!position) {
      // Dense graphs use compact references to the relation table. A leader
      // identifies the curve even when its marker needs an unused canvas area.
      const anchor = curvePoint(edge.points, .5)
      for (let y = 24; y < canvasHeight - 16 && !position; y += 28) for (let x = 24; x < width - 24; x += 32) {
        const box = labelBox(text, x, y)
        if (fits(box)) { position = { x, y, box, anchor }; break }
      }
      if (!position) {
        canvasHeight += 32
        const x = width / 2, y = canvasHeight - 16
        position = { x, y, box: labelBox(text, x, y), anchor }
      }
    }
    occupied.push(position.box)
    return { ...edge, ...position, text, numbered }
  })
  return { width, height: canvasHeight, nodes, edges, numbered: edges.some(edge => edge.numbered) }
})

async function load(plan?: Plan) {
  const epoch = ++generation
  controller?.abort(); controller = new AbortController()
  loading.value = true; error.value = ''; preview.value = null
  const query = plan ? `?${new URLSearchParams({ draft_id: plan.draft_id, revision: String(plan.revision) })}` : ''
  try {
    const value = await request<Preview>(`${projectPath(props.projectId)}/graph/structure${query}`, { signal: controller.signal })
    if (epoch !== generation) return
    preview.value = value; zoom.value = Math.min(1, 960 / scene.value.width)
  } catch (exception) {
    if (epoch !== generation) return
    error.value = exception instanceof ApiError && exception.code === 'graph_access_context_changed'
      ? '访问范围已变化，不能读取此前授权下的结构方案。请重新检查当前范围内的结构。'
      : exception instanceof Error ? exception.message : '结构方案读取失败，请重试。'
  } finally { if (epoch === generation) loading.value = false }
}
function choose(event: Event) {
  const plan = preview.value?.plans.find(item => item.validation_id === (event.target as HTMLSelectElement).value)
  if (plan) void load(plan)
}
watch([() => props.projectId, () => props.configRevision], () => { void load() }, { immediate: true })
onUnmounted(() => { generation++; controller?.abort() })
</script>

<template>
  <section class="panel structure-preview" aria-label="结构方案预览">
    <div class="panel-head"><h2>结构方案预览</h2><button class="btn small" :disabled="loading" @click="load()">刷新结构方案</button></div>
    <div class="panel-body">
      <p class="note">这里展示实体类型、关系定义和字段映射，不是具体实例节点。预览不会查询事实数据，也不会执行实例校验或生成实例；实例运行结果仍在业务图谱和运行记录中查看。</p>
      <p class="field-hint">方案由 Agent 提出。结构检查核对定义与授权字段，不等于业务语义验收；打开历史预览也不会重新检查当前数据。</p>
      <p v-if="loading" role="status">正在读取已检查的结构方案…</p>
      <p v-else-if="error" class="note amber" role="alert">{{ error }}</p>
      <p v-else-if="!selected" class="empty-state">尚无结构检查记录。实例图为空，不代表结构方案不存在；完成一次结构检查后即可在这里查看。</p>
      <template v-else>
        <label class="version-label">方案与修订<select aria-label="选择结构方案修订" :value="selected.validation_id" @change="choose"><option v-for="plan in preview?.plans" :key="plan.validation_id" :value="plan.validation_id">{{ plan.draft_id }} · 修订 {{ plan.revision }} · {{ plan.historical ? '历史版本' : '当前版本' }} · {{ plan.passed ? '检查通过' : '检查未通过' }}</option></select></label>
        <p class="structure-summary">{{ selected.entity_type_count }} 类实体 · {{ selected.relation_type_count }} 类关系 · {{ selected.mapping_count }} 个字段映射</p>
        <p class="note" :class="{ amber: !selected.passed }">{{ selected.passed ? '结构检查已通过' : '结构检查未通过' }} · {{ selected.historical ? '历史修订，仅供查看，不会替换当前方案。' : '当前修订。' }} 检查时间：{{ selected.checked_at }}</p>
        <p v-if="selected.draft.purpose">{{ selected.draft.purpose }}</p>
        <ul v-if="selected.diagnostics.length" aria-label="结构检查诊断"><li v-for="(diagnostic, index) in selected.diagnostics" :key="index">{{ diagnostic.code }}<span v-if="diagnostic.path"> · {{ diagnostic.path.join(' / ') }}</span></li></ul>
        <div class="structure-toolbar"><span>类型与关系定义</span><button class="btn small" aria-label="缩小结构图" @click="zoom = Math.max(.05, zoom / 1.25)">−</button><span>{{ Math.round(zoom * 100) }}%</span><button class="btn small" aria-label="放大结构图" @click="zoom = Math.min(3, zoom * 1.25)">+</button><button class="btn small" @click="zoom = Math.min(1, 960 / scene.width)">适配结构图</button></div>
        <div class="structure-diagram"><svg role="img" aria-label="图谱结构：实体类型与关系定义" :width="scene.width * zoom" :height="scene.height * zoom" :viewBox="`0 0 ${scene.width} ${scene.height}`">
          <defs><marker :id="arrowId" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 Z" fill="#6780aa"/></marker></defs>
          <g v-for="edge in scene.edges" :key="edge.type" :data-relation-type="edge.type"><title>{{ edge.source_type }} → {{ edge.type }} → {{ edge.target_type }}{{ edge.description ? `：${edge.description}` : '' }}</title><path :d="edge.path" fill="none" stroke="#6780aa" stroke-width="1.5" :marker-end="`url(#${arrowId})`"/></g>
          <g v-for="edge in scene.edges" :key="`label-${edge.type}`" :data-relation-label="edge.type" :data-numbered="edge.numbered"><title>{{ edge.type }}</title><line v-if="Math.hypot(edge.x - edge.anchor.x, edge.y - 6 - edge.anchor.y) > 14" :x1="edge.anchor.x" :y1="edge.anchor.y" :x2="edge.x" :y2="edge.y - 6" stroke="#8ba0bf" stroke-dasharray="2 3"/><rect class="relation-label-box" :x="edge.box.x" :y="edge.box.y" :width="edge.box.width" :height="edge.box.height" rx="4" fill="#fbfdff" fill-opacity=".96"/><text :x="edge.x" :y="edge.y" class="relation-label">{{ edge.text }}</text></g>
          <g v-for="node in scene.nodes" :key="node.type" :data-entity-type="node.type"><title>{{ node.type }}{{ node.description ? `：${node.description}` : '' }}</title><rect :x="node.x - 92" :y="node.y - 32" width="184" height="64" rx="10" fill="#eef4ff" stroke="#8ba9e1"/><text :x="node.x" :y="node.y - 3" class="entity-label">{{ node.type.length > 12 ? `${node.type.slice(0, 11)}…` : node.type }}</text><text :x="node.x" :y="node.y + 18" class="mapping-label">{{ node.mappings.length }} 个字段映射</text></g>
        </svg></div>
        <div class="table-scroll"><table aria-label="实体类型定义"><thead><tr><th>实体类型</th><th>含义</th><th>身份字段</th><th>字段映射</th></tr></thead><tbody><tr v-for="entity in selected.draft.entities" :key="entity.type"><td>{{ entity.type }}</td><td>{{ entity.description || '—' }}</td><td>{{ entity.key_names.join('、') }}</td><td>{{ entity.mappings.length }}</td></tr></tbody></table></div>
        <p v-if="scene.numbered" class="field-hint">部分关系使用短编号显示，完整名称对应下方关系定义表。</p>
        <div v-if="selected.draft.relations.length" class="table-scroll"><table aria-label="关系定义"><thead><tr><th v-if="scene.numbered">图中编号</th><th>关系</th><th>来源类型</th><th>目标类型</th><th>含义</th></tr></thead><tbody><tr v-for="(relation, index) in selected.draft.relations" :key="relation.type"><td v-if="scene.numbered">#{{ index + 1 }}</td><td>{{ relation.type }}</td><td>{{ relation.source_type }}</td><td>{{ relation.target_type }}</td><td>{{ relation.description || '—' }}</td></tr></tbody></table></div>
        <details class="binding-list"><summary>查看物理表与字段绑定（{{ selected.bindings.length }}）</summary><div class="table-scroll"><table><thead><tr><th>来源</th><th>表</th><th>字段</th></tr></thead><tbody><tr v-for="(binding, index) in selected.bindings" :key="index"><td>{{ binding.source_id }}</td><td>{{ binding.schema }}.{{ binding.table }}</td><td>{{ binding.columns.join('、') }}</td></tr></tbody></table></div></details>
        <TechnicalDetails :value="selected.draft" title="完整结构方案与映射参数"/>
      </template>
    </div>
  </section>
</template>

<style scoped>
.structure-preview{margin:16px 0 24px}.structure-preview .panel-head,.structure-toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.structure-preview .panel-head{justify-content:space-between}.version-label{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:18px 0}.version-label select{min-width:280px;max-width:100%}.structure-summary{font-size:18px;font-weight:650}.structure-toolbar{margin:20px 0 8px}.structure-toolbar>span:first-child{margin-right:auto}.structure-diagram{max-height:640px;overflow:auto;border:1px solid #dbe4f0;border-radius:10px;background:#fbfdff;padding:8px}.structure-diagram svg{display:block;margin:auto;max-width:none}.structure-diagram text{text-anchor:middle;font-family:inherit}.entity-label{font-size:13px;font-weight:600;fill:#263c60}.mapping-label{font-size:11px;fill:#617594}.relation-label{font-size:12px;fill:#3d567d;paint-order:stroke;stroke:#fbfdff;stroke-width:4px;stroke-linejoin:round}.structure-preview .table-scroll,.binding-list{margin-top:18px}.structure-preview td{white-space:normal;overflow-wrap:anywhere}.structure-preview th{white-space:nowrap}
</style>
