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
const focused: Graph = { ...initial, nodes: [{ id: 'a', label: '中心' }, { id: 'b', label: '直接邻居' }, { id: 'c', label: '间接邻居' }], edges: [{ source: 'b', target: 'a' }, { source: 'c', target: 'b' }], focus_node_id: 'a', focus_hops: 2, focus_traversal: 'chains', focus_neighbor_count: 2, next_offset: null }
const sharedOwner: Graph = {
  nodes: ['a', 'owner', 'sibling', 'parent', 'peer', 'grandparent', 'child', 'grandchild', 'far'].map(id => ({ id, label: `对象${id}` })),
  edges: [
    { source: 'a', target: 'owner' }, { source: 'sibling', target: 'owner' },
    { source: 'parent', target: 'a' }, { source: 'parent', target: 'peer' }, { source: 'grandparent', target: 'parent' },
    { source: 'a', target: 'child' }, { source: 'child', target: 'grandchild' }, { source: 'grandchild', target: 'far' },
  ],
}
function mount(graph = initial, extra: Record<string, unknown> = {}) { root = document.createElement('div'); document.body.append(root); const props = { graph, projectId: 'p', layer: 'business', ...extra }; app = createApp(GraphCanvas, props); app.mount(root); return props }
function button(text: string) { return [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === text)! }
function node(label = '中心') { return root.querySelector(`[class~="graph-node"][aria-label="${label}"]`)! }
function graphCalls() { return api.request.mock.calls.filter(([path]) => !path.includes('/details')) }
function pointerClick(target: Element, x: number, y: number, detail: number, jitter = 0) {
  target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1, button: 0, clientX: x, clientY: y }))
  if (jitter) target.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 1, clientX: x + jitter, clientY: y }))
  target.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1, button: 0, clientX: x + jitter, clientY: y }))
  target.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x + jitter, clientY: y, detail }))
}
beforeEach(() => { api.request.mockReset(); vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }); vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null) })
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('two-hop graph exploration', () => {
  it('defaults to upstream/downstream chains and lets the user explicitly include shared-owner siblings', async () => {
    api.request.mockResolvedValue(details)
    mount(sharedOwner, { projectId: undefined })
    node('对象a').dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); await nextTick()
    expect([...root.querySelectorAll('.graph-node')].map(n => n.getAttribute('aria-label'))).toEqual(['对象a', '对象owner', '对象parent', '对象grandparent', '对象child', '对象grandchild'])
    expect(node('对象sibling')).toBeNull(); expect(node('对象peer')).toBeNull(); expect(node('对象far')).toBeNull()
    expect(root.querySelectorAll('.graph-edge')).toHaveLength(5)
    const mode = root.querySelector<HTMLSelectElement>('[aria-label="两跳查看方式"]')!
    expect(mode.value).toBe('chains')
    mode.value = 'all'; mode.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    expect(node('对象sibling')).not.toBeNull(); expect(node('对象peer')).not.toBeNull(); expect(node('对象far')).toBeNull()
    mode.value = 'chains'; mode.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    expect(node('对象sibling')).toBeNull(); expect(node('对象peer')).toBeNull()
    button('返回全图').click(); await nextTick()
    expect(root.querySelectorAll('.graph-node')).toHaveLength(9)
    expect(api.request).not.toHaveBeenCalled()
    // The explicit details action has exactly the same default as double-click.
    node('对象a').dispatchEvent(new MouseEvent('click', { bubbles: true })); await nextTick()
    button('查看两跳关联').click(); await nextTick()
    expect(node('对象sibling')).toBeNull(); expect(node('对象peer')).toBeNull()
    expect(root.querySelectorAll('.graph-node')).toHaveLength(6)
  })

  it('keeps the same traversal for preview and server reads, ignoring a late previous mode', async () => {
    let finishOld!: (value: Graph) => void
    api.request.mockImplementation((path: string) => {
      if (path.includes('/details')) return Promise.resolve(details)
      if (new URL(path, 'http://test').searchParams.get('traversal') === 'chains') return new Promise(resolve => { finishOld = resolve })
      return Promise.resolve(twoHopNeighborhood(sharedOwner, 'a', 'all'))
    })
    mount({ ...sharedOwner, total_nodes: 2000, next_offset: 9, page_size: 1000 })
    node('对象a').dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); await nextTick()
    expect(node('对象sibling')).toBeNull()
    const mode = root.querySelector<HTMLSelectElement>('[aria-label="两跳查看方式"]')!
    mode.value = 'all'; mode.dispatchEvent(new Event('change', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('[aria-label="两跳范围补全状态"]')).toBeNull())
    expect(node('对象sibling')).not.toBeNull()
    expect(graphCalls().map(([url]) => new URL(url, 'http://test').searchParams.get('traversal'))).toEqual(['chains', 'all'])
    expect(graphCalls()[0]![1].signal.aborted).toBe(true)
    finishOld(twoHopNeighborhood(sharedOwner, 'a', 'chains')); await nextTick(); await nextTick()
    expect(node('对象sibling')).not.toBeNull()
    expect(mode.value).toBe('all')
  })

  it('keeps independent directions through cycles and never pulls a missing endpoint into a preview', () => {
    const graph: Graph = { nodes: ['a', 'b', 'c', 'd', 'e'].map(id => ({ id })), edges: [
      { source: 'a', target: 'a' }, { source: 'a', target: 'b' }, { source: 'b', target: 'a' },
      { source: 'b', target: 'c' }, { source: 'd', target: 'b' }, { source: 'missing', target: 'a' }, { source: 'missing', target: 'e' },
    ] }
    expect(twoHopNeighborhood(graph, 'a', 'chains').nodes.map(n => n.id)).toEqual(['a', 'b', 'c', 'd'])
    expect(twoHopNeighborhood(graph, 'missing', 'chains').nodes).toEqual([])
  })
  it('hides unrelated nodes immediately while the full neighborhood is still loading', async () => {
    let finish!: (value: Graph) => void
    api.request.mockImplementation((path: string) => path.includes('/details') ? Promise.resolve(details) : new Promise(resolve => { finish = resolve }))
    mount(); node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); await nextTick()
    expect(root.querySelector('[aria-label="两跳关联范围"]')).not.toBeNull()
    expect(node('无关对象')).toBeNull()
    expect(node()).not.toBeNull()
    expect(button('返回全图')).toBeDefined()
    finish(focused)
    await vi.waitFor(() => expect(node('间接邻居')).not.toBeNull())
    expect(node('无关对象')).toBeNull()
  })

  it('allows returning to the full graph before the first neighborhood response arrives', async () => {
    let finish!: (value: Graph) => void
    api.request.mockImplementation((path: string) => path.includes('/details') ? Promise.resolve(details) : new Promise(resolve => { finish = resolve }))
    mount(); node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); await nextTick()
    expect(button('返回全图')).toBeDefined()
    button('返回全图').click(); await nextTick()
    expect(node('无关对象')).not.toBeNull()
    expect(graphCalls()[0]![1].signal.aborted).toBe(true)
    finish(focused); await nextTick(); await nextTick()
    expect(node('无关对象')).not.toBeNull()
    expect(root.querySelector('[aria-label="两跳关联范围"]')).toBeNull()
  })
  it('searches an off-page identity then loads its five neighbors without search filters', async () => {
    const center = { id: 'report', label: '报表项', kind: '指标', identity: { 编码: 'EBG_PL_92200' } }
    const neighbors = Array.from({ length: 5 }, (_, i) => ({ id: `near${i}`, label: `邻居${i}`, kind: '维度' }))
    const neighborhood = { ...initial, nodes: [center, ...neighbors],
      edges: neighbors.map((n, i) => ({ id: `edge${i}`, source: center.id, target: n.id, relation: '关联' })),
      focus_node_id: center.id, focus_neighbor_count: 5, focus_total_edges: 5, matched_nodes: 6, next_offset: null }
    api.request.mockImplementation(async (path: string) => {
      if (path.includes('/details')) return { ...details, total_incident_edges: 5 }
      const url = new URL(path, 'http://test')
      return url.searchParams.get('focus') ? neighborhood : { ...initial, nodes: [center], edges: [], matched_nodes: 1, next_offset: null }
    })
    mount(); const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!
    input.value = 'EBG_PL_92200'; input.dispatchEvent(new Event('input', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelectorAll('.graph-edge')).toHaveLength(5))
    const query = new URL(graphCalls()[1]![0], 'http://test').searchParams
    expect(query.get('focus')).toBe('report'); expect(query.get('hops')).toBe('1')
    expect(query.has('q')).toBe(false); expect(query.has('kind')).toBe(false)
    expect(root.textContent).toContain('匹配 1 个节点')
    expect(root.querySelector('[aria-label="节点关联范围"]')?.textContent).toContain('当前画布显示 5 条相连关系，当前授权图中共有 5 条')
    node('报表项').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('[aria-label="两跳关联范围"]')).not.toBeNull())
    const twoHop = new URL(graphCalls().at(-1)![0], 'http://test').searchParams
    expect(twoHop.get('hops')).toBe('2'); expect(twoHop.has('q')).toBe(false); expect(twoHop.has('kind')).toBe(false)
    expect(input.value).toBe('')
    expect(root.querySelectorAll('.graph-node.dimmed')).toHaveLength(0)
    expect(root.querySelectorAll('.graph-edge')).toHaveLength(5)
  })

  it('keeps the search result and explains local edges when neighbor loading fails', async () => {
    api.request.mockImplementation(async (path: string) => {
      if (path.includes('/details')) return { ...details, total_incident_edges: 5 }
      if (new URL(path, 'http://test').searchParams.get('focus')) throw new Error('邻域读取失败')
      return { ...initial, nodes: [{ id: 'found', label: '查到的对象' }], edges: [], matched_nodes: 1, next_offset: null }
    })
    mount(); const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!
    input.value = '查到'; input.dispatchEvent(new Event('input', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toBe('邻域读取失败'))
    expect(node('查到的对象')).not.toBeNull()
    expect(root.querySelector('[aria-label="节点关联范围"]')?.textContent).toContain('当前画布显示 0 条相连关系，当前授权图中共有 5 条')
    expect(root.textContent).toContain('不能据此判断节点孤立')
  })

  it('loads two hops from the scoped server graph and restores the original scene', async () => {
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : focused)
    mount(initial, { includeCandidates: true }); await nextTick()
    const position = node().getAttribute('transform')
    node().dispatchEvent(new MouseEvent('click', { bubbles: true })); await nextTick()
    expect(graphCalls()).toHaveLength(0)
    node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelectorAll('.graph-node')).toHaveLength(3))
    const url = new URL(graphCalls()[0]![0], 'http://test')
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ focus: 'a', hops: '2', traversal: 'chains', layer: 'business', include_candidates: 'true', limit: '1000' })
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

  it('keeps unrelated nodes hidden and allows retry or return when the neighborhood request fails', async () => {
    api.request.mockRejectedValue(new Error('读取暂时失败'))
    mount(); node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toBe('读取暂时失败'))
    expect(node('无关对象')).toBeNull()
    expect(button('返回全图')).toBeDefined()
    expect(button('重试读取两跳关联')).toBeDefined()
    button('返回全图').click(); await nextTick()
    expect(node('无关对象')).not.toBeNull()
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

  it('opens an SVG node on the second click without requiring a separate native dblclick', async () => {
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : focused)
    mount(); await nextTick()
    const target = node().querySelector('circle')!
    pointerClick(target, 90, 70, 1); await nextTick()
    expect(graphCalls()).toHaveLength(0)
    pointerClick(target, 90, 70, 2)
    expect(graphCalls()).toHaveLength(1)
    // Browsers normally dispatch this after click(detail=2); it must not issue a duplicate request.
    target.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, detail: 2 }))
    await vi.waitFor(() => expect(root.querySelector('[aria-label="两跳关联范围"]')).not.toBeNull())
    expect(graphCalls()).toHaveLength(1)
  })

  it('opens Canvas node labels with a real click sequence and tolerates pointer jitter', async () => {
    const large: Graph = { ...initial, nodes: Array.from({ length: 301 }, (_, i) => ({ id: `n${i}`, label: `节点${i}` })), edges: [], next_offset: null }
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : { ...focused, nodes: [{ id: 'n0', label: '节点0' }], edges: [], focus_neighbor_count: 0 })
    mount(large); await nextTick(); await nextTick(); button('原始大小').click(); await nextTick()
    const canvas = root.querySelector('canvas')!
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 820, bottom: 480, width: 820, height: 480, toJSON() {} })
    canvas.setPointerCapture = vi.fn()
    const p = placeNodes(large).get('n0')!
    pointerClick(canvas, p.x, p.y + 40, 1, 2); await nextTick()
    expect(root.querySelector('.detail-name')?.textContent).toBe('节点0')
    pointerClick(canvas, p.x, p.y + 40, 2, 2)
    canvas.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, detail: 2, clientX: p.x + 2, clientY: p.y + 40 }))
    await vi.waitFor(() => expect(root.querySelector('[aria-label="两跳关联范围"]')).not.toBeNull())
    expect(graphCalls()).toHaveLength(1)
    expect(graphCalls()[0]![0]).toContain('focus=n0')
    expect(canvas.setPointerCapture).not.toHaveBeenCalled()
  })

  it.each(['drag', 'cancel'])('does not open a neighborhood after a Canvas %s gesture', async gesture => {
    const large: Graph = { ...initial, nodes: Array.from({ length: 301 }, (_, i) => ({ id: `n${i}`, label: `节点${i}` })), edges: [], next_offset: null }
    api.request.mockResolvedValue(details)
    mount(large); await nextTick(); await nextTick(); button('原始大小').click(); await nextTick()
    const canvas = root.querySelector('canvas')!
    vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 820, bottom: 480, width: 820, height: 480, toJSON() {} })
    canvas.setPointerCapture = vi.fn()
    const p = placeNodes(large).get('n0')!
    canvas.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1, button: 0, clientX: p.x, clientY: p.y }))
    const x = p.x + (gesture === 'drag' ? 40 : 0)
    if (gesture === 'drag') {
      canvas.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 1, clientX: x, clientY: p.y }))
      canvas.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1, clientX: x, clientY: p.y }))
      expect(canvas.setPointerCapture).toHaveBeenCalledWith(1)
    } else canvas.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true, pointerId: 1 }))
    canvas.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 2, clientX: x, clientY: p.y }))
    canvas.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, detail: 2, clientX: x, clientY: p.y }))
    await nextTick()
    expect(graphCalls()).toHaveLength(0)
    expect(root.querySelector('[aria-label="两跳关联范围"]')).toBeNull()
    // A new intentional click works after the discarded gesture.
    pointerClick(canvas, x, p.y, 1); await nextTick()
    expect(root.querySelector('.detail-name')?.textContent).toBe('节点0')
  })

  it('uses the same immediate double-click action in the accessible node list', async () => {
    const large: Graph = { ...initial, nodes: Array.from({ length: 301 }, (_, i) => ({ id: `n${i}`, label: `节点${i}` })), edges: [], next_offset: null }
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : { ...focused, nodes: [{ id: 'n0', label: '节点0' }], edges: [], focus_neighbor_count: 0 })
    mount(large); await nextTick()
    const target = root.querySelector('.scene-node-list button')!
    pointerClick(target, 10, 10, 1); await nextTick()
    pointerClick(target, 10, 10, 2)
    target.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, detail: 2 }))
    await vi.waitFor(() => expect(root.querySelector('[aria-label="两跳关联范围"]')).not.toBeNull())
    expect(graphCalls()).toHaveLength(1)
  })

  it('loads metadata neighborhoods from the server beyond the loaded metadata slice', async () => {
    api.request.mockImplementation(async (path: string) => path.includes('/details') ? details : focused)
    mount(initial, { layer: 'metadata' }); node().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await vi.waitFor(() => expect(node('间接邻居')).not.toBeNull())
    const url = new URL(graphCalls()[0]![0], 'http://test')
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ layer: 'metadata', focus: 'a', hops: '2', traversal: 'all' })
  })

  it('filters a fully loaded project graph locally and stops after two hops', async () => {
    const complete: Graph = {
      generation: 'g1', total_nodes: 4, total_edges: 3, next_offset: null, truncated: false, page_size: 1000,
      nodes: ['a', 'b', 'c', 'd'].map(id => ({ id, label: `对象${id}` })),
      edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }, { source: 'c', target: 'd' }],
    }
    api.request.mockResolvedValue(details)
    mount(complete)
    node('对象a').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await nextTick()
    expect(graphCalls()).toHaveLength(0)
    expect(root.querySelectorAll('.graph-node')).toHaveLength(3)
    expect(node('对象d')).toBeNull()
    expect(root.textContent).toContain('两跳范围已加载 3 / 3 个节点')
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
