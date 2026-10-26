// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import IdentityCandidates from './IdentityCandidates.vue'
import type { Graph } from './types'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
let app: App, root: HTMLDivElement
const graph: Graph = { nodes: [{ id: 'a', label: 'A', kind: 'Person', layer: 'business', status: 'accepted' }, { id: 'b', label: 'B', kind: 'Person', layer: 'business', status: 'candidate' }], edges: [{ id: 'edge1', source: 'a', target: 'b', relation: 'knows', status: 'candidate' }] }
function button(text: string) { return [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === text)! }
function mount(component: typeof GraphCanvas | typeof IdentityCandidates, props: Record<string, unknown>) { root = document.createElement('div'); document.body.append(root); app = createApp(component, props); app.mount(root) }
beforeEach(() => { api.request.mockReset(); vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }) })
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('业务图来源与归并裁决', () => {
  it('显示候选关系状态，键盘选中后按 fact ID 读取授权支持', async () => {
    api.request.mockResolvedValue({ facts: [{ id: 'edge1', fact_kind: 'Edge', payload: graph.edges[0], status: 'candidate' }], supports: [{ id: 'support', fact_id: 'edge1', status: 'candidate', origin: 'source_query', source_binding: 'people.code → family.parent_code', rule_id: 'r', rule_revision: 3, publication_id: 'pub', scope_ref: 'public', document_ref: 'doc:family' }], rules: [{ identity_columns: ['code', 'name'] }], next_offset: null, supports_truncated: false })
    mount(GraphCanvas, { graph, layer: 'business', projectId: 'p', includeCandidates: true })
    const edge = root.querySelector<SVGGElement>('.graph-edge')!
    expect(edge.getAttribute('aria-label')).toContain('候选'); edge.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('[aria-label="来源支持"]')?.textContent).toContain('people.code → family.parent_code'))
    expect(api.request).toHaveBeenCalledWith('/projects/p/graph/details?fact_id=edge1&include_candidates=true&offset=0')
    expect(root.querySelector('.graph-detail')?.textContent).toContain('候选'); expect(root.textContent).toContain('doc:family'); expect(root.textContent).toContain('字段映射与语义依据')
  })
  it('候选先展示原对象依据，确认后携带配置和裁决修订，支持撤销', async () => {
    let revision = 0
    const listing = () => ({ revision, candidates: [{ candidate_id: 'pair', members: ['a', 'b'], nodes: graph.nodes, evidence: ['f1'], facts: [{ id: 'f1', payload: { name: 'label', value: 'Shared' } }], supports: [{ source_binding: 'source_columns' }], channels: ['name'], status: 'candidate', has_active_decision: revision === 1 }], decisions: revision ? [{ decision_id: 'decision', candidate_id: 'pair', verdict: 'same', revision: 1, active: revision === 1, status: revision === 1 ? 'active' : 'revoked', members: ['a', 'b'], nodes: graph.nodes }] : [], total_candidates: 1, total_decisions: revision ? 1 : 0, next_offset: null, next_decision_offset: null })
    api.request.mockImplementation(async (_path, options) => { if (options?.method === 'POST') { revision++; return { revision } }; return listing() })
    const changed = vi.fn(); mount(IdentityCandidates, { projectId: 'p', configRevision: '"7"', graph, onChanged: changed })
    await vi.waitFor(() => expect(root.querySelector('#identity-candidate')).not.toBeNull())
    expect(api.request.mock.calls.every(call => !call[1])).toBe(true)
    const select = root.querySelector<HTMLSelectElement>('#identity-candidate')!; select.value = 'pair'; select.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    expect(root.textContent).toContain('source_columns'); expect(root.textContent).toContain('Shared')
    const confirm = vi.fn().mockReturnValue(false); vi.stubGlobal('confirm', confirm); button('确认为同一对象').click(); await nextTick()
    expect(api.request.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(0)
    confirm.mockReturnValue(true); button('确认为同一对象').click()
    await vi.waitFor(() => expect(changed).toHaveBeenCalledTimes(1))
    const write = api.request.mock.calls.find(call => call[1]?.method === 'POST')!
    expect(write).toEqual(['/projects/p/graph/identity-decisions', { method: 'POST', headers: { 'If-Match': '"7"' }, body: JSON.stringify({ revision: 0, candidate_id: 'pair', verdict: 'same' }) }])
    expect(button('确认为同一对象').disabled).toBe(true)
    button('撤销裁决').click(); await vi.waitFor(() => expect(changed).toHaveBeenCalledTimes(2))
    expect(api.request.mock.calls.find(call => call[0].endsWith('/revoke'))?.[1].body).toBe(JSON.stringify({ revision: 1 }))
    expect(root.textContent).toContain('已撤销')
  })
  it('运行或配置未保存期间不能提交裁决', async () => {
    api.request.mockResolvedValue({ revision: 0, candidates: [{ candidate_id: 'pair', members: ['a', 'b'], nodes: graph.nodes, evidence: [], facts: [], supports: [], channels: ['name'], status: 'candidate' }], decisions: [], total_candidates: 1, next_offset: null })
    mount(IdentityCandidates, { projectId: 'p', configRevision: '"7"', graph, disabled: true })
    await vi.waitFor(() => expect(root.querySelector('#identity-candidate')).not.toBeNull())
    const select = root.querySelector<HTMLSelectElement>('#identity-candidate')!; select.value = 'pair'; select.dispatchEvent(new Event('change', { bubbles: true })); await nextTick()
    expect(button('确认为同一对象').disabled).toBe(true); expect(button('确认为不同对象').disabled).toBe(true)
  })
})
