<script setup lang="ts">
import { ref, watch } from 'vue'
import { request, projectPath, json } from './api'
const props = defineProps<{ projectId: string; configRevision?: string; disabled?: boolean; refreshKey?: unknown }>()
type Candidate = { candidate_id: string; source: { source_id: string; schema: string; table: string }; proposal_kind: string; failure_code: string; review_stage: string; column_refs: string[]; document_refs: string[]; blocking_evidence_refs: string[]; required_evidence_refs: string[]; state: string; revision: number }
type Listing = { candidates: Candidate[]; total_candidates: number; next_offset: number | null }
const listing = ref<Listing | null>(null), busy = ref(false), error = ref(''), offset = ref(0)
let generation = 0
const stateName: Record<string, string> = { pending: '待处理', dismissed: '已搁置', retry_requested: '等待下次运行重新探索', revised: '已有修订，等待执行验证', resolved: '已验证替代规则' }
const reasons: Record<string, string> = { semantic_claim_refuted: '存在反证，需修订解释或补充证据', semantic_claim_unresolved: '业务含义尚不能确认', semantic_condition_unresolved: '适用条件尚未满足', semantic_review_missing_support: '缺少支持证据', semantic_review_incomplete_context: '审查未覆盖全部必要上下文', semantic_revision_no_progress: '修订未带来新证据', semantic_revision_constraints_changed: '修订改变了原有条件或约束' }
async function load(page = 0) {
  const epoch = ++generation; busy.value = true; error.value = ''
  try {
    const result = await request<Listing>(`${projectPath(props.projectId)}/discovery/candidates?offset=${page}`)
    if (epoch === generation) { listing.value = result; offset.value = page }
  } catch (e) { if (epoch === generation) error.value = e instanceof Error ? e.message : '候选读取失败' }
  finally { if (epoch === generation) busy.value = false }
}
async function decide(candidate: Candidate, action: string) {
  if (busy.value || props.disabled || !props.configRevision) return
  busy.value = true; error.value = ''
  try {
    await request(`${projectPath(props.projectId)}/discovery/decisions`, { method: 'POST', headers: { 'If-Match': props.configRevision }, body: json({ candidate_id: candidate.candidate_id, revision: candidate.revision, action }) })
    await load(offset.value)
  } catch (e) { error.value = e instanceof Error ? e.message : '操作未保存' }
  finally { busy.value = false }
}
watch([() => props.projectId, () => props.refreshKey], () => { listing.value = null; void load() }, { immediate: true })
</script>
<template>
  <section class="panel discovery-review" aria-label="发现待处理提案">
    <div class="panel-head"><h2>发现待处理提案 · {{ listing?.total_candidates ?? 0 }}</h2><button class="btn small" :disabled="busy" @click="load(offset)">刷新提案</button></div>
    <div class="panel-body">
      <p class="field-hint">查看失败位置和证据引用，补充说明后可请求重新探索。请求在下一次运行生效；搁置或重试均不会直接发布业务事实。</p>
      <p v-if="disabled" class="field-hint">运行结束并保存配置后可处理提案。</p>
      <p v-if="error" role="alert" class="note amber">{{ error }}</p><p v-if="busy" role="status">正在读取或保存…</p>
      <p v-if="listing && !listing.total_candidates" class="muted">当前没有待处理的发现提案。</p>
      <article v-for="candidate in listing?.candidates" :key="candidate.candidate_id" class="discovery-item">
        <h3>{{ candidate.source.source_id }} / {{ candidate.source.schema }}.{{ candidate.source.table }}</h3>
        <p>{{ stateName[candidate.state] || candidate.state }} · {{ candidate.proposal_kind }}</p><p>{{ reasons[candidate.failure_code] || candidate.failure_code }}</p>
        <details><summary>查看列和证据引用</summary><p>相关列：{{ candidate.column_refs.join('、') || '未提供' }}</p><p>定义文档：{{ candidate.document_refs.join('、') || '未提供' }}</p><p>反证引用：{{ candidate.blocking_evidence_refs.join('、') || '未提供' }}</p><p>需核对引用：{{ candidate.required_evidence_refs.join('、') || '未提供' }}</p></details>
        <div v-if="candidate.state !== 'resolved'" class="actions"><button class="btn primary small" :disabled="disabled || busy || candidate.state === 'retry_requested'" @click="decide(candidate, 'retry')">请求重新探索</button><button v-if="candidate.state !== 'dismissed'" class="btn small" :disabled="disabled || busy" @click="decide(candidate, 'dismiss')">搁置</button><button v-else class="btn small" :disabled="disabled || busy" @click="decide(candidate, 'reopen')">恢复待处理</button></div>
      </article>
      <div class="actions"><button v-if="offset" class="btn small" :disabled="busy" @click="load(Math.max(0, offset - 50))">上一页提案</button><button v-if="listing?.next_offset != null" class="btn small" :disabled="busy" @click="load(listing.next_offset)">下一页提案</button></div>
    </div>
  </section>
</template>
<style scoped>.discovery-review{margin-top:18px}.discovery-item{padding:14px 0;border-bottom:1px solid #e2e8f0;overflow-wrap:anywhere}.discovery-item .actions{margin-top:12px}</style>
