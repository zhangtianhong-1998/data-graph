// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App as VueApp } from 'vue'
import App from './App.vue'
import { clearSessions } from './api'

vi.mock('./GraphCanvas.vue', () => ({ default: {
  props: ['graph', 'loading', 'includeCandidates'],
  template: '<section data-testid="graph-view"><span data-testid="graph-loading">{{ loading }}</span><span data-testid="candidate-mode">{{ includeCandidates }}</span><span v-for="node in graph.nodes" :key="node.id" class="test-node">{{ node.label }}</span></section>',
} }))
vi.mock('./IdentityCandidates.vue', () => ({ default: { template: '<div />' } }))
vi.mock('./DiscoveryCandidates.vue', () => ({ default: { template: '<div />' } }))
let app: VueApp, root: HTMLDivElement
let resolveInitial!: (value: Response) => void, rejectInitial!: (reason: Error) => void
let graphCalls: string[]
const project = { project_id: 'p', name: '图请求顺序', files: [], created_at: '', updated_at: '' }
const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200 })
function button(label: string) { return [...root.querySelectorAll('button')].find(item => item.textContent?.trim() === label)! }

beforeEach(() => {
  clearSessions(); sessionStorage.clear(); graphCalls = []
  vi.stubGlobal('fetch', vi.fn((input: string) => {
    const url = String(input)
    if (url.includes('/graph?')) {
      graphCalls.push(url)
      if (graphCalls.length === 1) return new Promise<Response>((resolve, reject) => { resolveInitial = resolve; rejectInitial = reject })
      return Promise.resolve(response({ nodes: [{ id: 'candidate', label: '新候选图', status: 'candidate' }], edges: [] }))
    }
    if (url === '/api/projects') return Promise.resolve(response({ projects: [project] }))
    if (url === '/api/projects/p') return Promise.resolve(response(project))
    if (url.endsWith('/session')) return Promise.resolve(response({ session_id: 'session' }))
    if (url.endsWith('/config')) return Promise.resolve(response({ model: { mode: 'disabled' }, goal_markdown: '发现关系', sources: [] }))
    if (url.endsWith('/metadata')) return Promise.resolve(response({ tables: [], nodes: [], edges: [] }))
    if (url.endsWith('/runs')) return Promise.resolve(response({ runs: [] }))
    if (url.endsWith('/credentials')) return Promise.resolve(response({ database_sources: [], model: false }))
    throw new Error(`Unexpected ${url}`)
  }))
})
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); clearSessions(); sessionStorage.clear() })

async function switchToCandidates() {
  root = document.createElement('div'); document.body.append(root); app = createApp(App); app.mount(root)
  await vi.waitFor(() => expect(button('运行与结果')).toBeDefined())
  button('运行与结果').click(); await nextTick(); button('业务图谱').click(); await nextTick()
  expect(root.querySelector('[data-testid="graph-loading"]')?.textContent).toBe('true')
  const toggle = root.querySelector<HTMLInputElement>('input[type="checkbox"]')!
  expect(toggle.disabled).toBe(false)
  toggle.checked = true; toggle.dispatchEvent(new Event('change', { bubbles: true }))
  await vi.waitFor(() => expect(root.querySelector('.test-node')?.textContent).toBe('新候选图'))
  await vi.waitFor(() => expect(root.querySelector('[data-testid="graph-loading"]')?.textContent).toBe('false'))
  expect(graphCalls).toHaveLength(2)
  expect(graphCalls[0]).toContain('include_candidates=false')
  expect(graphCalls[1]).toContain('include_candidates=true')
}

async function openBusinessGraph() {
  root = document.createElement('div'); document.body.append(root); app = createApp(App); app.mount(root)
  await vi.waitFor(() => expect(button('运行与结果')).toBeDefined())
  button('运行与结果').click(); await nextTick(); button('业务图谱').click(); await nextTick()
}

function graphResponse(label = '已发布业务节点') {
  return response({ nodes: [{ id: 'published', label }], edges: [] })
}

function graphFetch(implementation: (url: string, init?: RequestInit) => Promise<Response>) {
  const original = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation((input, init) => String(input).includes('/graph?') ? implementation(String(input), init) : original(input, init))
}

describe('background graph request isolation', () => {
  it('recovers an initial 503 before batch loading can start', async () => {
    let attempts = 0
    graphFetch(async () => ++attempts === 1 ? new Response('', { status: 503 }) : graphResponse())
    await openBusinessGraph()
    await vi.waitFor(() => expect(root.querySelector('.test-node')?.textContent).toBe('已发布业务节点'))
    expect(attempts).toBe(2)
    expect(root.textContent).not.toContain('图谱读取失败')
  })

  it('limits initial failures and keeps a manual initial-read retry instead of an empty success', async () => {
    let fail = true, attempts = 0
    graphFetch(async () => { attempts++; if (fail) throw new TypeError('network unavailable'); return graphResponse() })
    await openBusinessGraph()
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toContain('图谱读取失败'))
    expect(attempts).toBe(3)
    expect(root.querySelector('[data-testid="graph-view"]')).toBeNull()
    fail = false; button('重试读取图谱').click()
    await vi.waitFor(() => expect(root.querySelector('.test-node')?.textContent).toBe('已发布业务节点'))
    expect(attempts).toBe(4)
    expect(root.querySelector('[role="alert"]')).toBeNull()
  })

  it('does not retry initial access failures', async () => {
    let attempts = 0
    graphFetch(async () => { attempts++; return new Response(JSON.stringify({ error: { code: 'graph_access_context_changed' } }), { status: 403 }) })
    await openBusinessGraph()
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toContain('图谱读取失败'))
    await new Promise(resolve => setTimeout(resolve, 350))
    expect(attempts).toBe(1)
  })

  it('keeps a successful business refresh when metadata is unavailable', async () => {
    let metadataFails = false, graphVersion = '原图'
    graphFetch(async () => graphResponse(graphVersion))
    const original = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation((input, init) => metadataFails && String(input).endsWith('/metadata')
      ? Promise.resolve(new Response(JSON.stringify({ error: { code: 'metadata_unavailable' } }), { status: 503 })) : original(input, init))
    await openBusinessGraph()
    await vi.waitFor(() => expect(root.querySelector('.test-node')?.textContent).toBe('原图'))
    metadataFails = true; graphVersion = '新图'; button('刷新图谱').click()
    await vi.waitFor(() => expect(root.querySelector('.test-node')?.textContent).toBe('新图'))
    await vi.waitFor(() => expect(root.querySelector('.error-strip')).not.toBeNull())
    expect(root.querySelector('[role="alert"]')?.textContent || '').not.toContain('图谱读取失败')
  })

  it('cancels an old project initial read and never retries or displays its late reply', async () => {
    const other = { ...project, project_id: 'q', name: '另一个项目' }
    const original = vi.mocked(fetch).getMockImplementation()!
    let oldSignal: AbortSignal | null | undefined
    vi.mocked(fetch).mockImplementation((input, init) => {
      const url = String(input)
      if (url === '/api/projects') return Promise.resolve(response({ projects: [project, other] }))
      if (url === '/api/projects/q') return Promise.resolve(response(other))
      if (url.startsWith('/api/projects/q/graph?')) return Promise.resolve(graphResponse('另一项目图'))
      if (url.startsWith('/api/projects/p/graph?')) oldSignal = init?.signal
      return original(input, init)
    })
    await openBusinessGraph()
    ;[...root.querySelectorAll<HTMLButtonElement>('.project')].find(item => item.textContent?.includes(other.name))!.click()
    await vi.waitFor(() => expect(root.querySelector('.test-node')?.textContent).toBe('另一项目图'))
    expect(oldSignal?.aborted).toBe(true)
    resolveInitial(graphResponse('迟到旧项目图'))
    await new Promise(resolve => setTimeout(resolve, 350))
    expect(root.querySelector('.test-node')?.textContent).toBe('另一项目图')
    expect(graphCalls).toHaveLength(1)
  })

  it('keeps the newer candidate graph visible when the initial request completes late', async () => {
    await switchToCandidates()
    resolveInitial(response({ nodes: [{ id: 'old', label: '旧普通图' }], edges: [] }))
    await nextTick(); await nextTick(); await new Promise(resolve => setTimeout(resolve, 0))
    expect(root.querySelector('.test-node')?.textContent).toBe('新候选图')
    expect(root.querySelector('[data-testid="candidate-mode"]')?.textContent).toBe('true')
    expect(root.querySelector('[data-testid="graph-loading"]')?.textContent).toBe('false')
    expect(root.textContent).not.toContain('旧普通图')
  })

  it('ignores a late initial failure after a successful candidate refresh', async () => {
    await switchToCandidates()
    rejectInitial(new Error('old graph request failed'))
    await nextTick(); await nextTick(); await new Promise(resolve => setTimeout(resolve, 0))
    expect(root.querySelector('.test-node')?.textContent).toBe('新候选图')
    expect(root.querySelector('[data-testid="graph-loading"]')?.textContent).toBe('false')
    expect(root.textContent).not.toContain('图谱读取失败')
    expect(root.querySelector('.error-strip')).toBeNull()
  })
})
