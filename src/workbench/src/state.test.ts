import { describe, expect, it, vi, afterEach } from 'vitest'
import { assertNoCredentials, graphOutcome, isActive, splitLines } from './state'
import { ApiError, request } from './api'
import type { Run } from './types'

afterEach(() => vi.unstubAllGlobals())
describe('运行和配置边界', () => {
  it('业务产出与运行状态分开，元数据不冒充业务图', () => {
    expect(graphOutcome({ status: 'COMPLETED_WITH_GAPS', validation: { business_graph: 'empty' }, counts: { tables: 1 } })).toContain('没有已发布的业务实体')
    expect(graphOutcome({ validation: { business_graph: 'entities_only' }, counts: { business_nodes: 2, business_edges: 0 } })).toContain('2 个业务实体，尚未确认实体间关系')
    expect(graphOutcome({ validation: { business_graph: 'entities_and_relations' }, counts: { business_nodes: 9, business_edges: 10 } })).toContain('9 个业务实体、10 条业务关系')
    expect(graphOutcome({ validation: { business_graph: 'candidates_only' } })).toContain('只有候选')
    expect(graphOutcome({ status: 'completed' })).toBe('')
  })
  it('只有后端 queued/running 状态触发轮询', () => {
    for (const status of ['completed', 'partial', 'failed', 'cancelled', 'interrupted']) expect(isActive({ status } as Run)).toBe(false)
    expect(isActive({ status: 'running' } as Run)).toBe(true)
    expect(isActive({ status: 'queued' } as Run)).toBe(true)
  })
  it('允许环境变量引用，拒绝嵌套明文凭据', () => {
    expect(() => assertNoCredentials({ model: { api_key_env: 'MODEL_KEY' }, sources: [{ dsn_env: 'DB_DSN' }] })).not.toThrow()
    expect(() => assertNoCredentials({ sources: [{ password: 'private' }] })).toThrow('明文密钥')
    expect(() => assertNoCredentials({ model: { api_key: 'private' } })).toThrow('明文密钥')
  })
  it('只修剪目标条目，不拆分内部标点', () => expect(splitLines('组织、层级\n\n 指标与度量 ')).toEqual(['组织、层级', '指标与度量']))
})
describe('API 安全错误与真实通信', () => {
  it('禁用浏览器响应缓存，JSON 请求设置类型', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"project_id":"p1"}'))
    vi.stubGlobal('fetch', fetchMock)
    await expect(request('/projects', { method: 'POST', body: '{}' })).resolves.toEqual({ project_id: 'p1' })
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ cache: 'no-store', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } })
  })
  it('只展示安全代码，不把服务端原文或秘密放进错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'config_invalid', message: 'secret data' } }), { status: 400 })))
    try { await request('/config') } catch (error) { expect(error).toBeInstanceOf(ApiError); expect(String(error)).not.toContain('secret data'); expect((error as ApiError).code).toBe('config_invalid') }
  })
  it('拒绝非短代码，网络失败不暴露原始异常', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: '<script>private</script>' } }), { status: 500 })))
    await expect(request('/x')).rejects.toMatchObject({ code: 'http_500' })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private socket string')))
    await expect(request('/x')).rejects.toMatchObject({ code: 'network_unavailable' })
  })
})
