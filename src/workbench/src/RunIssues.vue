<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ report: unknown }>()
type RunIssue = { code?: string; table?: string; reason_code?: unknown; field_path?: unknown; constraint?: unknown; count?: unknown }
const diagnosticCode = (value: unknown) => typeof value === 'string' && /^[a-z][a-z0-9_]{0,127}$/.test(value) ? value : ''
const diagnosticConstraint = (value: unknown) => typeof value === 'string' && /^[a-z][A-Za-z0-9_]{0,63}$/.test(value) ? value : ''
function fieldPath(value: unknown): string {
  if (typeof value !== 'string') return ''
  try {
    const path: unknown = JSON.parse(value)
    if (!Array.isArray(path) || path.length > 16 || !path.every(part =>
      typeof part === 'number' ? Number.isSafeInteger(part) && part >= 0 && part < 1000000 :
        typeof part === 'string' && (part === '*' || /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(part)))) return ''
    return path.reduce<string>((text, part) => text + (typeof part === 'number' ? `[${part}]` : `${text ? '.' : ''}${part}`), '') || '提交参数'
  } catch { return '' }
}
const diagnosticCount = (issue: RunIssue) => typeof issue.count === 'number' && Number.isSafeInteger(issue.count) && issue.count > 0 ? issue.count : null
const isDiagnostic = (issue: RunIssue) => issue.code === 'discovery_draft_rejected' || issue.code === 'discovery_draft_field_diagnostic'
const issueKey = (issue: RunIssue) => JSON.stringify([issue.code, issue.table, ...(isDiagnostic(issue) ? [diagnosticCode(issue.reason_code), fieldPath(issue.field_path), diagnosticConstraint(issue.constraint)] : [])])
const hints: Record<string, string> = {
  discovery_draft_rejected: '以下是本次草案被拒绝的原因及次数；是否仍待处理，以提交状态为准。',
  discovery_draft_field_diagnostic: '按字段路径和约束修正提交。此处只展示参数结构，不展示被拒绝的输入值。',
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
  excluded_column_projected: '提案使用了已排除的列。检查该列的用途，修正字段映射或有依据地修订排除声明后重新验证。',
  model_response_truncated: '模型回复未完整返回，本次内容不能作为完整提交。缩小单次提交范围，逐条提交后再完成发现。',
  unexplained_columns: '部分字段尚未被规则解释。检查是否需要补充别名、属性或明确排除；已有规则的发布不代表这些字段已覆盖。',
  concept_scope_evidence_required: '概念的共享范围缺少证据。选择适用于该表的定义章节，说明哪些主体可以共用该概念后重新提交。',
  semantic_review_unissued_evidence: '语义审查使用了本次未签发或重复的引用。按本次可用证据修正引用后重新审查；这不代表业务含义已被否定。',
  concept_key_label_conflict: '同一概念编码对应了不同名称。核对编码范围和名称定义，解决冲突后再发布，不能仅按名称合并。',
  concept_binding_conflict: '同一联合上下文对应了不同的概念组合。检查上下文身份键和组件映射，避免把不同组合当作同一对象。',
  reference_arity_mismatch: '对象身份键已变化，关系仍使用旧的列映射。按新的身份键修订关系端点后重新验证。',
  reference_self_link_forbidden: '关系两端解析为同一对象。检查端点选择和关系含义，不要直接允许自环。',
  concept_segment_mismatch: '名称不符合已声明的分解结构。修订声明或数据；本次映射没有发布。',
}
const issues = computed(() => {
  const report = props.report as { issues?: RunIssue[] } | null
  return (report?.issues || []).filter((item, index, items) => item.code && hints[item.code] && items.findIndex(other => issueKey(other) === issueKey(item)) === index)
})
</script>
<template><section v-if="issues.length" aria-label="问题与处理建议"><h3>问题与处理建议</h3><p v-for="issue in issues" :key="issueKey(issue)" class="note amber"><strong>{{ issue.table || issue.code }}</strong><br/>{{ hints[issue.code!] }}<template v-if="isDiagnostic(issue)"><br/><span v-if="diagnosticCode(issue.reason_code)">拒绝原因：<code>{{ diagnosticCode(issue.reason_code) }}</code>；</span><span v-if="fieldPath(issue.field_path)">字段：<code>{{ fieldPath(issue.field_path) }}</code>；</span><span v-if="diagnosticConstraint(issue.constraint)">约束：<code>{{ diagnosticConstraint(issue.constraint) }}</code>；</span><span v-if="diagnosticCount(issue)">发生 {{ diagnosticCount(issue) }} 次</span></template></p></section></template>
