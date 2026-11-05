// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import type { App } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import { ApiError } from './api'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
let app: App, root: HTMLDivElement
const nodes = Array.from({ length: 5001 }, (_, i) => ({ id: `n${i}`, label: `节点${i}` }))
const edges = Array.from({ length: 5000 }, (_, i) => ({ id: `e${i}`, source: `n${i}`, target: `n${i + 1}` }))
const initial = { complete_batches: true, generation: 'initial', nodes: nodes.slice(0, 1000), edges: [], total_nodes: 5001, total_edges: 5000, page_size: 1000, next_offset: 1000, truncated: true }
function page(path: string) {
  const url = new URL(path, 'http://local'), part = url.searchParams.get('part')!, offset = Number(url.searchParams.get('offset'))
  const values = (part === 'nodes' ? nodes : edges).slice(offset, offset + 1000), total = part === 'nodes' ? nodes.length : edges.length
  return { ...initial, generation: 'complete', part, offset, next_offset: offset + values.length < total ? offset + values.length : null, nodes: part === 'nodes' ? values : [], edges: part === 'edges' ? values : [], matched_nodes: nodes.length, matched_edges: edges.length }
}
function mount() { root = document.createElement('div'); document.body.append(root); const props = reactive({ graph: initial, projectId: 'p', layer: 'business', configRevision: 1 }); app = createApp({ render: () => h(GraphCanvas, props) }); app.mount(root); return props }
function button(text: string) { return [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === text)! }
beforeEach(() => { api.request.mockReset(); vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }); vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null) })
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
describe('full graph canvas', () => {
  it('automatically loads beyond 4000 and shows a complete graph with cross-page edges', async () => {
    api.request.mockImplementation(async path => path.includes('/details') ? { facts: [], supports: [], rules: [], next_offset: null } : page(path))
    mount()
    await vi.waitFor(() => expect(root.textContent).toContain('已加载完整图谱'))
    expect(root.querySelector('canvas')!.getAttribute('aria-label')).toContain('5001 个节点')
    expect(root.textContent).toContain('已加载 5000 / 全图 5000 条关系')
    // Full graph stays in memory; local two-hop switches to five round SVG nodes.
    const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!
    input.value = '节点1000'; input.dispatchEvent(new Event('input', { bubbles: true })); await nextTick()
    button('节点1000 · 状态未提供 ·').click(); await nextTick(); button('查看两跳关联').click()
    await vi.waitFor(() => expect(root.querySelectorAll('.graph-node')).toHaveLength(5))
    expect(root.querySelectorAll('.graph-node circle')).toHaveLength(10)
    expect(root.querySelectorAll('.graph-edge')).toHaveLength(4)
    button('返回全图').click(); await nextTick()
    expect(root.querySelector('canvas')!.getAttribute('aria-label')).toContain('5001 个节点')
  })
  it('pauses in-flight loading and resumes without describing partial data as complete', async () => {
    let resolve: (value: unknown) => void = () => {}
    api.request.mockImplementation(() => new Promise(done => { resolve = done }))
    mount(); await nextTick(); const signal = api.request.mock.calls[0][1].signal
    button('暂停加载').click(); await nextTick()
    expect(signal.aborted).toBe(true); expect(root.textContent).toContain('当前仅为部分数据')
    resolve(page(api.request.mock.calls[0][0])); await nextTick()
    api.request.mockImplementation(async path => page(path)); button('继续加载').click()
    await vi.waitFor(() => expect(root.textContent).toContain('已加载完整图谱'))
  })
  it('clears revoked data after a configuration or publication mismatch', async () => {
    api.request.mockImplementation(async path => { if (path.includes('offset=1000')) throw new ApiError('graph_read_conflict'); return page(path) })
    mount()
    await vi.waitFor(() => expect(root.textContent).toContain('配置已变化'))
    expect(root.querySelector('canvas')).toBeNull(); expect(root.textContent).not.toContain('节点999')
    expect(button('重新加载全图')).toBeDefined()
  })
  it('purges a completed graph, its neighborhood and detail state on configuration revision before refetch', async () => {
    api.request.mockImplementation(async path => path.includes('/details') ? { facts: [], supports: [], rules: [], next_offset: null } : page(path))
    const props = mount()
    await vi.waitFor(() => expect(root.textContent).toContain('已加载完整图谱'))
    const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!
    input.value = '节点1000'; input.dispatchEvent(new Event('input', { bubbles: true })); await nextTick()
    button('节点1000 · 状态未提供 ·').click(); await nextTick(); button('查看两跳关联').click()
    await vi.waitFor(() => expect(root.querySelectorAll('.graph-node')).toHaveLength(5))
    let resolve: (value: unknown) => void = () => {}
    api.request.mockImplementation(() => new Promise(done => { resolve = done }))
    const before = api.request.mock.calls.length
    props.configRevision = 2; await nextTick()
    expect(api.request.mock.calls.length).toBe(before + 1)
    expect(api.request.mock.calls.at(-1)![0]).toContain('/graph/batches?')
    expect(api.request.mock.calls.at(-1)![0]).toContain('offset=0')
    expect(root.querySelector('canvas')).toBeNull()
    expect(root.querySelectorAll('.graph-node')).toHaveLength(0)
    expect(root.querySelector('[aria-label="两跳关联范围"]')).toBeNull()
    expect(root.textContent).not.toContain('节点1000')
    expect(button('返回全图')).toBeUndefined()
    resolve({ generation: 'restricted', part: 'nodes', offset: 0, next_offset: null,
      nodes: [{ id: 'allowed', label: '当前授权对象' }], edges: [], total_nodes: 1, total_edges: 0, matched_nodes: 1, matched_edges: 0 })
    await vi.waitFor(() => expect(root.textContent).toContain('已加载完整图谱'))
    expect(root.querySelectorAll('.graph-node')).toHaveLength(1)
    expect(root.textContent).toContain('当前授权对象')
    expect(root.textContent).not.toContain('节点1000')
  })
  it('cancels old project requests and ignores responses delivered after project switch', async () => {
    const pending: Array<{ path: string; resolve: (value: unknown) => void }> = []
    api.request.mockImplementation(path => new Promise(resolve => pending.push({ path, resolve })))
    const props = mount(); await nextTick(); const signal = api.request.mock.calls[0][1].signal
    props.projectId = 'other'; await nextTick(); expect(signal.aborted).toBe(true)
    pending[0]!.resolve(page(pending[0]!.path)); await nextTick(); await nextTick()
    expect(root.textContent).not.toContain('已加载完整图谱')
    expect(api.request.mock.calls.at(-1)![0]).toContain('/projects/other/')
  })
})
