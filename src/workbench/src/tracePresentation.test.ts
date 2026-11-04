import { describe, expect, it } from 'vitest'
import { formatTraceValue, traceBlocks, traceContext, traceToolName } from './tracePresentation'
const event = (kind: string, data: unknown) => ({ seq: 7, time: '', kind, data })
describe('actual recorded agent conversation', () => {
  it('reads actual wire messages in order with tools and roles intact', () => {
    const blocks = traceBlocks(event('model_request', { payload: { model: 'local', messages: [
      { role: 'system', content: 'system instruction' }, { role: 'user', content: '{"columns":["编码"]}' },
      { role: 'assistant', content: null, tool_calls: [{ id: 'call-1', function: { name: 'check_fd', arguments: '{"a":1}' } }] },
      { role: 'tool', tool_call_id: 'call-1', content: '{"status":"ok"}' },
    ], tools: [{ function: { name: 'check_fd' } }], max_tokens: 65536 } }))
    expect(blocks.map(block => block.title)).toEqual(['消息 1 · 系统指令','消息 2 · 用户输入','消息 3 · 请求调用工具 · check_fd','消息 4 · 工具返回','本次可用工具定义','模型请求参数'])
    expect(blocks[2]!.value).toBe('{"a":1}')
    expect(formatTraceValue(blocks[3]!.value)).toContain('"status": "ok"')
  })
  it('shows only reasoning actually returned and keeps provider finish reason', () => {
    const plain = traceBlocks(event('model_response', { response: { choices: [{ message: { role: 'assistant', content: 'done' }, finish_reason: 'stop' }] } }))
    expect(plain.some(block => block.title.includes('推理'))).toBe(false)
    expect(plain.map(block => block.value)).toEqual(['done','stop'])
    const reasoning = traceBlocks(event('model_response', { response: { choices: [{ message: { role: 'assistant', reasoning_content: 'returned reasoning', tool_calls: [{ function: { name: 'finish_discovery', arguments: '{"pending":false}' } }] } }] } }))
    expect(reasoning.map(block => block.value)).toEqual(['returned reasoning','{"pending":false}'])
  })
  it('shows tool inputs and results without replacing them with progress counters', () => {
    expect(traceBlocks(event('tool_call', { call: { name: 'profile_columns', input: { columns: ['编码'] } } }))[0]).toEqual({ title: '工具输入 · profile_columns', value: { columns: ['编码'] } })
    expect(traceBlocks(event('tool_result', { name: 'profile_columns', content: [{ type: 'text', text: '{"rows":9}' }] }))[0]!.value).toBe('{"rows":9}')
  })
  it('renders native Harness message blocks and the actual dispatch input', () => {
    const blocks = traceBlocks(event('model_request', { recording_source: 'native_llm_stream_hook', payload: {
      messages: [{ role: 'assistant', content: [{ type: 'tool-call', name: 'mcp__graph__describe_sources', arguments: '{}' }] },
        { role: 'tool', content: [{ type: 'text', text: '{"tables":[]}' }] }], tools: [], model: 'local',
    } }))
    expect(blocks[0]).toEqual({ title: '消息 1 · 请求调用工具 · mcp__graph__describe_sources', value: '{}' })
    expect(blocks[1]).toEqual({ title: '消息 2 · 工具返回', value: '{"tables":[]}' })
    expect(blocks.some(block => block.title === '记录范围')).toBe(true)
    const response = traceBlocks(event('model_response', { response: { message: { role: 'assistant', content: [
      { type: 'reasoning', text: '服务返回的内容' }, { type: 'text', text: '已完成' },
    ] }, usage: { inputTokens: 100 }, finish_reason: 'stop' }, native: { original: true } }))
    expect(response.map(block => block.value)).toEqual(['服务返回的内容', '已完成', 'stop', { inputTokens: 100 }])
    expect(traceBlocks(event('model_response', { response: { message: { role: 'assistant', content: [{ type: 'text', text: '仅正文' }] } } })).some(block => block.title.includes('推理'))).toBe(false)
  })
  it('shows native step and tool-call associations without relabeling them as discovery quotas', () => {
    const call = event('tool_call', { name: 'mcp__graph__inspect_source', call_id: 'call-1', harness_turn: 1, harness_step: 3, arguments: '{}' })
    expect(traceToolName(call)).toBe('mcp__graph__inspect_source')
    expect(traceContext(call)).toBe('会话第 1 轮 · 执行第 3 步 · 工具调用 call-1')
    expect(traceBlocks(call)[0]!.value).toBe('{}')
  })
  it('labels a pruned replacement as a history update, preserving its source link', () => {
    const replacement = event('context_history_edit', { call_id: 'call-1', source_event_seqs: [13],
      content: [{ type: 'text', text: 'pruned history' }] })
    expect(traceBlocks(replacement)[0]!.value).toContain('工具没有重新执行')
    expect(traceBlocks(replacement)[1]!.value).toEqual([13])
    expect(traceBlocks(replacement)[2]!.value).toBe('pruned history')
    expect(traceContext(replacement)).toContain('更新历史上下文')
  })
})
