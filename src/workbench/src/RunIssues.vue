<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ report: unknown }>()
type RunIssue = { code?: string; table?: string; status?: unknown; stage?: unknown; column_names?: unknown; reason_code?: unknown; field_path?: unknown; constraint?: unknown; count?: unknown; category?: unknown; next_action?: unknown; draft_id?: string; message?: unknown }
const categories: Record<string, string> = { semantic_decision: '业务判断', evidence_gap: '证据不足', proposal_contract: '提案格式或约束', review_protocol: '审查响应错误', internal_error: '程序错误', goal_gap: '目标缺口', decision: '已采用的解释', suggestion: '可选建议' }
const actions: Record<string, string> = { continue_other_candidates: '继续处理其他候选', gather_evidence: '补充证据', revise_proposal: '修订提案', retry_review: '重新审查', fix_code: '修复代码后续跑' }
const states: Record<string, string> = { rejected: '已拒绝，提案已结束', resolved: '已处理', unresolved: '分析已结束，结论仍有缺口', deferred: '工作尚未完成', partial: '执行未完成', diagnostic: '过程记录' }
const enumLabel = (value: unknown, labels: Record<string, string>) => typeof value === 'string' && Object.prototype.hasOwnProperty.call(labels, value) ? labels[value] : ''
const protocolStages: Record<string, string> = { provider_envelope: '接口响应结构', provider_usage: '用量字段', response_message: '消息结构', response_json: 'JSON 解析', response_schema: '输出字段校验', repair_contract: '格式修复结果' }
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
const isDiagnostic = (issue: RunIssue) => issue.code === 'discovery_draft_rejected' || issue.code === 'discovery_draft_field_diagnostic' || issue.code === 'model_protocol_diagnostic'
function columnNames(issue: RunIssue): string[] {
  if (!['unexplained_columns', 'coverage_shared_label_unresolved'].includes(issue.code || '') || typeof issue.column_names !== 'string' || issue.column_names.length > 32768) return []
  try {
    const names: unknown = JSON.parse(issue.column_names)
    return Array.isArray(names) && names.length <= 32 && names.every(name => typeof name === 'string' && name.length > 0 && name.length <= 256 && !/[\u0000-\u001f\u007f]/.test(name)) ? names : []
  } catch { return [] }
}
const constructionMessageCodes = new Set(['construction_goal_gap', 'construction_decision', 'construction_suggestion'])
const issueMessage = (issue: RunIssue) => constructionMessageCodes.has(issue.code || '') && issue.category === ({ construction_goal_gap: 'goal_gap', construction_decision: 'decision', construction_suggestion: 'suggestion' } as Record<string, string>)[issue.code!] && typeof issue.message === 'string' ? issue.message : ''
const issueKey = (issue: RunIssue) => JSON.stringify([issue.code, issue.table, issue.draft_id, issueMessage(issue), enumLabel(issue.status, states), columnNames(issue), ...(isDiagnostic(issue) ? [diagnosticCode(issue.reason_code), fieldPath(issue.field_path), diagnosticConstraint(issue.constraint), enumLabel(issue.stage, protocolStages)] : [])])
const hints: Record<string, string> = {
  returned_rows_budget_exhausted: '本轮所有来源累计返回的行数已超过上限，后续数据检查未完成。请在模型配置的“数据读取与构图额度”中调整累计返回行数，再继续运行；分批验证不会重置本轮计数。',
  returned_bytes_budget_exhausted: '本轮所有来源累计返回的数据量已超过上限，后续数据检查未完成。请在模型配置的“数据读取与构图额度”中调整累计返回数据量，再继续运行。这是累计读取额度，不是内存、磁盘容量或 MCP 通信超时。',
  memory_budget_exhausted: '构图或数据校验保留的工作集超过内存额度。请检查投影和索引规模，或在模型配置中调整“构图工作内存上限”；增加累计返回量额度不能解除这个限制。',
  construction_tool_transport_failed: '图谱工具通信失败，运行已停止反复调用并保留草稿。请检查进程诊断；恢复服务后可继续上次成果，或选择从零运行。',
  construction_compaction_failed: '上下文摘要连续截断，运行已停止反复压缩。原历史与已保存草稿保留；请检查模型输出限制，调整后继续运行。',
  construction_draft_validation_failed: '该草稿当前的数据检查未通过。此前已发布成果保留；查看具体诊断，修订或重新验证后再确认完成。',
  construction_unpublished_draft: '该草稿尚未发布。查看草稿的数据检查结果与本次实际轨迹，已发布的其他成果保留。',
  construction_conflicts_isolated: '无冲突部分已发布；冲突节点及相连边暂时隔离，完整候选与名称证据保留。修订后重新验证，不必删除其余成果。',
  construction_unresolved_work: '仍有目标未覆盖或草稿尚未发布，已发布结果保留。请查看下方构建结果中的具体事项。',
  source_retry_limit_exhausted: '反复请求已停止的数据库来源，运行已结束探索并保留草稿。请先恢复数据库，再重新运行；已有发布结果保留。',
  source_circuit_open: '该来源已达到本次运行的失败上限，暂不再查询。可继续查看结构方案及其他来源的结果，修复数据库后重新运行。',
  database_reconnect_failed: '数据库连接恢复失败，本次运行不再自动重连。请检查连接诊断和数据库状态后重新运行。',
  construction_goal_gap: '该事项影响本次目标覆盖，需补充证据或修订后继续。',
  construction_decision: '本次采用的解释，保留其依据供后续核对。',
  construction_suggestion: '这是后续可选改进，不影响本次目标的完成状态。',
  construction_scoped_access_not_supported: '这是旧版本运行记录：当时的构建流程不支持此访问范围。当前版本已支持按来源证据过滤结果，原权限不会自动扩大。',
  scope_binding_conflict: '同一来源表配置了不同的可见范围列。请核对 scope_bindings，明确采用哪一组列；系统不会自动选择或改动授权。',
  graph_support_visibility_missing: '该图缺少逐条来源证据的可见范围，暂不能安全展示。请保留历史文件，按当前范围绑定重新验证和发布。',
  business_graph_filtered_by_scopes: '当前访问范围内没有可见业务节点。未配置范围列的结果默认归入 public。请核对既有范围绑定，不要为了显示结果扩大权限。',
  graph_access_context_changed: '图谱发布后，来源或访问权限已改变。旧图暂不展示，历史文件保留；请按当前授权重新验证和发布。',
  discovery_draft_rejected: '这次提交未被接纳，原因和次数保留在此。输入错误本身不会产生待办。',
  discovery_draft_field_diagnostic: '按字段路径和约束修正提交。此处只展示参数结构，不展示被拒绝的输入值。',
  discovery_invalid_drafts: '存在尚未解决的保留草案冲突，或最终提交仍不合法。查看同表诊断和当前任务状态。',
  discovery_submission_incomplete: '发现过程提前中断。已有合法草案继续验证；检查预算、超时或响应截断后续跑。',
  discovery_submission_pending: '本次调查明确仍有工作待完成。已发布结果保留；过程记录中的旧待办不再阻塞已结束的调查。',
  model_protocol_diagnostic: '模型响应校验的过程记录。下方显示出错阶段、字段和约束；是否已恢复，以当前任务状态为准。',
  llm_schema_invalid: '本次调查仍未获得符合协议的回复。查看请求重试和响应诊断；已发布的结果继续保留。',
  semantic_claim_unresolved: '现有证据不能确认业务含义。补充定义、适用条件或反例，不要直接放宽审核。',
  semantic_claim_refuted: '提案存在反证。先修正业务解释，不要重复提交相同主张。',
  semantic_review_protocol_invalid: '语义审查响应不符合接口约定。检查模型兼容性；这不代表业务主张已被否定。',
  semantic_revision_evidence_missing: '新审查遗漏了必须核对的反证。补全本次已签发证据的分类后再验证，不能直接忽略原反证。',
  semantic_recovery_evidence_required: '历史查询证据不能直接用于本次恢复。需要按保留的字段映射重新核验；已有候选继续保留。',
  semantic_recovery_obligation_missing: '恢复审查尚未说明新结构如何处理历史反驳。补全该项审查后继续验证。',
  semantic_recovery_obligation_unresolved: '审查已完成，但现有证据仍不能支持新结构。保留候选，继续处理其他候选；补充证据后可再次审查。',
  recovery_state_invalid: '历史恢复约束存在程序错误，当前候选已保留。修复代码后续跑；补充业务证据或重复提交不能解决此问题。',
  semantic_recovery_shape_changed: '恢复提案改变了保留的身份、范围或位置约束。核对改变的原因，通过独立修订处理，不能直接绕过原约束。',
  semantic_revision_constraints_changed: '修订遗漏或改写了原有审查条件。保留原条件与证据要求后重新提交。',
  relation_waiting_for_foundation: '本表的层级对象尚未验证完成，相关关系探索等待基础规则。其他独立表继续处理。',
  no_confirmable_entity_group: '本表没有形成可确认的实体规则。结合本次其他错误判断是证据不足、提交失败还是目标不适用。',
  discovery_time_budget_exhausted: '探索时间已用完，剩余时间留给规则验证和发布。未完成的探索可在下一次运行继续。',
  discovery_query_budget_exhausted: '探索查询额度已用完，剩余额度留给完整数据验证。',
  relation_discovery_attempt_budget_exhausted: '本表关系探索已达到尝试上限，核心规则继续验证与发布。',
  relation_discovery_unresolved: '关系探索已结束，部分候选缺少依据。已有关系保留，补充资料后可重新调查。',
  relation_discovery_pending: '关系探索尚未完成。检查同表的提交、协议或执行错误后继续处理。',
  relation_endpoint_kind_mismatch: '所选关系不适用于这两类对象，该提案未发布。已共享身份的对象可保留多份来源支持，无须因此新增业务关系。',
  coverage_existing_rule_conflict: '补充提案改变了已有规则的身份或其他约束，需要单独修订，不能作为属性增补直接替换。',
  namespace_scope_missing: '发现可能遗漏的主体范围，但身份修订尚未通过。核对范围列的业务含义后重试；当前结果仍采用原规则。',
  identity_domain_evidence_unresolved: '跨表编码复用缺少可用的定义证据。补充适用的编码域说明并重新审查，不能仅按同码合并。',
  excluded_column_projected: '提案使用了已排除的列。检查该列的用途，修正字段映射或有依据地修订排除声明后重新验证。',
  model_response_truncated: '模型回复未完整返回，本次内容不能作为完整提交。缩小单次提交范围，逐条提交后再完成发现。',
  model_context_window_exceeded: '输入超出模型的 token 窗口。检查 tokenizer、上下文裁剪及输入窗口配置；这与 max_prompt_bytes 请求字节上限是两项独立限制。',
  model_reference_collision: '模型上下文中的短引用发生映射冲突。原始引用未被替换，检查引用映射代码后重试；这不代表业务对象冲突。',
  unexplained_columns: '部分字段尚未被规则解释。下面列出尚未映射的字段；名称已从其他表取得时，还需核验这些列能否作为同一属性的额外来源。',
  coverage_shared_label_unresolved: '重复名称列尚未通过共享编码核验。可能存在名称差异、缺失编码或空值，需核对后才能添加来源支持；已有名称和规则继续保留。',
  concept_scope_evidence_required: '概念的共享范围缺少证据。选择适用于该表的定义章节，说明哪些主体可以共用该概念后重新提交。',
  semantic_review_unissued_evidence: '语义审查使用了本次未签发或重复的引用。按本次可用证据修正引用后重新审查；这不代表业务含义已被否定。',
  concept_key_label_conflict: '同一概念编码对应了不同名称。核对编码范围和名称定义，解决冲突后再发布，不能仅按名称合并。',
  concept_binding_conflict: '同一联合上下文对应了不同的概念组合。检查上下文身份键和组件映射，避免把不同组合当作同一对象。',
  reference_arity_mismatch: '对象身份键已变化，关系仍使用旧的列映射。按新的身份键修订关系端点后重新验证。',
  reference_self_link_forbidden: '关系两端解析为同一对象。检查端点选择和关系含义，不要直接允许自环。',
  concept_segment_mismatch: '名称不符合已声明的分解结构。修订声明或数据；本次映射没有发布。',
}
const requestRetries = computed(() => {
  const counts = (props.report as { counts?: Record<string, unknown> } | null)?.counts
  const count = (key: string) => typeof counts?.[key] === 'number' && Number.isSafeInteger(counts[key]) && counts[key] >= 0 ? counts[key] as number : 0
  return { attempts: count('request_retry_attempts'), succeeded: count('request_retry_succeeded'), exhausted: count('request_retry_exhausted') }
})
const record = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
const nonnegativeInteger = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const quantity = (value: number, bytes: boolean) => bytes
  ? value >= 1048576 ? `${(value / 1048576).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} MiB` : `${value.toLocaleString('zh-CN')} B`
  : value.toLocaleString('zh-CN')
const sourceBudgetRows = computed(() => {
  const budget = record(record(record(props.report)?.validation)?.query_budget)
  if (!budget) return []
  return [
    { label: '查询次数', used: 'queries', limit: 'max_queries', remaining: 'remaining_queries', bytes: false },
    { label: '累计返回行数', used: 'returned_rows', limit: 'max_returned_rows', remaining: 'remaining_returned_rows', bytes: false },
    { label: '累计返回数据量', used: 'returned_bytes', limit: 'max_returned_bytes', remaining: 'remaining_returned_bytes', bytes: true },
  ].filter(row => nonnegativeInteger(budget[row.used]) || nonnegativeInteger(budget[row.limit]) || budget[row.limit] === null)
    .map(row => ({
      label: row.label,
      used: nonnegativeInteger(budget[row.used]) ? quantity(budget[row.used] as number, row.bytes) : '未记录',
      limit: budget[row.limit] === null ? '不限' : nonnegativeInteger(budget[row.limit]) ? quantity(budget[row.limit] as number, row.bytes) : '未记录',
      remaining: budget[row.limit] === null ? '不限' : nonnegativeInteger(budget[row.remaining]) ? quantity(budget[row.remaining] as number, row.bytes) : '未记录',
    }))
})
const issues = computed(() => {
  const report = props.report as { issues?: RunIssue[] } | null
  return (report?.issues || []).filter((item, index, items) => diagnosticCode(item.code) && items.findIndex(other => issueKey(other) === issueKey(item)) === index)
})
</script>
<template><section v-if="issues.length || requestRetries.attempts || requestRetries.exhausted || sourceBudgetRows.length" aria-label="问题与处理建议"><h3>问题与处理建议</h3><div v-if="sourceBudgetRows.length" class="source-budget" aria-label="本轮数据读取额度"><h4>本轮数据读取额度</h4><p class="field-hint">按本轮报告记录的配置与用量展示，所有来源和阶段累计；累计数据量不是当前内存占用，也不是模型 token。</p><div class="table-scroll"><table><thead><tr><th>项目</th><th>已用</th><th>上限</th><th>剩余</th></tr></thead><tbody><tr v-for="row in sourceBudgetRows" :key="row.label"><th scope="row">{{ row.label }}</th><td>{{ row.used }}</td><td>{{ row.limit }}</td><td>{{ row.remaining }}</td></tr></tbody></table></div></div><p v-if="requestRetries.attempts || requestRetries.exhausted" class="note" aria-label="请求重试">追加请求 {{ requestRetries.attempts }} 次，{{ requestRetries.succeeded }} 个请求恢复成功，{{ requestRetries.exhausted }} 个请求用尽重试额度。已恢复的请求错误仅作过程记录，任务状态按最终结果判断。</p><p v-for="issue in issues" :key="issueKey(issue)" class="note" :class="{ amber: !['rejected', 'resolved', 'diagnostic'].includes(String(issue.status)) }"><strong>{{ issue.table || issue.code }}</strong><span v-if="enumLabel(issue.status, states)"> · {{ enumLabel(issue.status, states) }}</span><br/>{{ hints[issue.code!] || '此问题暂未提供专门说明。请保留错误码，结合本次运行轨迹排查。' }}<template v-if="issueMessage(issue)"><br/>{{ issueMessage(issue) }}</template><template v-if="issue.draft_id"><br/>图谱草稿：{{ issue.draft_id }}</template><template v-if="columnNames(issue).length"><br/>相关列：{{ columnNames(issue).join('、') }}<span v-if="diagnosticCount(issue) && diagnosticCount(issue)! > columnNames(issue).length">（共 {{ diagnosticCount(issue) }} 列，仅展示前 {{ columnNames(issue).length }} 列）</span></template><template v-if="enumLabel(issue.category, categories) || enumLabel(issue.next_action, actions)"><br/><span v-if="enumLabel(issue.category, categories)">问题类型：{{ enumLabel(issue.category, categories) }}；</span><span v-if="enumLabel(issue.next_action, actions)">处理：{{ enumLabel(issue.next_action, actions) }}</span></template><template v-if="isDiagnostic(issue)"><br/><span v-if="issue.code === 'model_protocol_diagnostic' && enumLabel(issue.stage, protocolStages)">阶段：{{ enumLabel(issue.stage, protocolStages) }}；</span><span v-if="diagnosticCode(issue.reason_code)">拒绝原因：<code>{{ diagnosticCode(issue.reason_code) }}</code>；</span><span v-if="fieldPath(issue.field_path)">字段：<code>{{ fieldPath(issue.field_path) }}</code>；</span><span v-if="diagnosticConstraint(issue.constraint)">约束：<code>{{ diagnosticConstraint(issue.constraint) }}</code>；</span><span v-if="diagnosticCount(issue)">发生 {{ diagnosticCount(issue) }} 次</span></template></p></section></template>

<style scoped>
.source-budget { margin: 12px 0 20px; }
.source-budget h4 { margin: 0 0 6px; }
.source-budget .field-hint { margin-bottom: 10px; }
.source-budget th[scope=row] { font-weight: 500; text-align: left; }
</style>
