<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { json, projectPath, request } from './api'
import type { BusinessContext, Graph, GraphNode } from './types'
type Candidate = { candidate_id: string; members: string[]; channels: string[]; evidence: string[]; status: string; has_active_decision: boolean; supports_truncated: boolean; facts_truncated?: boolean; nodes: GraphNode[]; facts: unknown[]; supports: unknown[] }
type Decision = { decision_id: string; candidate_id: string; verdict: 'same' | 'different'; revision: number; active: boolean; status: string; members: string[]; nodes: GraphNode[] }
type Review = { revision: number; backend?: string; version_id?: string; candidates: Candidate[]; decisions: Decision[]; total_candidates: number; next_offset: number | null; total_decisions: number; next_decision_offset: number | null }
const props = defineProps<{ projectId: string; configRevision?: string; graph: Graph; businessContext?: BusinessContext | null; disabled?: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const review = ref<Review | null>(null), selectedId = ref(''), busy = ref(false), error = ref(''), offset = ref(0), decisionOffset = ref(0)
const selected = computed(() => review.value?.candidates.find(c => c.candidate_id === selectedId.value))
const members = ref<string[]>([])
watch(selected, candidate => {
  members.value = candidate && (candidate.members.length === 2 || review.value?.backend !== 'publication') ? [...candidate.members] : []
})
const canDecide = computed(() => !busy.value && !props.disabled && selected.value?.status === 'candidate'
  && !selected.value.has_active_decision && members.value.length >= 2)
function toggleMember(id: string, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  members.value = checked ? [...members.value.filter(member => member !== id), id] : members.value.filter(member => member !== id)
}
const channel = (name: string) => ({ name: '名称相似', code: '编码相似', definition: '定义相似',
  same_source_observation: '同源记录', same_typed_identity: '同类型同编码', normalized_label: '名称相近',
  same_source_key_different_identity_domains: '同一来源键值，草稿声明的身份域不同',
  independent_drafts_unverified_identity: '不同草稿使用相同编码', source_key_requires_role_review: '同一来源键，对象角色待核对' }[name] || name)
const label = (node: GraphNode) => String(node.label || node.name || node.id)
let generation = 0
async function load(at = offset.value, decisionAt = decisionOffset.value) {
  const epoch = ++generation; busy.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ offset: String(at), decision_offset: String(decisionAt) })
    if (typeof props.graph.version_id === 'string' && props.graph.version_id) params.set('version', props.graph.version_id)
    if (props.businessContext != null) params.set('business_context', JSON.stringify(props.businessContext))
    const value = await request<Review>(`${projectPath(props.projectId)}/graph/identity-candidates?${params}`)
    if (epoch !== generation) return
    review.value = value; offset.value = at; decisionOffset.value = decisionAt
    if (!value.candidates.some(c => c.candidate_id === selectedId.value)) selectedId.value = ''
  } catch (e) { if (epoch === generation) { review.value = null; error.value = e instanceof Error ? e.message : '候选读取失败' } }
  finally { if (epoch === generation) busy.value = false }
}
async function decide(verdict: 'same' | 'different') {
  if (!selected.value || !review.value || !canDecide.value) return
  const candidate = selected.value
  const names = candidate.nodes.filter(node => members.value.includes(node.id)).map(label).join('、')
  const meaning = verdict === 'same' ? '是同一对象' : '两两不同'
  if (!window.confirm(`确认所选对象“${names}”${meaning}？本次裁决可以撤销。`)) return
  await mutate('/graph/identity-decisions', { revision: review.value.revision, candidate_id: candidate.candidate_id, verdict,
    ...(review.value.backend === 'publication' ? { members: [...members.value] } : {}) })
}
async function revoke(decision: Decision) {
  if (!review.value || busy.value || props.disabled || !window.confirm('撤销这次对象裁决？当前图谱会按剩余有效裁决重新显示，原始依据和历史记录保留。')) return
  await mutate(`/graph/identity-decisions/${encodeURIComponent(decision.decision_id)}/revoke`, { revision: review.value.revision })
}
async function mutate(endpoint: string, value: Record<string, unknown>) {
  busy.value = true; error.value = ''
  try {
    const headers: Record<string, string> = props.configRevision ? { 'If-Match': props.configRevision } : {}
    const version = review.value?.version_id
    const payload = version ? { ...value, version_id: version,
      ...(props.businessContext != null ? { business_context: props.businessContext } : {}) } : value
    await request(`${projectPath(props.projectId)}${endpoint}`, { method: 'POST', headers, body: json(payload) })
    if (version) { selectedId.value = ''; review.value = null }
    else await load()
    emit('changed')
  } catch (e) { error.value = e instanceof Error ? e.message : '裁决未保存' }
  finally { busy.value = false }
}
watch([() => props.projectId, () => props.graph, () => props.businessContext], () => {
  selectedId.value = ''; review.value = null; void load(0, 0)
}, { immediate: true })
</script>

<template>
  <section class="panel identity-review">
    <div class="panel-head"><h2>对象归并候选</h2><button class="btn small" :disabled="busy" @click="load()">刷新候选</button></div>
    <div class="panel-body">
      <p class="field-hint">相似名称、编码或定义用于提示检查。选择候选后，可核对完整对象与来源，再确认是否为同一对象。不同业务关系会分别保留。</p>
      <p v-if="disabled" class="field-hint">运行结束并保存配置后可进行裁决。</p>
      <p v-if="error" role="alert" class="note amber">{{ error }}</p>
      <p v-if="busy" role="status">正在读取或保存…</p>
      <p v-if="review && !review.total_candidates" class="muted">当前没有归并候选。</p>
      <template v-if="review?.total_candidates">
        <label for="identity-candidate">对象候选（共 {{ review.total_candidates }} 组）</label>
        <select id="identity-candidate" v-model="selectedId" :disabled="busy"><option value="">选择一组候选</option><option v-for="item in review.candidates" :key="item.candidate_id" :value="item.candidate_id">{{ item.nodes.map(label).join(' / ') }} · {{ item.channels.map(channel).join('、') }}{{ item.status === 'needs_review' ? ' · 依据已变化' : item.has_active_decision ? ' · 已裁决' : '' }}</option></select>
        <div class="actions"><button v-if="offset" class="btn small" :disabled="busy" @click="load(Math.max(0, offset - 50))">上一页</button><button v-if="review.next_offset !== null" class="btn small" :disabled="busy" @click="load(review.next_offset!)">下一页</button></div>
      </template>
      <div v-if="selected" class="candidate-detail">
        <fieldset v-if="review?.backend === 'publication' && selected.members.length > 2">
          <legend>选择本次裁决的对象（至少两个）</legend>
          <label v-for="node in selected.nodes" :key="node.id"><input type="checkbox" :checked="members.includes(node.id)" @change="toggleMember(node.id, $event)" /> {{ label(node) }} · {{ node.id }}</label>
        </fieldset>
        <div class="candidate-nodes"><article v-for="node in selected.nodes" :key="node.id"><h3>{{ label(node) }}</h3><p>{{ node.kind }} · {{ node.status === 'accepted' ? '已接受' : '候选 / 未决' }}</p><pre>{{ JSON.stringify(node, null, 2) }}</pre></article></div>
        <p v-if="selected.supports_truncated" class="note amber">来源支持超过 500 条，当前显示前 500 条。</p><p>候选依据：{{ selected.channels.map(channel).join('、') }}</p>
        <details><summary>查看字段事实与来源支持（{{ selected.facts_truncated ? '前 ' : '' }}{{ selected.evidence.length }} 条事实）</summary><pre>{{ JSON.stringify({ facts: selected.facts, supports: selected.supports }, null, 2) }}</pre></details>
        <div class="actions"><button class="btn primary" :disabled="!canDecide" @click="decide('same')">确认为同一对象</button><button class="btn" :disabled="!canDecide" @click="decide('different')">确认为不同对象</button></div>
        <p v-if="selected.status === 'needs_review'" class="note amber">支持依据已变化，请重新运行抽取后检查。</p>
        </div>
      <h3 v-if="review?.total_decisions">裁决记录（{{ review.total_decisions }} 条）</h3><div v-for="decision in review?.decisions" :key="decision.decision_id" class="decision-row"><span>{{ decision.nodes.map(label).join(' / ') || decision.members.join(' / ') }} · 第 {{ decision.revision }} 次裁决：{{ decision.verdict === 'same' ? '同一对象' : '不同对象' }} · {{ decision.status === 'revoked' ? '已撤销' : decision.status === 'needs_review' ? '依据变化，需复核' : '生效' }}</span><button v-if="decision.active" class="btn small" :disabled="disabled || busy" @click="revoke(decision)">撤销裁决</button></div>
      <div class="actions"><button v-if="decisionOffset" class="btn small" :disabled="busy" @click="load(offset, Math.max(0, decisionOffset - 50))">上一页裁决</button><button v-if="review?.next_decision_offset != null" class="btn small" :disabled="busy" @click="load(offset, review.next_decision_offset)">下一页裁决</button></div>
    </div>
  </section>
</template>

<style scoped>
.identity-review{margin-top:18px}.identity-review select{width:100%;margin:10px 0}.candidate-nodes{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:16px}.candidate-nodes article{padding:12px;border:1px solid #e2e8f0;border-radius:8px;min-width:0}.candidate-detail pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto;font-size:12px}.candidate-detail .actions,.decision-row{margin-top:14px}.decision-row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
</style>
