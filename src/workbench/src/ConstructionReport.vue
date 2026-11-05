<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ report: unknown }>()
type Check = { passed?: boolean | null; diagnostics?: Array<{ code?: string }>; metrics?: Record<string, number> }
type Draft = { draft_id: string; revision: number; status: string; entity_types: number; relation_types: number; mappings: number; unresolved?: string[]; decisions?: string[]; suggestions?: string[]; validation?: Check }
type Usage = { input_tokens?: number; output_tokens?: number; total_tokens?: number; cache_read_tokens?: number; cache_write_tokens?: number; max_input_tokens?: number; measured_calls?: number; dispatched_calls?: number | null; missing_usage_calls?: number; complete?: boolean; by_purpose?: { compaction?: { total_tokens?: number } } }
type StructurePublication = { draft_id: string; revision: number; path?: string; validation_id?: string }
type Construction = {
  drafts?: Draft[]
  summary?: { draft_count?: number; published_drafts?: number; published_structures?: number; entity_type_count?: number; relation_type_count?: number; mapping_count?: number }
  execution?: { model_runtime?: string; data_validation?: string; structure_validation?: string; instance_enumeration?: string; structure_publications?: StructurePublication[] }
  acceptance?: { independent_gold?: string; customer_database?: string }
  usage?: Usage
  statistics?: { complete?: boolean; nodes?: number; edges?: number; entity_types?: Record<string, { nodes: number; unique_labels: number; unique_source_labels?: number; aliases: number }>; relation_types?: Record<string, { edges: number; multiple_parent_nodes: number; acyclic: boolean; cycle_nodes: number; acyclic_declared: boolean }> }
}
const report = computed(() => props.report as { construction?: Construction; validation?: { construction_target?: string }; counts?: { structures_published?: number } } | null)
const data = computed(() => report.value?.construction)
const structureOnly = computed(() => report.value?.validation?.construction_target === 'structure')
const structurePublications = computed(() => data.value?.execution?.structure_publications || [])
const publishedStructures = computed(() => data.value?.summary?.published_structures ?? report.value?.counts?.structures_published ?? (data.value?.execution?.structure_publications ? structurePublications.value.length : undefined))
const modelLabels: Record<string, string> = { executed: '已执行真实模型调用', test_double: '仅模拟调用', not_run: '未执行', interrupted: '调用过程被中断' }
const checkLabels: Record<string, string> = { passed: '已通过', failed: '未通过', not_run: '未执行', incomplete: '尚未完成' }
const acceptanceLabels: Record<string, string> = { passed: '已验收', failed: '未通过', not_run: '尚未验收', partial: '尚未完成' }
const draftLabels: Record<string, string> = { candidate: '草稿已保存', validated: '数据检查已通过', published: '已发布', needs_revision: '需要修订', stale: '来源已变化，需重验' }
const draftCheck = (draft: Draft) => draft.validation?.passed === true ? '已通过' : draft.validation?.passed === false ? '未通过' : '未执行'
const structurePublished = (draft: Draft) => structurePublications.value.some(item => item.draft_id === draft.draft_id && item.revision === draft.revision)
const structureDraftCheck = (draft: Draft) => structurePublished(draft) || data.value?.execution?.structure_validation === 'passed' ? '已通过' : '未记录单项结果'
const quantity = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value.toLocaleString('zh-CN') : '未记录'
const usageValue = (value: number | undefined) => (data.value?.usage?.measured_calls || data.value?.usage?.complete) ? quantity(value) : '未记录'
const compactionTokens = computed(() => data.value?.usage?.by_purpose?.compaction?.total_tokens ?? (data.value?.usage?.complete ? 0 : undefined))
</script>

<template>
  <section v-if="data" class="construction-report" aria-label="图谱构建结果">
    <h3>{{ structureOnly ? '图谱结构构建结果' : '图谱构建结果' }}</h3>
    <p v-if="structureOnly" class="construction-summary">已发布 {{ quantity(publishedStructures) }} 个结构方案 · {{ quantity(data.summary?.entity_type_count) }} 类对象定义 · {{ quantity(data.summary?.relation_type_count) }} 类关系定义 · {{ quantity(data.summary?.mapping_count) }} 个字段映射</p>
    <p v-else class="construction-summary">已发布 {{ data.summary?.published_drafts ?? 0 }} 个图谱草稿包 · {{ data.summary?.entity_type_count ?? 0 }} 类实体 · {{ data.summary?.relation_type_count ?? 0 }} 类关系 · {{ data.summary?.mapping_count ?? 0 }} 个字段映射</p>
    <p class="field-hint">{{ structureOnly ? '结构方案保存对象、关系和字段映射，可用于后续实例构建。' : '一个草稿包可包含多个来源表、对象与关系，不等于一条抽取规则。' }}</p>
    <dl class="construction-checks">
      <div><dt>模型执行</dt><dd>{{ modelLabels[data.execution?.model_runtime || 'not_run'] || '状态未提供' }}</dd></div>
      <template v-if="structureOnly">
        <div><dt>结构检查</dt><dd>{{ checkLabels[data.execution?.structure_validation || 'not_run'] || '状态未提供' }}</dd></div>
        <div><dt>实例枚举与数据验证</dt><dd>{{ data.execution?.instance_enumeration === 'not_requested' ? '本次未请求' : '未记录执行结果' }}</dd></div>
      </template>
      <div v-else><dt>全量数据检查</dt><dd>{{ checkLabels[data.execution?.data_validation || 'not_run'] || '状态未提供' }}</dd></div>
      <div><dt>独立真值评测</dt><dd>{{ acceptanceLabels[data.acceptance?.independent_gold || 'not_run'] || '尚未验收' }}</dd></div>
      <div><dt>客户数据库验收</dt><dd>{{ acceptanceLabels[data.acceptance?.customer_database || 'not_run'] || '尚未验收' }}</dd></div>
    </dl>
    <p v-if="structureOnly" class="field-hint" aria-label="结构产物范围">本次检查对象、关系定义和字段映射，未枚举或验证实例数据。结构方案发布不代表实例图谱完整；业务解释来自 Agent，独立评测与客户验收分别记录。</p>
    <p v-else class="field-hint">发布表示已通过所记录的数据检查。业务解释来自 Agent，独立评测与客户验收分别记录。</p>
    <section v-if="structurePublications.length" aria-label="已发布结构方案">
      <h4>已发布结构方案</h4>
      <ul><li v-for="item in structurePublications" :key="`${item.draft_id}:${item.revision}`"><strong>{{ item.draft_id }} · 修订 {{ item.revision }}</strong><p v-if="item.path" class="structure-path">产物路径：<code>{{ item.path }}</code></p></li></ul>
      <p class="field-hint">路径相对于本次运行的 construction 目录，产物为结构 YAML。</p>
    </section>
    <section v-if="data.usage" aria-label="模型用量">
      <h4>模型用量</h4>
      <dl class="construction-checks">
        <div><dt>已记录输入 tokens（含缓存）</dt><dd>{{ usageValue(data.usage.input_tokens) }}</dd></div>
        <div><dt>已记录输出 tokens</dt><dd>{{ usageValue(data.usage.output_tokens) }}</dd></div>
        <div><dt>已记录合计 tokens</dt><dd>{{ usageValue(data.usage.total_tokens) }}</dd></div>
        <div><dt>其中摘要调用 tokens</dt><dd>{{ usageValue(compactionTokens) }}</dd></div>
      </dl>
      <p class="field-hint">缓存读取 {{ usageValue(data.usage.cache_read_tokens) }} · 缓存写入 {{ usageValue(data.usage.cache_write_tokens) }} · 单次输入峰值 {{ usageValue(data.usage.max_input_tokens) }} tokens。摘要调用已计入合计。</p>
      <p :class="data.usage.complete ? 'field-hint' : 'note amber'">{{ data.usage.complete ? '用量统计完整' : '用量统计不完整' }}：已记录 {{ quantity(data.usage.measured_calls) }} / 已派发 {{ quantity(data.usage.dispatched_calls) }} 次调用。<template v-if="!data.usage.complete">{{ quantity(data.usage.missing_usage_calls) }} 次调用缺少用量；已记录数值不能代表完整消耗，缺失用量不计为零。</template></p>
    </section>
    <template v-if="!structureOnly && data.statistics?.complete">
      <h4>已发布图谱统计</h4><p>{{ data.statistics.nodes }} 个节点 · {{ data.statistics.edges }} 条关系。以下计数由程序根据当前可见图谱计算。</p>
      <div v-if="Object.keys(data.statistics.entity_types || {}).length" class="table-scroll"><table><thead><tr><th>实体类型</th><th>节点</th><th>不同展示名称</th><th>不同来源名称</th><th>别名</th></tr></thead><tbody><tr v-for="(stats, kind) in data.statistics.entity_types" :key="kind"><th>{{ kind }}</th><td>{{ stats.nodes }}</td><td>{{ stats.unique_labels }}</td><td>{{ stats.unique_source_labels ?? '未记录' }}</td><td>{{ stats.aliases }}</td></tr></tbody></table></div>
      <div v-if="Object.keys(data.statistics.relation_types || {}).length" class="table-scroll"><table><thead><tr><th>关系类型</th><th>边</th><th>多入边节点</th><th>当前图是否有环</th><th>映射要求无环</th></tr></thead><tbody><tr v-for="(stats, kind) in data.statistics.relation_types" :key="kind"><th>{{ kind }}</th><td>{{ stats.edges }}</td><td>{{ stats.multiple_parent_nodes }}</td><td>{{ stats.acyclic ? '无环' : `有环（${stats.cycle_nodes} 个节点）` }}</td><td>{{ stats.acyclic_declared ? '是' : '未声明' }}</td></tr></tbody></table></div>
    </template>
    <article v-for="draft in data.drafts || []" :key="`${draft.draft_id}:${draft.revision}`" class="construction-draft">
      <h4>{{ draft.draft_id }} · 修订 {{ draft.revision }} <span class="badge">{{ structureOnly ? (structurePublished(draft) ? '结构方案已发布' : '结构映射草稿') : (draftLabels[draft.status] || draft.status) }}</span></h4>
      <p v-if="structureOnly">{{ draft.entity_types }} 类对象定义 · {{ draft.relation_types }} 类关系定义 · {{ draft.mappings }} 个映射 · 结构检查：{{ structureDraftCheck(draft) }}</p>
      <p v-else>{{ draft.entity_types }} 类实体 · {{ draft.relation_types }} 类关系 · {{ draft.mappings }} 个映射 · 数据检查：{{ draftCheck(draft) }}</p>
      <p v-if="!structureOnly && draft.validation?.diagnostics?.length" class="note amber">检查诊断：{{ draft.validation.diagnostics.map(item => item.code || '未提供代码').join('、') }}</p>
      <section v-if="draft.unresolved?.length" aria-label="目标缺口"><h5>目标缺口</h5><ul><li v-for="(item, i) in draft.unresolved" :key="i">{{ item }}</li></ul></section>
      <section v-if="draft.decisions?.length" aria-label="已采用的解释"><h5>已采用的解释</h5><ul><li v-for="(item, i) in draft.decisions" :key="i">{{ item }}</li></ul></section>
      <section v-if="draft.suggestions?.length" aria-label="可选建议"><h5>可选建议</h5><p class="field-hint">供后续选择，不影响本次目标完成状态。</p><ul><li v-for="(item, i) in draft.suggestions" :key="i">{{ item }}</li></ul></section>
    </article>
  </section>
</template>
<style scoped>
.construction-report{margin:20px 0;font-size:13px;line-height:1.7}.construction-report h3{margin:0 0 10px}.construction-report h4{margin:16px 0 6px;overflow-wrap:anywhere}.construction-report h5{font-size:12px;margin:12px 0 4px}.construction-summary{font-weight:600}.construction-checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}.construction-checks dt{color:var(--muted);font-size:11px}.construction-checks dd{margin:3px 0 0}.construction-draft{margin-top:18px;padding-top:2px;border-top:1px solid var(--line)}.construction-report ul{padding-left:20px;margin:5px 0}.construction-report li{margin:5px 0;white-space:pre-wrap;overflow-wrap:anywhere}.construction-report table{width:100%;border-collapse:collapse;font-size:12px}.construction-report th,.construction-report td{text-align:left;padding:7px;border-bottom:1px solid var(--line)}
</style>
