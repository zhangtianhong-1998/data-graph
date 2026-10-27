// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App as VueApp } from 'vue'
import App from './App.vue'
import { clearSessions } from './api'
import type { RunConfig } from './types'

let app: VueApp | undefined, root: HTMLDivElement, config: RunConfig, revision: number, conflict: boolean
const requests: Array<{ url: string; method: string; body?: string; headers: Headers }> = []
const project = { project_id: 'p', name: 'draft project', files: [], created_at: '2026-10-26', updated_at: '2026-10-26' }
const response = (value: unknown, status = 200, etag?: string) => new Response(JSON.stringify(value), { status, headers: etag ? { ETag: etag } : {} })
function button(label: string) { const el = [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === label); if (!el) throw new Error(`Missing ${label}`); return el }
async function edit(selector: string, text: string) { const el = root.querySelector<HTMLTextAreaElement>(selector)!; el.value = text; el.dispatchEvent(new Event('input', { bubbles: true })); await nextTick() }
async function ready() { root = document.createElement('div'); document.body.append(root); app = createApp(App); app.mount(root); await vi.waitFor(() => expect(root.querySelector('.page-empty')).toBeNull()) }
async function editAdvanced(value: unknown) { button('Agent 与模型').click(); await nextTick(); await edit('#advanced-config', typeof value === 'string' ? value : JSON.stringify(value, null, 2)) }
const writes = () => requests.filter(r => r.method !== 'GET' && !r.url.endsWith('/session'))
beforeEach(() => {
  clearSessions(); sessionStorage.clear(); requests.length = 0; revision = 5; conflict = false
  config = { goal: { purpose: '原目标', entity_kinds: [], relation_kinds: [] }, scopes: ['public'], sources: [{ id: 'data', kind: 'csv_file', path: 'files/data.csv' }], model: { mode: 'disabled' } }
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const method = options.method || 'GET'; const headers = new Headers(options.headers); const body = typeof options.body === 'string' ? options.body : undefined
    requests.push({ url, method, body, headers })
    if (url === '/api/projects') return response({ projects: [project] })
    if (url === '/api/projects/p') return response(project)
    if (url.endsWith('/session')) return response({ session_id: 'test' })
    if (url.endsWith('/config')) {
      if (method === 'PUT') {
        if (conflict || headers.get('If-Match') !== `"${revision}"`) return response({ error: { code: 'config_revision_conflict' } }, 409)
        config = JSON.parse(body!); revision++
      }
      return response(config, 200, `"${revision}"`)
    }
    if (url.endsWith('/credentials')) return response({ database_sources: [], model: false })
    if (url.endsWith('/metadata') || url.endsWith('/metadata/refresh')) return response({ nodes: [], edges: [], tables: [] })
    if (url.includes('/graph/identity-candidates')) return response({ candidates: [], decisions: [], revision: 0, total_candidates: 0, total_decisions: 0 })
    if (url.includes('/graph?')) return response({ nodes: [], edges: [] })
    if (url.endsWith('/runs')) return method === 'GET' ? response({ runs: [] }) : response({ run_id: 'run_test', project_id: 'p', config_revision: revision, status: 'completed', stage: 'finished', cleanup_status: 'completed' }, 202)
    if (url.includes('/database/')) return response({ saved: false, revision: 0 })
    throw new Error(`Unexpected ${method} ${url}`)
  }))
})
afterEach(() => { app?.unmount(); root?.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); clearSessions(); sessionStorage.clear() })

describe('完整配置与修订', () => {
  it('运行时指导按位置保存，刷新后恢复 Markdown 原文和阶段', async () => {
    await ready(); button('抽取目标').click(); await nextTick(); button('添加指导').click(); await nextTick()
    await edit('#guidance-text-0', '# 复核要求\n保留未决并核对反证。')
    const select = root.querySelector<HTMLSelectElement>('#guidance-injection-0')!
    select.value = 'semantic_review'; select.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    button('保存目标').click(); await vi.waitFor(() => expect(writes()).toHaveLength(1))
    expect(config.goal!.runtime_guidance).toEqual([{ text: '# 复核要求\n保留未决并核对反证。', injection: 'semantic_review', stages: [] }])
    await vi.waitFor(() => expect(button('保存目标').disabled).toBe(false))
    expect(root.querySelector<HTMLTextAreaElement>('#guidance-text-0')!.value).toBe('# 复核要求\n保留未决并核对反证。')
    expect(root.querySelector<HTMLSelectElement>('#guidance-injection-0')!.value).toBe('semantic_review')
  })

  it('保存高级 JSON 后按新 ETag 启动，保留 goal 与 scopes', async () => {
    await ready(); const draft = { ...config, goal: { ...config.goal, purpose: '对象关系新目标' }, scopes: ['public', 'approved'] }
    await editAdvanced(draft); button('保存并开始抽取').click()
    await vi.waitFor(() => expect(writes().map(r => r.url)).toEqual(['/api/projects/p/config', '/api/projects/p/runs']))
    expect(JSON.parse(writes()[0]!.body!)).toEqual(draft)
    expect(writes()[0]!.headers.get('If-Match')).toBe('"5"'); expect(writes()[1]!.headers.get('If-Match')).toBe('"6"')
    expect(config.scopes).toEqual(['public', 'approved'])
  })
  it.each(['刷新元数据', '＋ 连接'])('%s 前保存高级草稿', async label => {
    await ready(); const draft = { ...config, goal: { ...config.goal, purpose: '新的完整配置' } }
    await editAdvanced(draft); button('数据接入').click(); await nextTick(); button(label).click()
    await vi.waitFor(() => expect(writes()[0]?.url).toBe('/api/projects/p/config'))
    expect(JSON.parse(writes()[0]!.body!)).toEqual(draft)
    await vi.waitFor(() => expect(button('删除项目').disabled).toBe(false))
  })
  it('无效 JSON 启动、上传、元数据刷新均不丢稿或请求写入', async () => {
    await ready(); await editAdvanced('{broken draft'); button('保存并开始抽取').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('JSON 格式不正确'))
    expect(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value).toBe('{broken draft')
    button('数据接入').click(); await nextTick(); button('刷新元数据').click()
    await vi.waitFor(() => expect(button('删除项目').disabled).toBe(false))
    const input = root.querySelector<HTMLInputElement>('input[accept=".xlsx,.csv"]')!
    Object.defineProperty(input, 'files', { value: [new File(['a\nx'], 'table.csv', { type: 'text/csv' })] })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await vi.waitFor(() => expect(button('删除项目').disabled).toBe(false))
    button('Agent 与模型').click(); await nextTick()
    expect(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value).toBe('{broken draft'); expect(writes()).toEqual([])
  })
  it('再次点击当前项目保留完整配置草稿', async () => {
    await ready(); await editAdvanced('{unsaved draft')
    const reads = requests.filter(r => r.url.endsWith('/config')).length
    root.querySelector<HTMLButtonElement>('.project.active')!.click(); await nextTick()
    expect(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value).toBe('{unsaved draft')
    expect(requests.filter(r => r.url.endsWith('/config'))).toHaveLength(reads)
  })
  it('配置冲突保留完整草稿且不启动、不自动重试', async () => {
    await ready(); const draft = JSON.stringify({ ...config, scopes: ['public', 'draft'] }, null, 2)
    await editAdvanced(draft); conflict = true; button('保存并开始抽取').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('本地草稿已保留'))
    expect(root.querySelector<HTMLTextAreaElement>('#advanced-config')!.value).toBe(draft)
    expect(writes()).toHaveLength(1); expect(button('重新加载配置')).toBeDefined()
  })
  it('同时修改普通表单和 JSON 时让用户选择，不隐式覆盖任一份', async () => {
    await ready(); button('抽取目标').click(); await nextTick(); await edit('#purpose', '普通表单草稿')
    await editAdvanced({ ...config, scopes: ['public', 'draft'] }); button('保存并开始抽取').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('表单和完整 JSON 都有修改'))
    expect(writes()).toEqual([]); button('抽取目标').click(); await nextTick()
    expect(root.querySelector<HTMLTextAreaElement>('#purpose')!.value).toBe('普通表单草稿')
  })
  it('旧项目保存保留指定类型，切换自动后不再写入指标模板', async () => {
    config.goal!.entity_kinds = ['Person']; config.goal!.relation_kinds = ['knows']
    await ready(); button('抽取目标').click(); await nextTick(); await edit('#purpose', '人员关联')
    expect(root.querySelector<HTMLSelectElement>('#vocabulary-mode')!.value).toBe('specified')
    button('保存目标').click(); await vi.waitFor(() => expect(writes()).toHaveLength(1))
    expect(config.goal!.entity_kinds).toEqual(['Person']); expect(config.goal!.relation_kinds).toEqual(['knows'])
    await vi.waitFor(() => expect(button('保存目标').disabled).toBe(false))
    const select = root.querySelector<HTMLSelectElement>('#vocabulary-mode')!; select.value = 'automatic'; select.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    button('保存目标').click(); await vi.waitFor(() => expect(writes()).toHaveLength(2))
    expect(config.goal!.entity_kinds).toEqual([]); expect(config.goal!.relation_kinds).toEqual([])
  })
})
