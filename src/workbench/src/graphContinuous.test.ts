// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
let app: App, root: HTMLDivElement
const emptyDetails = { facts: [], supports: [], rules: [], next_offset: null }
const initial = { generation: 'g1', nodes: [{ id: 'start', label: '起点' }], edges: [], total_nodes: 1400, page_size: 240, next_offset: 240, kind_counts: { Metric: 1400 } }
function mount(graph = initial) { root = document.createElement('div'); document.body.append(root); app = createApp(GraphCanvas, { graph, projectId: 'p', layer: 'business' }); app.mount(root) }
function button(text: string) { return [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === text)! }
function search(text: string) { const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!; input.value = text; input.dispatchEvent(new Event('input', { bubbles: true })) }
beforeEach(() => { api.request.mockReset(); vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }); vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null) })
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
describe('continuous graph interaction', () => {
  it('expands neighbours across batches while preserving existing nodes and edges', async () => {
    let read = 0
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? emptyDetails : {
      ...initial, nodes: [{ id: 'start', label: '起点' }, { id: ++read === 1 ? 'beyond' : 'last', label: '邻居' }],
      edges: [{ id: `e${read}`, source: 'start', target: read === 1 ? 'beyond' : 'last' }], next_offset: read === 1 ? 240 : null,
    })
    mount(); root.querySelector('.graph-node')!.dispatchEvent(new MouseEvent('click', { bubbles: true })); await nextTick()
    const position = root.querySelector('.graph-node')!.getAttribute('transform')
    button('展开相邻节点').click()
    await vi.waitFor(() => expect(root.querySelectorAll('.graph-node')).toHaveLength(2))
    expect(root.querySelector('.graph-node')!.getAttribute('transform')).toBe(position)
    button('继续展开关联').click()
    await vi.waitFor(() => expect(root.querySelectorAll('.graph-node')).toHaveLength(3))
    expect(root.querySelectorAll('.graph-edge')).toHaveLength(2)
    expect(api.request.mock.calls.filter(c => c[0].includes('focus=start')).at(-1)?.[0]).toContain('offset=240')
    expect(button('继续展开关联').disabled).toBe(true)
  })
  it('automatically loads the rest of a small graph and switches large scenes to Canvas', async () => {
    const nodes = Array.from({ length: 450 }, (_, i) => ({ id: `n${i}`, label: `指标${i}` }))
    api.request.mockResolvedValue({ ...initial, nodes, edges: [], total_nodes: 450, next_offset: null })
    mount({ ...initial, nodes: nodes.slice(0, 240), total_nodes: 450 })
    await vi.waitFor(() => expect(root.querySelector('canvas')).not.toBeNull())
    expect(root.querySelectorAll('.graph-node')).toHaveLength(0)
    expect(root.querySelector('canvas')!.getAttribute('aria-label')).toContain('450 个节点')
    expect(root.textContent).toContain('画布已加载 450')
  })
  it('ignores and cancels stale search requests', async () => {
    let oldResolve: (value: unknown) => void = () => {}
    api.request.mockImplementation((path: string) => {
      if (path.includes('/details')) return Promise.resolve(emptyDetails)
      if (path.includes('q=old')) return new Promise(resolve => { oldResolve = resolve })
      return Promise.resolve({ ...initial, nodes: [{ id: 'new', label: '最新' }], next_offset: null })
    })
    mount(); search('old')
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledTimes(1))
    const signal = api.request.mock.calls[0][1].signal as AbortSignal
    search('new')
    await vi.waitFor(() => expect(root.textContent).toContain('最新'))
    expect(signal.aborted).toBe(true)
    oldResolve({ ...initial, nodes: [{ id: 'stale', label: '过期' }] }); await nextTick(); await nextTick()
    expect(root.textContent).not.toContain('过期')
    expect(root.querySelectorAll('.graph-node')).toHaveLength(2)
  })
})
