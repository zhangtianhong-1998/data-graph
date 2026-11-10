// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App as VueApp } from 'vue'
import App from './App.vue'
import { clearSessions } from './api'
import { modelLimitError } from './modelLimits'
import type { RunConfig } from './types'
let app: VueApp, root: HTMLDivElement, config: RunConfig, revision: number
let limits: Record<string, unknown>, rejectLimits: boolean
const runModels: Array<Record<string, unknown>> = []
const calls: Array<{ url: string; method: string; body: unknown; headers: Headers }> = []
const project = { project_id: 'p', name: '模型配置', files: [], created_at: '', updated_at: '' }
const response = (value: unknown, etag = '') => new Response(JSON.stringify(value), { status: 200, headers: etag ? { ETag: etag } : {} })
function button(name: string) { return [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === name)! }
async function edit(id: string, value: string) { const input = root.querySelector<HTMLInputElement>(`#${id}`)!; input.value = value; input.dispatchEvent(new Event('input', { bubbles: true })); await nextTick() }
async function mount() {
  root = document.createElement('div'); document.body.append(root); app = createApp(App); app.mount(root)
  await vi.waitFor(() => expect(button('模型配置')).toBeDefined())
  button('模型配置').click(); await nextTick()
  await vi.waitFor(() => expect(button('保存配置').disabled).toBe(false))
}
beforeEach(() => {
  clearSessions(); sessionStorage.clear(); revision = 1; calls.length = 0; runModels.length = 0; rejectLimits = false
  limits = { max_calls: 240, max_prompt_tokens: 1000, max_output_tokens: 200, timeout_seconds: 60, max_prompt_bytes: 131072, max_response_bytes: 262144, context_window_tokens: 2000000 }
  config = { sources: [{ id: 'data', kind: 'csv_file' }], goal: { purpose: '保留模型输入' }, model: { mode: 'disabled', ...limits, max_calls: 240, max_tokens: null } }
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method || 'GET', body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined, headers = new Headers(init.headers)
    calls.push({ url, method, body, headers })
    if (url === '/api/projects') return response({ projects: [project] })
    if (url === '/api/projects/p') return response(project)
    if (url.endsWith('/session')) return response({ session_id: 'session' })
    if (url.endsWith('/config')) { if (method === 'PUT') { config = body; revision++ } return response(config, `"${revision}"`) }
    if (url.includes('/model-limits')) {
      if (method === 'PUT' && rejectLimits) return new Response(JSON.stringify({ error: { code: 'config_revision_conflict' } }), { status: 409 })
      if (method === 'PUT') { limits = { ...limits, ...body }; config.agent_config = 'files/agent-new.yaml'; revision++ }
      return response(url.includes('include_config=true') ? { config, limits } : limits, `"${revision}"`)
    }
    if (url.endsWith('/credentials')) return response({ database_sources: [], model: false })
    if (url.endsWith('/metadata')) return response({ tables: [], nodes: [], edges: [] })
    if (url.includes('/graph?')) return response({ nodes: [], edges: [] })
    if (url.endsWith('/runs')) {
      if (method === 'GET') return response({ runs: [] })
      runModels.push(structuredClone(config.agent_config ? limits : config.model!))
      return response({ run_id: 'new', project_id: 'p', status: 'completed', stage: 'finished', cleanup_status: 'completed' })
    }
    if (url.includes('/trace?')) return response({ events: [], total_events: 0, next_offset: null, status: 'not_recorded', active: false })
    throw new Error(`Unexpected ${method} ${url}`)
  }))
})
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); clearSessions(); sessionStorage.clear() })
describe('visible model limits', () => {
  it.each([false, true])('saves cumulative data budgets and working memory, then restores after reload (%s)', async separate => {
    if (separate) { delete config.model; config.agent_config = 'files/agent-original.yaml' }
    await mount()
    expect(root.querySelector<HTMLInputElement>('#max-returned-rows')!.value).toBe('400000')
    expect(root.querySelector<HTMLInputElement>('#max-returned-mib')!.value).toBe('64')
    expect(root.querySelector<HTMLInputElement>('#max-working-mib')!.value).toBe('32')
    await edit('max-returned-rows', '1200000')
    await edit('max-returned-mib', '512')
    await edit('max-working-mib', '1024')
    await edit('model-max-calls', '400')
    button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存'))
    expect(config).toMatchObject({ max_returned_rows: 1200000, max_returned_bytes: 536870912, max_working_bytes: 1073741824 })
    expect(separate ? limits.max_calls : config.model!.max_calls).toBe(400)
    app.unmount(); root.remove(); await mount()
    expect(root.querySelector<HTMLInputElement>('#max-returned-rows')!.value).toBe('1200000')
    expect(root.querySelector<HTMLInputElement>('#max-returned-mib')!.value).toBe('512')
    expect(root.querySelector<HTMLInputElement>('#max-working-mib')!.value).toBe('1024')
    // Saving only model settings must not restore old resource defaults.
    await edit('model-max-calls', '600'); button('保存配置').click()
    await vi.waitFor(() => expect(separate ? limits.max_calls : config.model!.max_calls).toBe(600))
    expect(config).toMatchObject({ max_returned_rows: 1200000, max_returned_bytes: 536870912, max_working_bytes: 1073741824 })
  })
  it('persists explicit unlimited return budgets and keeps byte-precise existing memory values', async () => {
    config.max_working_bytes = 1073741825
    await mount()
    for (const id of ['max-returned-rows-unlimited', 'max-returned-mib-unlimited']) {
      const input = root.querySelector<HTMLInputElement>(`#${id}`)!
      input.checked = true; input.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    }
    button('保存配置').click()
    await vi.waitFor(() => expect(config.max_returned_bytes).toBeNull())
    expect(config.max_returned_rows).toBeNull(); expect(config.max_working_bytes).toBe(1073741825)
    app.unmount(); root.remove(); await mount()
    expect(root.querySelector<HTMLInputElement>('#max-returned-mib-unlimited')!.checked).toBe(true)
    expect(root.querySelector<HTMLInputElement>('#max-returned-rows-unlimited')!.checked).toBe(true)
    expect(root.querySelector<HTMLInputElement>('#max-returned-mib')!.disabled).toBe(true)
    expect(Number(root.querySelector<HTMLInputElement>('#max-working-mib')!.value) * 1048576).toBe(1073741825)
  })
  it('merges resource controls with unrelated JSON fields and rejects conflicting resource changes', async () => {
    await mount()
    await edit('advanced-config', JSON.stringify({ ...config, max_run_seconds: 9000 }))
    await edit('max-returned-mib', '1024'); button('保存完整配置').click()
    await vi.waitFor(() => expect(config.max_returned_bytes).toBe(1073741824))
    expect(config.max_run_seconds).toBe(9000)
    await vi.waitFor(() => expect(button('保存完整配置').disabled).toBe(false))
    await edit('advanced-config', JSON.stringify({ ...config, max_returned_bytes: 268435456 }))
    await edit('max-returned-mib', '512'); button('保存完整配置').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('max_returned_bytes'))
    expect(config.max_returned_bytes).toBe(1073741824)
  })
  it.each([['max-returned-rows', ''], ['max-returned-rows', '1.5'], ['max-returned-mib', '0'],
    ['max-returned-mib', '0.0000001'], ['max-working-mib', '-1'], ['max-working-mib', '']])('rejects invalid resource control %s=%s', async (id, value) => {
    await mount(); await edit(id, value)
    expect(button('保存配置').disabled).toBe(true)
    expect(button('保存并开始抽取').disabled).toBe(true)
    expect(calls.some(call => call.method === 'PUT')).toBe(false)
  })

  it.each([false, true])('保存默认额度后刷新仍生效，无需展开 JSON (%s)', async separate => {
    config.max_run_seconds = 1800
    if (separate) { delete config.model; config.agent_config = 'files/agent-original.yaml' }
    await mount()
    expect(root.querySelector<HTMLDetailsElement>('.advanced-panel')!.open).toBe(false)
    button('使用默认额度').click(); await nextTick()
    button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存'))
    expect(separate ? limits : config.model).toMatchObject({ max_calls: 2000, max_prompt_tokens: 1048576,
      max_output_tokens: 65536, timeout_seconds: 800, context_window_tokens: null })
    expect(config.max_run_seconds).toBe(60000)
    app.unmount(); root.remove(); await mount()
    expect(root.textContent).toContain('1048576 tokens')
    expect(root.querySelector<HTMLInputElement>('#model-output-tokens')!.value).toBe('65536')
  })

  it.each([false, true])('persists query limit and construction target independently of model source (%s)', async separate => {
    if (separate) { delete config.model; config.agent_config = 'files/agent-original.yaml' }
    config.max_db_queries = 240
    await mount()
    const check = root.querySelector<HTMLInputElement>('#query-count-unlimited')!
    expect(check.checked).toBe(false)
    check.checked = true; check.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    const target = root.querySelector<HTMLSelectElement>('#construction-target')!
    target.value = 'structure'; target.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存'))
    expect(config.max_db_queries).toBeNull(); expect(config.construction_target).toBe('structure')
    app.unmount(); root.remove(); await mount()
    expect(root.querySelector<HTMLInputElement>('#query-count-unlimited')!.checked).toBe(true)
    expect(root.querySelector<HTMLInputElement>('#max-db-queries')!.disabled).toBe(true)
    expect(root.querySelector<HTMLSelectElement>('#construction-target')!.value).toBe('structure')
    const restored = root.querySelector<HTMLInputElement>('#query-count-unlimited')!
    restored.checked = false; restored.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    await edit('max-db-queries', '500'); button('保存完整配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('完整配置已保存'))
    expect(config.max_db_queries).toBe(500)
    app.unmount(); root.remove(); await mount()
    expect(root.querySelector<HTMLInputElement>('#query-count-unlimited')!.checked).toBe(false)
    expect(root.querySelector<HTMLInputElement>('#max-db-queries')!.value).toBe('500')
  })
  it('merges query controls with unrelated JSON edits and detects same-field conflicts', async () => {
    config.max_db_queries = 240
    await mount()
    await edit('advanced-config', JSON.stringify({ ...config, max_run_seconds: 9000 }))
    await edit('max-db-queries', '500'); button('保存完整配置').click()
    await vi.waitFor(() => expect(config.max_db_queries).toBe(500))
    expect(config.max_run_seconds).toBe(9000)
    await vi.waitFor(() => expect(button('保存完整配置').disabled).toBe(false))
    await edit('advanced-config', JSON.stringify({ ...config, max_db_queries: 800 }))
    await edit('max-db-queries', '1000'); button('保存完整配置').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('max_db_queries'))
    expect(config.max_db_queries).toBe(500)
  })
  it.each(['', '0', '-1', '1.5'])('rejects an invalid enabled query limit: %s', async invalid => {
    await mount()
    const check = root.querySelector<HTMLInputElement>('#query-count-unlimited')!
    check.checked = false; check.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    await edit('max-db-queries', invalid)
    expect(root.textContent).toContain('查询次数上限须为正整数')
    expect(button('保存配置').disabled).toBe(true)
    expect(button('保存并开始抽取').disabled).toBe(true)
    expect(calls.some(call => call.method === 'PUT')).toBe(false)
  })

  it('explicitly discards form changes even when JSON changed the same previewed field', async () => {
    await mount(); await edit('model-max-calls', '80')
    const draft = JSON.parse(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value)
    draft.model.max_calls = 120
    await edit('advanced-config', JSON.stringify(draft)); button('放弃表单修改').click()
    await vi.waitFor(() => expect(root.querySelector<HTMLInputElement>('#model-max-calls')!.value).toBe('240'))
    button('保存完整配置').click()
    await vi.waitFor(() => expect(config.model?.max_calls).toBe(120))
    expect(root.querySelector('.error-strip')).toBeNull()
  })
  it('keeps JSON in sync with model edits and accepts formatting without a false conflict', async () => {
    await mount(); await edit('model-name', 'new-model'); await edit('model-output-tokens', '65536')
    const draft = JSON.parse(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value)
    expect(draft.model).toMatchObject({ model: 'new-model', max_output_tokens: 65536 })
    await edit('advanced-config', JSON.stringify(draft))
    button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存'))
    expect(config.model).toMatchObject({ model: 'new-model', max_output_tokens: 65536 })
    expect(root.querySelector('.error-strip')).toBeNull()
  })
  it('does not block configuration on a pending or failed graph read', async () => {
    const original = vi.mocked(fetch).getMockImplementation()!
    let rejectGraph!: (reason: Error) => void
    vi.mocked(fetch).mockImplementation((input, init) => String(input).includes('/graph?') ? new Promise((_, reject) => { rejectGraph = reject }) : original(input, init))
    await mount(); await edit('model-output-tokens', '65536'); button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存'))
    rejectGraph(new Error('graph unavailable')); await nextTick(); await nextTick()
    expect(root.querySelector('.error-strip')).toBeNull()
    expect(root.querySelector<HTMLInputElement>('#model-output-tokens')!.value).toBe('65536')
  })
  it('does not report a committed config write as failed when credential status cannot refresh', async () => {
    await mount(); await edit('model-output-tokens', '65536')
    const original = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation((input, init) => String(input).endsWith('/credentials') ? Promise.reject(new Error('status unavailable')) : original(input, init))
    button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存'))
    expect(config.model?.max_output_tokens).toBe(65536)
    expect(root.querySelector('.error-strip')).toBeNull()
  })
  it('retains an unsaved key after failure and restores saved status in a new page session', async () => {
    let fail = true, saved = false
    const original = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation((input, init) => {
      if (String(input).endsWith('/credentials/model')) {
        if (fail) return Promise.reject(new Error('key save unavailable'))
        saved = true
        return Promise.resolve(response({ model: true, database_sources: [] }))
      }
      if (String(input).endsWith('/credentials')) return Promise.resolve(response({ model: saved, database_sources: [] }))
      return original(input, init)
    })
    await mount(); await edit('model-key', 'synthetic-key'); button('保存配置').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('API key 保存失败，输入已保留'))
    expect(root.querySelector<HTMLInputElement>('#model-key')!.value).toBe('synthetic-key')
    expect(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value).not.toContain('synthetic-key')
    fail = false; button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存'))
    expect(root.querySelector<HTMLInputElement>('#model-key')!.value).toBe('')
    app.unmount(); root.remove(); await mount()
    expect(root.textContent).toContain('已在本机保存密钥')
    expect(root.querySelector<HTMLInputElement>('#model-key')!.value).toBe('')
  })
  it('saves a model key against the revision and normalized binding returned by the config write', async () => {
    config.model = { ...config.model, base_url: 'https://a.example/v1', model: 'model-a' }
    const original = vi.mocked(fetch).getMockImplementation()!
    const credentialWrites: RequestInit[] = []
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (String(input).endsWith('/credentials/model')) {
        credentialWrites.push(init!)
        return response({ model: true, database_sources: [] })
      }
      if (String(input).endsWith('/config') && init?.method === 'PUT') {
        const result = await original(input, init)
        config.model!.base_url = 'https://normalized.example/v1'
        config.model!.model = 'normalized-model'
        return response(config, result.headers.get('ETag')!)
      }
      return original(input, init)
    })
    await mount(); await edit('model-key', 'synthetic-key'); button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存'))
    expect(credentialWrites).toHaveLength(1)
    expect(new Headers(credentialWrites[0]!.headers).get('If-Match')).toBe('"2"')
    expect(JSON.parse(String(credentialWrites[0]!.body))).toEqual({ api_key: 'synthetic-key', expected_model: { base_url: 'https://normalized.example/v1', model: 'normalized-model' } })
  })
  it.each(['config_revision_conflict', 'model_credential_binding_conflict'])('retains the key and offers reload after %s without retrying', async code => {
    const original = vi.mocked(fetch).getMockImplementation()!
    const credentialWrites: RequestInit[] = []
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (String(input).endsWith('/credentials/model')) {
        credentialWrites.push(init!)
        return new Response(JSON.stringify({ error: { code } }), { status: 409 })
      }
      return original(input, init)
    })
    await mount(); await edit('model-key', 'synthetic-key'); button('保存配置').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('API key 保存失败，输入已保留'))
    expect(root.querySelector<HTMLInputElement>('#model-key')!.value).toBe('synthetic-key')
    expect(button('重新加载配置')).toBeDefined()
    expect(credentialWrites).toHaveLength(1)
    expect(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value).not.toContain('synthetic-key')
    // Reload explicitly discards this draft. Do not carry A's retained key into B's form.
    config.model = { ...config.model, base_url: 'https://b.example/v1', model: 'model-b' }; revision++
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true))
    button('重新加载配置').click()
    await vi.waitFor(() => expect(root.querySelector<HTMLInputElement>('#model-url')!.value).toBe('https://b.example/v1'))
    expect(root.querySelector<HTMLInputElement>('#model-key')!.value).toBe('')
    expect(credentialWrites).toHaveLength(1)
  })
  it('clears a model key with the viewed revision and keeps saved status after a stale clear', async () => {
    const original = vi.mocked(fetch).getMockImplementation()!
    const credentialWrites: RequestInit[] = []
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (String(input).endsWith('/credentials')) return response({ model: true, database_sources: [] })
      if (String(input).endsWith('/credentials/model')) {
        credentialWrites.push(init!)
        return new Response(JSON.stringify({ error: { code: 'config_revision_conflict' } }), { status: 409 })
      }
      return original(input, init)
    })
    await mount(); button('清除密钥').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('配置已被其他页面修改'))
    expect(credentialWrites).toHaveLength(1)
    expect(credentialWrites[0]!.method).toBe('DELETE')
    expect(new Headers(credentialWrites[0]!.headers).get('If-Match')).toBe('"1"')
    expect(root.textContent).toContain('已在本机保存密钥')
    expect(button('重新加载配置')).toBeDefined()
  })
  it('shows a safe field diagnostic and retains invalid JSON values for correction', async () => {
    await mount()
    await edit('advanced-config', JSON.stringify({ ...config, model: { ...config.model, max_output_tokens: 'wrong' } }))
    const original = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation((input, init) => String(input).endsWith('/config') && init?.method === 'PUT' ? Promise.resolve(new Response(JSON.stringify({ error: { code: 'invalid_project_config', diagnostics: [{ path: 'model.max_output_tokens', type: 'int_parsing', hint: '请填写整数。' }] } }), { status: 400 })) : original(input, init))
    button('保存完整配置').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('model.max_output_tokens：请填写整数'))
    expect(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value).toContain('wrong')
  })
  it.each([false, true])('the complete-config button saves edited controls when JSON is unchanged (Agent file: %s)', async separate => {
    if (separate) { delete config.model; config.agent_config = 'files/agent-original.yaml' }
    await mount()
    await edit('model-input-tokens', '1048576'); await edit('model-output-tokens', '65536')
    button('保存完整配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('完整配置已保存'))
    expect(separate ? limits : config.model).toMatchObject({ max_prompt_tokens: 1048576, max_output_tokens: 65536 })
    app.unmount(); root.remove(); await mount()
    expect(root.querySelector<HTMLInputElement>('#model-input-tokens')!.value).toBe('1048576')
    expect(root.querySelector<HTMLInputElement>('#model-output-tokens')!.value).toBe('65536')
  })
  it('does not overwrite edited controls with a separately edited JSON draft', async () => {
    await mount()
    await edit('advanced-config', JSON.stringify({ ...config, model: { ...config.model, max_output_tokens: 300 }, goal: { purpose: 'JSON 中的新目标' } }, null, 2))
    await edit('model-output-tokens', '65536')
    button('保存完整配置').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('model.max_output_tokens'))
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
    expect(root.querySelector<HTMLInputElement>('#model-output-tokens')!.value).toBe('65536')
    expect(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value).toContain('JSON 中的新目标')
    button('放弃表单修改').click(); await nextTick(); button('保存完整配置').click()
    await vi.waitFor(() => expect(config.goal?.purpose).toBe('JSON 中的新目标'))
    expect(config.model?.max_output_tokens).toBe(300)
  })
  it.each([false, true])('persists the total window and shows the actual input cap (Agent file: %s)', async separate => {
    if (separate) { delete config.model; config.agent_config = 'files/agent-original.yaml' }
    await mount()
    await edit('model-input-tokens', '1048576'); await edit('model-output-tokens', '65536'); await edit('model-context-tokens', '131072')
    expect(root.querySelector('[data-testid="effective-input-tokens"]')?.textContent).toContain('65536 tokens')
    await edit('model-context-tokens', '')
    expect(root.querySelector('[data-testid="effective-input-tokens"]')?.textContent).toContain('1048576 tokens')
    button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存，下次启动立即生效'))
    expect((separate ? limits : config.model)?.context_window_tokens).toBeNull()
    app.unmount(); root.remove(); await mount()
    expect(root.querySelector<HTMLInputElement>('#model-context-tokens')!.value).toBe('')
    expect(root.querySelector('[data-testid="effective-input-tokens"]')?.textContent).toContain('1048576 tokens')
  })
  it('applies the saved Agent snapshot without a fallible follow-up config read', async () => {
    delete config.model; config.agent_config = 'files/agent-original.yaml'
    await mount(); await edit('model-output-tokens', '65536')
    const readCount = calls.filter(c => c.method === 'GET' && (c.url.endsWith('/config') || c.url.endsWith('/model-limits'))).length
    const original = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation((input, init) => {
      const url = String(input)
      if ((!init?.method || init.method === 'GET') && (url.endsWith('/config') || url.endsWith('/model-limits'))) return Promise.reject(new Error('read disconnected'))
      return original(input, init)
    })
    button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存，下次启动立即生效'))
    expect(calls.filter(c => c.method === 'GET' && (c.url.endsWith('/config') || c.url.endsWith('/model-limits')))).toHaveLength(readCount)
    expect(root.querySelector('.error-strip')).toBeNull()
    expect(root.querySelector<HTMLInputElement>('#model-output-tokens')!.value).toBe('65536')
    button('开始抽取').click()
    await vi.waitFor(() => expect(runModels).toHaveLength(1))
    expect(calls.find(c => c.method === 'POST' && c.url.endsWith('/runs'))!.headers.get('If-Match')).toBe('"2"')
    expect(runModels[0]!.max_output_tokens).toBe(65536)
  })
  it('saves token, timeout and whole-run model call controls', async () => {
    await mount()
    await edit('model-max-calls', '24'); await edit('model-input-tokens', '1048576'); await edit('model-output-tokens', '65536'); await edit('model-timeout-seconds', '300')
    button('保存配置').click()
    await vi.waitFor(() => expect(calls.some(c => c.method === 'PUT')).toBe(true))
    expect(config.model).toMatchObject({ max_calls: 24, max_prompt_tokens: 1048576, max_output_tokens: 65536, timeout_seconds: 300, max_tokens: null, max_prompt_bytes: 131072 })
    await vi.waitFor(() => expect(button('保存配置').disabled).toBe(false))
    expect(root.querySelector<HTMLInputElement>('#model-input-tokens')!.value).toBe('1048576')
  })
  it('does not expose obsolete per-table or byte-budget controls', async () => {
    await mount()
    expect(root.querySelector('#model-discovery-steps')).toBeNull()
    expect(root.querySelector('#model-prompt-bytes')).toBeNull()
    expect(root.textContent).not.toContain('DeepSeek Harness')
    expect(root.querySelector('#model-working-context-tokens')).toBeNull()
    await edit('model-input-tokens', '')
    button('保存配置').click()
    await vi.waitFor(() => expect(config.model?.max_prompt_tokens).toBeNull())
    expect(config.model?.max_prompt_bytes).toBe(131072)
  })
  it('keeps an internal HTTP service opt-in explicit and saves it with model settings', async () => {
    await mount(); await edit('model-url', 'http://models.internal/v1')
    const checkbox = root.querySelector<HTMLInputElement>('#model-allow-http')!
    expect(checkbox).not.toBeNull()
    expect(checkbox.checked).toBe(false)
    checkbox.checked = true; checkbox.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    button('保存配置').click()
    await vi.waitFor(() => expect(config.model?.allow_insecure_http).toBe(true))
    expect(root.textContent).not.toContain('Harness')
  })
  it('requires explicit activation of native persistence for an old trace-disabled project', async () => {
    config.record_trace = false
    root = document.createElement('div'); document.body.append(root); app = createApp(App); app.mount(root)
    await vi.waitFor(() => expect(button('模型配置')).toBeDefined())
    button('模型配置').click(); await nextTick()
    expect(button('保存配置').disabled).toBe(true)
    expect(root.textContent).toContain('运行需要保存轨迹')
    button('启用运行轨迹').click(); await nextTick()
    button('保存配置').click()
    await vi.waitFor(() => expect(config.record_trace).toBe(true))
    expect(root.textContent).toContain('运行后需手动清理')
  })
  it('reports the default Harness capacity and rejects disabling persistence in JSON', async () => {
    delete config.model!.max_prompt_tokens; delete config.model!.context_window_tokens
    await mount()
    expect(root.querySelector('[data-testid="effective-input-tokens"]')?.textContent).toContain('1048576 tokens')
    await edit('advanced-config', JSON.stringify({ ...config, record_trace: false }))
    button('保存完整配置').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('record_trace 设为 true'))
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
  })
  it.each([false, true])('persists the whole-run deadline independently of the Agent file (Agent file: %s)', async separate => {
    if (separate) { delete config.model; config.agent_config = 'files/agent-original.yaml' }
    await mount(); await edit('run-seconds', '3600')
    button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存，下次启动立即生效'))
    expect(config.max_run_seconds).toBe(3600)
    app.unmount(); root.remove(); await mount()
    expect(root.querySelector<HTMLInputElement>('#run-seconds')!.value).toBe('3600')
  })
  it('rejects an invalid whole-run deadline without saving or starting', async () => {
    await mount(); await edit('run-seconds', '0')
    expect(root.textContent).toContain('整次运行时限须大于 0')
    expect(button('保存配置').disabled).toBe(true)
    expect(button('保存并开始抽取').disabled).toBe(true)
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
  })
  it('blocks invalid limits and oversized output reservation before a write', async () => {
    await mount(); await edit('model-timeout-seconds', '0')
    expect(button('保存配置').disabled).toBe(true)
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
    await edit('model-timeout-seconds', '300'); await edit('model-output-tokens', '2000000')
    expect(root.textContent).toContain('必须小于模型的总上下文窗口')
    expect(button('保存配置').disabled).toBe(true)
  })
  it('updates limits of a separate Agent file through its revision-checked endpoint', async () => {
    delete config.model; config.agent_config = 'files/agent-original.yaml'
    await mount(); await edit('model-max-calls', '24'); await edit('model-input-tokens', '1048576'); await edit('model-output-tokens', '65536'); await edit('model-timeout-seconds', '300')
    button('保存配置').click()
    await vi.waitFor(() => expect(config.agent_config).toBe('files/agent-new.yaml'))
    const write = calls.find(c => c.method === 'PUT')!
    expect(write.url).toBe('/api/projects/p/model-limits?include_config=true')
    expect(write.headers.get('If-Match')).toBe('"1"')
    expect(write.body).toEqual({ max_calls: 24, max_prompt_tokens: 1048576, max_output_tokens: 65536, timeout_seconds: 300, context_window_tokens: 2000000 })
    expect(config.model).toBeUndefined()
  })
  it('persists changed separate-file limits before starting a run', async () => {
    delete config.model; config.agent_config = 'files/agent-original.yaml'
    await mount(); await edit('model-timeout-seconds', '500')
    button('保存并开始抽取').click()
    await vi.waitFor(() => expect(calls.some(c => c.method === 'POST' && c.url.endsWith('/runs'))).toBe(true))
    const writes = calls.filter(c => c.method === 'PUT' || c.method === 'POST' && c.url.endsWith('/runs'))
    expect(writes.map(c => c.url)).toEqual(['/api/projects/p/model-limits?include_config=true', '/api/projects/p/runs'])
    expect(writes[1]!.headers.get('If-Match')).toBe('"2"')
  })
  it.each([false, true])('saved controls apply to the next run and survive reload (Agent file: %s)', async separate => {
    if (separate) { delete config.model; config.agent_config = 'files/agent-original.yaml' }
    await mount()
    await edit('model-max-calls', '20'); await edit('model-input-tokens', '1048576')
    await edit('model-output-tokens', '65536'); await edit('model-timeout-seconds', '420')
    button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存，下次启动立即生效'))
    await vi.waitFor(() => expect(button('开始抽取').disabled).toBe(false))
    button('开始抽取').click()
    await vi.waitFor(() => expect(runModels).toHaveLength(1))
    expect(runModels[0]).toMatchObject({ max_calls: 20, max_prompt_tokens: 1048576, max_output_tokens: 65536, timeout_seconds: 420 })
    const start = calls.find(c => c.method === 'POST' && c.url.endsWith('/runs'))!
    expect(start.headers.get('If-Match')).toBe('"2"')
    expect(calls.filter(c => c.method === 'PUT')).toHaveLength(1)
    app.unmount(); root.remove(); await mount()
    expect(root.querySelector<HTMLInputElement>('#model-max-calls')!.value).toBe('20')
    expect(root.querySelector<HTMLInputElement>('#model-output-tokens')!.value).toBe('65536')
    expect(root.querySelector<HTMLInputElement>('#model-timeout-seconds')!.value).toBe('420')
  })
  it('saves Markdown target with separate-file controls before starting', async () => {
    delete config.model; config.agent_config = 'files/agent-original.yaml'
    await mount(); await edit('model-max-calls', '24')
    button('抽取目标').click(); await nextTick(); await edit('purpose', '# 保存后的目标\n发现对象关系并保留来源。')
    button('模型配置').click(); await nextTick(); button('保存配置').click()
    await vi.waitFor(() => expect(root.textContent).toContain('模型配置已保存，下次启动立即生效'))
    await vi.waitFor(() => expect(button('开始抽取').disabled).toBe(false))
    const writes = calls.filter(c => c.method === 'PUT')
    expect(writes.map(c => c.url)).toEqual(['/api/projects/p/config', '/api/projects/p/model-limits?include_config=true'])
    expect(writes[1]!.headers.get('If-Match')).toBe('"2"')
    expect(writes[1]!.body).toMatchObject({ max_calls: 24 })
    expect(config.goal_markdown).toBe('# 保存后的目标\n发现对象关系并保留来源。')
    button('开始抽取').click(); await vi.waitFor(() => expect(runModels).toHaveLength(1))
    expect(runModels[0]!.max_calls).toBe(24)
    expect(calls.find(c => c.method === 'POST' && c.url.endsWith('/runs'))!.headers.get('If-Match')).toBe('"3"')
  })
  it('retains unsaved controls and does not start after a configuration write fails', async () => {
    delete config.model; config.agent_config = 'files/agent-original.yaml'
    await mount(); await edit('model-max-calls', '24'); rejectLimits = true
    button('保存并开始抽取').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('本地草稿已保留'))
    expect(runModels).toHaveLength(0)
    expect(limits.max_calls).toBe(240)
    expect(root.querySelector<HTMLInputElement>('#model-max-calls')!.value).toBe('24')
  })
  it.each(['-1', '1.5'])('rejects invalid call budgets %s before save or run', async value => {
    await mount(); await edit('model-max-calls', value)
    expect(root.textContent).toContain('模型调用总额度须为非负整数')
    expect(button('保存配置').disabled).toBe(true)
    expect(button('保存并开始抽取').disabled).toBe(true)
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
  })
  it('shows the current default for configurations without an explicit call budget', async () => {
    delete config.model!.max_calls
    await mount()
    expect(root.querySelector<HTMLInputElement>('#model-max-calls')!.value).toBe('2000')
    expect(button('保存配置').disabled).toBe(false)
  })
  it('keeps the latest Agent load locked when a previous file request returns late', async () => {
    delete config.model; config.agent_config = 'files/agent-original.yaml'
    const pending: Array<(value: Response) => void> = []
    const original = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation((input, init) => {
      if (String(input).endsWith('/model-limits') && (!init?.method || init.method === 'GET')) return new Promise(resolve => pending.push(resolve))
      return original(input, init)
    })
    root = document.createElement('div'); document.body.append(root); app = createApp(App); app.mount(root)
    await vi.waitFor(() => expect(pending).toHaveLength(1))
    button('模型配置').click(); await nextTick()
    await edit('advanced-config', JSON.stringify({ ...config, agent_config: 'files/agent-second.yaml' }, null, 2))
    button('保存完整配置').click()
    await vi.waitFor(() => expect(pending).toHaveLength(2))
    pending[0]!(response({ ...limits, max_calls: 8 }))
    await nextTick(); await nextTick()
    expect(root.textContent).toContain('正在读取模型额度')
    expect(button('保存配置').disabled).toBe(true)
    expect(button('开始抽取').disabled).toBe(true)
    pending[1]!(response({ ...limits, max_calls: 24 }))
    await vi.waitFor(() => expect(button('保存配置').disabled).toBe(false))
    expect(root.querySelector<HTMLInputElement>('#model-max-calls')!.value).toBe('24')
  })
  it('supports positive fractional timeout but only integral token and byte limits', () => {
    expect(modelLimitError({ max_prompt_tokens: null, timeout_seconds: .5 })).toBe('')
    expect(modelLimitError({ max_output_tokens: .5 })).not.toBe('')
    expect(modelLimitError({ max_calls: .5 })).not.toBe('')
    expect(modelLimitError({ max_output_tokens: null })).not.toBe('')
    expect(modelLimitError({ max_calls: 1 })).toBe('')
    expect(modelLimitError({ max_calls: 32 })).toBe('')
    expect(modelLimitError({ context_window_tokens: null })).toBe('')
    expect(modelLimitError({ context_window_tokens: .5 })).not.toBe('')
  })
})
