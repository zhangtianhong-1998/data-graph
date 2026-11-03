import { describe, expect, it } from 'vitest'
import { formatTraceValue, traceBlocks } from './tracePresentation'
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
})
