<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ report: unknown }>()
const hints: Record<string, string> = {
  discovery_invalid_drafts: '仍有未处理的草案提交错误。查看字段诊断，纠正参数或明确撤回该草案后再完成提交。',
  discovery_submission_incomplete: '发现过程提前中断。已有合法草案继续验证；检查预算、超时或响应截断后续跑。',
  discovery_submission_pending: 'Agent 标记仍有待探索工作。已发布结果与未完成工作分别保留。',
  semantic_claim_unresolved: '现有证据不能确认业务含义。补充定义、适用条件或反例，不要直接放宽审核。',
  semantic_claim_refuted: '提案存在反证。先修正业务解释，不要重复提交相同主张。',
  semantic_review_protocol_invalid: '语义审查响应不符合接口约定。检查模型兼容性；这不代表业务主张已被否定。',
  no_confirmable_entity_group: '本表没有形成可确认的实体规则。结合本次其他错误判断是证据不足、提交失败还是目标不适用。',
  discovery_time_budget_exhausted: '探索时间已用完，剩余时间留给规则验证和发布。未完成的探索可在下一次运行继续。',
  discovery_query_budget_exhausted: '探索查询额度已用完，剩余额度留给完整数据验证。',
  relation_discovery_attempt_budget_exhausted: '本表关系探索已达到尝试上限，核心规则继续验证与发布。',
  coverage_existing_rule_conflict: '补充提案改变了已有规则的身份或其他约束，需要单独修订，不能作为属性增补直接替换。',
  namespace_scope_missing: '发现可能遗漏的主体范围，但身份修订尚未通过。核对范围列的业务含义后重试；当前结果仍采用原规则。',
  identity_domain_evidence_unresolved: '跨表编码复用缺少可用的定义证据。补充适用的编码域说明并重新审查，不能仅按同码合并。',
  reference_arity_mismatch: '对象身份键已变化，关系仍使用旧的列映射。按新的身份键修订关系端点后重新验证。',
  reference_self_link_forbidden: '关系两端解析为同一对象。检查端点选择和关系含义，不要直接允许自环。',
  concept_segment_mismatch: '名称不符合已声明的分解结构。修订声明或数据；本次映射没有发布。',
}
const issues = computed(() => {
  const report = props.report as { issues?: Array<{ code?: string; table?: string }> } | null
  return (report?.issues || []).filter((item, index, items) => item.code && hints[item.code] && items.findIndex(other => other.code === item.code && other.table === item.table) === index)
})
</script>
<template><section v-if="issues.length" aria-label="问题与处理建议"><h3>问题与处理建议</h3><p v-for="issue in issues" :key="`${issue.code}:${issue.table}`" class="note amber"><strong>{{ issue.table || issue.code }}</strong><br/>{{ hints[issue.code!] }}</p></section></template>
