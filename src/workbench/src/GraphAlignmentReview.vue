<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { projectPath, request } from './api'
import type { Graph, BusinessContext } from './types'
const props = defineProps<{ graph: Graph; projectId: string; version?: string; businessContext?: BusinessContext | null }>()
type Pending = { id?: string; reason?: string; node_ids?: string[]; definition_ids?: string[]; status?: string }
const page = ref<Graph | null>(null), offset = ref(0)
const visible = computed(() => page.value || props.graph)
const counts = computed(() => ['pending_alignment_count', 'pending_definition_count', 'pending_relation_count', 'pending_cycle_count']
  .map(key => Number(visible.value[key] || 0)))
const total = computed(() => counts.value.reduce((sum, n) => sum + n, 0))
const pending = computed(() => [
  ...((visible.value.definition_conflicts || []) as Pending[]),
  ...((visible.value.alignment_candidates || []) as Pending[]),
  ...((visible.value.unresolved_relations || []) as Pending[]),
  ...((visible.value.context_cycle_diagnostics || []) as Pending[]),
])
const details = ref<unknown>(null), error = ref(''), busy = ref(false)
let epoch = 0
const reason = (value?: string) => ({ incompatible_definition_contracts: '同名定义尚未对齐',
  cross_unit_unverified_identity_domain: '对象身份尚未确认', ambiguous_inherited_identity: '关系端点有多个候选', definition_endpoint_ambiguous: '关系端点有多个候选',
  same_name_requires_identity_review: '名称相近，需确认身份', context_cycle_analysis_incomplete: '限定关系的环检测尚未完成' }[value || ''] || '对应关系尚未确认')
const label = (id: string) => props.graph.nodes.find(n => n.id === id)?.label || id
async function loadPage(next: number) {
  const current = ++epoch
  busy.value = true; error.value = ''; details.value = null
  const params = new URLSearchParams({ include_candidates: 'true', review_offset: String(next), limit: '1' })
  const version = props.version || props.graph.version_id
  if (typeof version === 'string' && version) params.set('version', version)
  if (props.businessContext != null) params.set('business_context', JSON.stringify(props.businessContext))
  try {
    const result = await request<Graph>(`${projectPath(props.projectId)}/graph?${params}`)
    if (current === epoch) { page.value = result; offset.value = next }
  } catch (e) { if (current === epoch) error.value = e instanceof Error ? e.message : '读取失败' }
  finally { if (current === epoch) busy.value = false }
}
async function inspect(id: string, relation = false) {
  const current = ++epoch; busy.value = true; error.value = ''; details.value = null
  const params = new URLSearchParams({ [relation ? 'fact_id' : 'entity_id']: id, include_candidates: 'true' })
  const version = props.version || props.graph.version_id
  if (typeof version === 'string' && version) params.set('version', version)
  if (props.businessContext != null) params.set('business_context', JSON.stringify(props.businessContext))
  try {
    const value = await request(`${projectPath(props.projectId)}/graph/details?${params}`)
    if (current === epoch) details.value = value
  } catch (e) { if (current === epoch) error.value = e instanceof Error ? e.message : '依据读取失败' }
  finally { if (current === epoch) busy.value = false }
}
watch([() => props.graph, () => props.businessContext, () => props.projectId], () => { epoch++; page.value = null; offset.value = 0; details.value = null; error.value = ''; busy.value = false })
</script>
<template>
  <section v-if="pending.length" class="panel alignment-review">
    <div class="panel-head"><h2>待对齐内容（{{ total || pending.length }}）</h2></div>
    <div class="panel-body">
      <p class="field-hint">对象与原有事实分别保留。这里的候选对应关系尚未用于图遍历或推理。</p>
      <details v-for="(item, index) in pending" :key="index">
        <summary>{{ reason(item.reason) }} · {{ item.node_ids?.length || 0 }} 个对象</summary>
        <div class="actions"><button v-if="item.id" class="btn small" :disabled="busy" @click="inspect(item.id, true)">查看未决关系依据</button><button v-for="id in item.node_ids" :key="id" class="btn small" :disabled="busy" @click="inspect(id)">{{ label(id) }} · 查看依据</button></div>
      </details>
      <p v-if="busy">正在读取依据…</p><p v-if="error" class="note amber" role="alert">{{ error }}</p>
      <details v-if="details" open><summary>对象事实与引用</summary><pre>{{ JSON.stringify(details, null, 2) }}</pre></details>
      <div v-if="Math.max(...counts) > 200" class="actions">
        <button class="btn small" :disabled="busy || offset === 0" @click="loadPage(offset - 200)">上一页</button>
        <span>第 {{ offset / 200 + 1 }} 页</span>
        <button class="btn small" :disabled="busy || offset + 200 >= Math.max(...counts)" @click="loadPage(offset + 200)">下一页</button>
      </div>
    </div>
  </section>
</template>
<style scoped>.alignment-review { margin-top: 16px } details { margin: 8px 0 } pre { max-height: 400px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere }</style>
