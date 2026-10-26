<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { json, projectPath, request } from './api'
import type { Graph, GraphNode } from './types'
type Candidate = { candidate_id: string; members: string[]; channels: string[]; evidence: string[]; status: string; has_active_decision: boolean; supports_truncated: boolean; nodes: GraphNode[]; facts: unknown[]; supports: unknown[] }
type Decision = { decision_id: string; candidate_id: string; verdict: 'same' | 'different'; revision: number; active: boolean; status: string; members: string[]; nodes: GraphNode[] }
type Review = { revision: number; candidates: Candidate[]; decisions: Decision[]; total_candidates: number; next_offset: number | null; total_decisions: number; next_decision_offset: number | null }
const props = defineProps<{ projectId: string; configRevision?: string; graph: Graph; disabled?: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const review = ref<Review | null>(null), selectedId = ref(''), busy = ref(false), error = ref(''), offset = ref(0), decisionOffset = ref(0)
const selected = computed(() => review.value?.candidates.find(c => c.candidate_id === selectedId.value))
const channel = (name: string) => ({ name: '名称相似', code: '编码相似', definition: '定义相似' }[name] || name)
const label = (node: GraphNode) => String(node.label || node.name || node.id)
let generation = 0
async function load(at = offset.value, decisionAt = decisionOffset.value) {
  const epoch = ++generation; busy.value = true; error.value = ''
  try {
    const value = await request<Review>(`${projectPath(props.projectId)}/graph/identity-candidates?offset=${at}&decision_offset=${decisionAt}`)
    if (epoch !== generation) return
    review.value = value; offset.value = at; decisionOffset.value = decisionAt
    if (!value.candidates.some(c => c.candidate_id === selectedId.value)) selectedId.value = ''
  } catch (e) { if (epoch === generation) error.value = e instanceof Error ? e.message : '候选读取失败' }
  finally { if (epoch === generation) busy.value = false }
}
async function decide(verdict: 'same' | 'different') {
  if (!selected.value || !review.value || busy.value || props.disabled) return
  const candidate = selected.value
  const names = candidate.nodes.map(label).join('、')
  if (!window.confirm(`将“${names}”确认${verdict === 'same' ? '为同一对象' : '为不同对象'}？请核对下方属性和来源依据。裁决会保留记录，可以撤销。`)) return
  await mutate('/graph/identity-decisions', { revision: review.value.revision, candidate_id: candidate.candidate_id, verdict })
}
async function revoke(decision: Decision) {
  if (!review.value || busy.value || props.disabled || !window.confirm('撤销这次对象裁决？当前图谱会按剩余有效裁决重新显示，原始依据和历史记录保留。')) return
  await mutate(`/graph/identity-decisions/${encodeURIComponent(decision.decision_id)}/revoke`, { revision: review.value.revision })
}
async function mutate(endpoint: string, value: unknown) {
  busy.value = true; error.value = ''
  try {
    const headers: Record<string, string> = props.configRevision ? { 'If-Match': props.configRevision } : {}
    await request(`${projectPath(props.projectId)}${endpoint}`, { method: 'POST', headers, body: json(value) })
    await load(); emit('changed')
  } catch (e) { error.value = e instanceof Error ? e.message : '裁决未保存' }
  finally { busy.value = false }
}
watch([() => props.projectId, () => props.graph], () => { void load(0, 0) }, { immediate: true })
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
        <label for="identity-candidate">待检查对象（共 {{ review.total_candidates }} 组）</label>
        <select id="identity-candidate" v-model="selectedId" :disabled="busy"><option value="">选择一组候选</option><option v-for="item in review.candidates" :key="item.candidate_id" :value="item.candidate_id">{{ item.nodes.map(label).join(' / ') }} · {{ item.channels.map(channel).join('、') }}{{ item.status === 'needs_review' ? ' · 依据已变化' : '' }}</option></select>
        <div class="actions"><button v-if="offset" class="btn small" :disabled="busy" @click="load(Math.max(0, offset - 50))">上一页</button><button v-if="review.next_offset !== null" class="btn small" :disabled="busy" @click="load(review.next_offset!)">下一页</button></div>
      </template>
      <div v-if="selected" class="candidate-detail">
        <div class="candidate-nodes"><article v-for="node in selected.nodes" :key="node.id"><h3>{{ label(node) }}</h3><p>{{ node.kind }} · {{ node.status === 'accepted' ? '已接受' : '候选 / 未决' }}</p><pre>{{ JSON.stringify(node, null, 2) }}</pre></article></div>
        <p v-if="selected.supports_truncated" class="note amber">来源支持超过 500 条，当前显示前 500 条。</p><p>候选依据：{{ selected.channels.map(channel).join('、') }}</p>
        <details><summary>查看字段事实与来源支持（{{ selected.evidence.length }} 条事实）</summary><pre>{{ JSON.stringify({ facts: selected.facts, supports: selected.supports }, null, 2) }}</pre></details>
        <div class="actions"><button class="btn primary" :disabled="disabled || busy || selected.status !== 'candidate' || selected.has_active_decision" @click="decide('same')">确认为同一对象</button><button class="btn" :disabled="disabled || busy || selected.status !== 'candidate' || selected.has_active_decision" @click="decide('different')">确认为不同对象</button></div>
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
