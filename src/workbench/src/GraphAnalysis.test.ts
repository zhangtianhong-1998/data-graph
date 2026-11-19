// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref } from 'vue'
import type { App } from 'vue'
import GraphAnalysis from './GraphAnalysis.vue'

const api = vi.hoisted(() => ({ request: vi.fn(), ensureSession: vi.fn() }))
vi.mock('./api', () => ({ projectPath: (id: string) => '/projects/' + id, ...api }))
let app: App, root: HTMLDivElement
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); api.request.mockReset() })
const complete = {version_id: 'v1', status: 'completed', counts: {nodes: 2, edges: 1, isolated_nodes: 0, communities: 1},
  nodes: [{id: 'r', community_id: 'community_p', in_degree: 1, out_degree: 0, degree_centrality: 1, pagerank: 0.65}],
  communities: [{id: 'community_p', size: 2}]}
function mount() {
  const context = ref({project: 'M'})
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({render: () => h(GraphAnalysis, {projectId: 'p', version: 'v1', businessContext: context.value,
    graph: {nodes: [{id: 'r', label: '收入指标'}], edges: []}})})
  app.mount(root)
  return context
}
const button = (text: string) => [...root.querySelectorAll('button')].find(b => b.textContent?.includes(text))!
describe('版本图分析', () => {
  it('keeps export and offline calculation on the selected version and business context', async () => {
    api.request.mockImplementation(async (url, opt) => {
      if (url.includes('/export?')) return new Blob(['<graphml/>'])
      return opt?.method === 'POST' ? {...complete, status: 'queued'} : complete
    })
    const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:test')
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    mount()
    await vi.waitFor(() => expect(button('已完成')).toBeTruthy())
    button('全图分析').click()
    await nextTick()
    expect(root.textContent).toContain('收入指标')
    expect(root.textContent).toContain('0.6500')
    for (const [label, format] of [['Semantica JSON', 'semantica-json'], ['GraphML', 'graphml']]) {
      button(label!).click()
      await vi.waitFor(() => expect(api.request.mock.calls.some(([url]) => url.includes('format=' + format))).toBe(true))
      await nextTick()
    }
    expect(create).toHaveBeenCalledTimes(2)
    button('重新计算').click()
    await vi.waitFor(() => expect(button('排队中')).toBeTruthy())
    for (const [url, opts] of api.request.mock.calls) {
      const u = new URL(url, 'https://workbench.test')
      expect(u.searchParams.get('version')).toBe('v1')
      expect(u.searchParams.get('business_context')).toBe('{"project":"M"}')
      if (u.pathname.endsWith('analytics') && opts.method !== 'POST') expect(u.searchParams.get('limit')).toBe('10')
    }
  })

  it('drops delayed analytics from the previous business context', async () => {
    let resolveFirst!: (value: unknown) => void
    api.request.mockImplementationOnce(() => new Promise(resolve => {resolveFirst = resolve}))
    api.request.mockResolvedValue({version_id: 'v1', status: 'not_built'})
    const context = mount()
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledTimes(1))
    context.value = {project: 'K'}
    await vi.waitFor(() => expect(button('尚未计算')).toBeTruthy())
    resolveFirst(complete)
    await nextTick()
    expect(button('尚未计算')).toBeTruthy()
    expect(root.textContent).not.toContain('已完成')
  })
})
