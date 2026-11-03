/** Display only recorded messages and calls; never reconstruct missing agent reasoning. */
export type TraceEvent = { seq: number; time: string; kind: string; data: unknown }
export type TraceBlock = { title: string; value: unknown }
export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : [] }
export function formatTraceValue(value: unknown): string {
  if (typeof value === 'string') {
    try { return JSON.stringify(JSON.parse(value), null, 2) } catch { return value }
  }
  return JSON.stringify(value, null, 2) ?? ''
}
const roles: Record<string, string> = { system: '系统指令', user: '用户输入', assistant: '模型输出', tool: '工具返回', developer: '开发指令' }
export function traceToolName(event: TraceEvent): string {
  const data = record(event.data), call = record(data.call)
  return String(data.name || call.name || '')
}
export function traceContext(event: TraceEvent): string {
  const data = record(event.data), items: string[] = []
  const tasks: Record<string, string> = { T0_discovery: '结构发现', T1_table_discovery: '表内对象发现', T2_cross_table: '跨表关系发现', T3_hierarchy: '层级发现', T4_document: '文档分析', T5_revision: '提案修订', semantic_review: '语义复核' }
  if (data.task) items.push(tasks[String(data.task)] || String(data.task))
  if (data.discovery_step) items.push(`探索第 ${data.discovery_step}${data.max_discovery_steps ? `/${data.max_discovery_steps}` : ''} 轮`)
  if (data.attempt) items.push(`请求第 ${data.attempt} 次`)
  return items.join(' · ')
}
function messageBlocks(message: unknown, prefix = ''): TraceBlock[] {
  const value = record(message), result: TraceBlock[] = []
  const role = roles[String(value.role)] || String(value.role || '消息')
  if (value.content != null) result.push({ title: `${prefix}${role}${value.name ? ` · ${value.name}` : ''}`, value: value.content })
  if (value.reasoning_content != null) result.push({ title: `${prefix}接口返回的推理内容`, value: value.reasoning_content })
  for (const call of array(value.tool_calls)) {
    const fn = record(record(call).function)
    result.push({ title: `${prefix}请求调用工具 · ${fn.name || record(call).name || '未命名'}`, value: fn.arguments ?? call })
  }
  return result
}
export function traceBlocks(event: TraceEvent): TraceBlock[] {
  const data = record(event.data)
  if (event.kind === 'model_request') {
    const payload = record(data.payload ?? data)
    const blocks = array(payload.messages).flatMap((message, index) => messageBlocks(message, `消息 ${index + 1} · `))
    // Tool definitions are part of the actual request, separate from dialogue.
    if (payload.tools) blocks.push({ title: '本次可用工具定义', value: payload.tools })
    const { messages: _messages, tools: _tools, ...parameters } = payload
    if (Object.keys(parameters).length) blocks.push({ title: '模型请求参数', value: parameters })
    return blocks.length ? blocks : [{ title: '已记录的模型输入', value: data }]
  }
  if (event.kind === 'model_response') {
    const response = record(data.response ?? data)
    const blocks = array(response.choices).flatMap((choice, index) => {
      const item = record(choice)
      const output = messageBlocks(item.message ?? item.delta, array(response.choices).length > 1 ? `输出 ${index + 1} · ` : '')
      if (item.finish_reason != null) output.push({ title: '接口结束原因', value: item.finish_reason })
      return output
    })
    if (response.usage) blocks.push({ title: '接口报告用量', value: response.usage })
    return blocks.length ? blocks : [{ title: '已记录的模型返回', value: response }]
  }
  if (event.kind === 'tool_call' || event.kind === 'tool_request') {
    const call = record(data.call)
    return [{ title: `工具输入 · ${traceToolName(event)}`, value: call.input ?? data.input ?? data.arguments ?? data }]
  }
  if (event.kind === 'tool_result') {
    const content = array(data.content)
    return content.length ? content.map((item, index) => ({ title: `工具返回 ${index + 1} · ${traceToolName(event)}`, value: record(item).text ?? item })) : [{ title: '工具返回', value: data }]
  }
  if (event.kind === 'agent_start') {
    return [{ title: 'Agent 系统指令', value: data.system }, { title: 'Agent 初始材料', value: data.packet }].filter(block => block.value != null)
  }
  if (event.kind === 'agent_end') {
    const { agent_id: _agent, task: _task, reply, ...summary } = data
    return [{ title: 'Agent 收尾状态（已记录）', value: summary }, ...(reply != null ? [{ title: 'Agent 最后返回', value: reply }] : [])]
  }
  return [{ title: event.kind.endsWith('error') ? '失败诊断' : '记录内容', value: data }]
}
