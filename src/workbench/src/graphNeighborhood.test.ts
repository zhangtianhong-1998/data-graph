// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import type { Graph } from './types'
import { twoHopNeighborhood } from './graphNeighborhood'
import { placeNodes, sceneBounds } from './graphScene'
import { fitCamera } from './graphViewport'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
let app: App, root: HTMLDivElement
const details = { facts: [], supports: [], rules: [], next_offset: null }
const initial: Graph = { generation: 'g1', nodes: [{ id: 'a', label: '中心' }, { id: 'unrelated', label: '无关对象' }], edges: [], total_nodes: 2000, page_size: 240, next_offset: 240 }
const focused: Graph = { ...initial, nodes: [{ id: 'a', label: '中心' }, { id: 'b', label: '直接邻居' }, { id: 'c', label: '间接邻居' }], edges: [{ source: 'b', target: 'a' }, { source: 'b', target: 'c' }], focus_node_id: 'a', focus_hops: 2, focus_neighbor_count: 2, next_offset: null }
function mount(graph = initial, extra: Record<string, unknown> = {}) { root = document.createElement('div'); document.body.append(root); const props = { graph, projectId: 'p', layer: 'business', ...extra }; app = createApp(GraphCanvas, props); app.mount(root); return props }
function button(text: string) { return [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === text)! }
function node(label = '中心') { return root.querySelector(`[class~="graph-node"][aria-label="${label}"]`)! }
function graphCalls() { return api.request.mock.calls.filter(([path]) => !path.includes('/details')) }
beforeEach(() => { api.request.mockReset(); vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }); vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null) })
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('two-hop graph exploration', () => {
  it('loads two hops from the scoped server graph and restores the original scene', async () => {
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : focused)
    mount(initial, { includeCandidates: true }); await nextTick()
    const position = node().getAttribute('transform')
    node().dispatchEvent(new MouseEvent('click', { bubbles: true })); await nextTick()
    expect(graphCalls()).toHaveLength(0)
    node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelectorAll('.graph-node')).toHaveLength(3))
    const url = new URL(graphCalls()[0]![0], 'http://test')
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ focus: 'a', hops: '2', layer: 'business', include_candidates: 'true', limit: '1000' })
    expect(node('无关对象')).toBeNull()
    expect(node('间接邻居')).not.toBeNull()
    expect(root.querySelectorAll('.graph-edge')).toHaveLength(2)
    expect(root.textContent).toContain('两跳范围已加载 3 / 3 个节点')
    expect(button('展开相邻节点')).toBeUndefined()
    button('返回全图').click(); await nextTick()
    expect(node('无关对象')).not.toBeNull()
    expect(node('间接邻居')).toBeNull()
    expect(node().getAttribute('transform')).toBe(position)
    expect(graphCalls()).toHaveLength(1)
  })

  it('provides an explicit detail-panel action and shows bounded results honestly', async () => {
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : { ...focused, focus_neighbor_count: 2500, truncated: true, neighborhood_truncated: true })
    mount(); node().dispatchEvent(new MouseEvent('click', { bubbles: true })); await nextTick()
    button('查看两跳关联').click()
    await vi.waitFor(() => expect(root.textContent).toContain('两跳范围已加载 3 / 2501 个节点'))
    expect(root.textContent).toContain('尚未完整')
    expect(root.textContent).toContain('1000 个节点')
  })

  it('keeps focused searches local and retains the center when no edges exist', async () => {
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : { ...focused, nodes: [focused.nodes[0]], edges: [], focus_neighbor_count: 0 })
    mount(); node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(root.textContent).toContain('两跳范围已加载 1 / 1 个节点'))
    const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!
    input.value = '中心'; input.dispatchEvent(new Event('input', { bubbles: true })); await nextTick()
    expect(graphCalls()).toHaveLength(1)
    expect(node()).not.toBeNull()
  })

  it('ignores an old neighborhood response after returning to the full graph', async () => {
    let resolveOld: (value: Graph) => void = () => {}
    api.request.mockImplementation((path: string) => path.includes('/details') ? Promise.resolve(details) : path.includes('focus=b') ? new Promise(resolve => { resolveOld = resolve }) : Promise.resolve(focused))
    mount(); node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(node('直接邻居')).not.toBeNull())
    node('直接邻居').dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); await nextTick()
    const signal = graphCalls()[1]![1].signal as AbortSignal
    button('返回全图').click(); await nextTick()
    expect(signal.aborted).toBe(true)
    resolveOld({ ...focused, nodes: [{ id: 'b', label: '过期范围' }] }); await nextTick(); await nextTick()
    expect(node('无关对象')).not.toBeNull()
    expect(root.textContent).not.toContain('过期范围')
  })

  it('keeps the current scene available when the neighborhood request fails', async () => {
    api.request.mockRejectedValue(new Error('读取暂时失败'))
    mount(); node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toBe('读取暂时失败'))
    expect(node('无关对象')).not.toBeNull()
    expect(button('返回全图')).toBeUndefined()
  })

  it('refreshes the base graph instead of restoring a previous publication', async () => {
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : path.includes('hops=2') ? { ...focused, generation: 'g2' } : { ...initial, generation: 'g2', nodes: [{ id: 'new', label: '新发布节点' }] })
    mount(); node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(button('返回全图')).toBeDefined())
    button('返回全图').click()
    await vi.waitFor(() => expect(node('新发布节点')).not.toBeNull())
    expect(node('无关对象')).toBeNull()
  })

  it('handles double-clicks on the Canvas renderer using the transformed hit position', async () => {
    const large: Graph = { ...initial, nodes: Array.from({ length: 301 }, (_, i) => ({ id: `n${i}`, label: `节点${i}` })), edges: [], next_offset: null }
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : { ...focused, nodes: [{ id: 'n0', label: '节点0' }], edges: [], focus_neighbor_count: 0 })
    mount(large); await nextTick(); await nextTick()
    const canvas = root.querySelector('canvas')!
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 820, bottom: 480, width: 820, height: 480, toJSON() {} })
    const positions = placeNodes(large), p = positions.get('n0')!
    const camera = fitCamera(sceneBounds(positions), { width: 820, height: 480 })
    canvas.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, clientX: p.x * camera.zoom + camera.offset.x, clientY: p.y * camera.zoom + camera.offset.y }))
    await vi.waitFor(() => expect(graphCalls()).toHaveLength(1))
    expect(graphCalls()[0]![0]).toContain('focus=n0')
    expect(graphCalls()[0]![0]).toContain('hops=2')
  })

  it('loads metadata neighborhoods from the server beyond the loaded metadata slice', async () => {
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : focused)
    mount(initial, { layer: 'metadata' }); node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(node('间接邻居')).not.toBeNull())
    const url = new URL(graphCalls()[0]![0], 'http://test')
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ layer: 'metadata', focus: 'a', hops: '2' })
  })

  it('uses the complete local metadata graph for either edge direction, stopping after two hops', async () => {
    const graph: Graph = { nodes: ['a', 'b', 'c', 'd', 'e'].map(id => ({ id, label: `对象${id}` })), edges: [{ source: 'b', target: 'a' }, { source: 'b', target: 'c' }, { source: 'c', target: 'd' }, { source: 'c', target: 'b' }, { source: 'a', target: 'a' }] }
    expect(twoHopNeighborhood(graph, 'a').nodes.map(n => n.id)).toEqual(['a', 'b', 'c'])
    api.request.mockResolvedValue(details)
    mount(graph, { layer: 'metadata', projectId: undefined })
    node('对象a').dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); await nextTick()
    expect(root.querySelectorAll('.graph-node')).toHaveLength(3)
    expect(node('对象d')).toBeNull(); expect(node('对象e')).toBeNull()
    expect(api.request).not.toHaveBeenCalled()
    button('返回全图').click(); await nextTick()
    expect(root.querySelectorAll('.graph-node')).toHaveLength(5)
  })
})
