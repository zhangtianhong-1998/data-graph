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

describe('background graph request isolation', () => {
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
