// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import IdentityCandidates from './IdentityCandidates.vue'
import RunIssues from './RunIssues.vue'
import type { Graph } from './types'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
let app: App, root: HTMLDivElement
const graph: Graph = { nodes: [{ id: 'a', label: 'A', kind: 'Person', layer: 'business', status: 'accepted' }, { id: 'b', label: 'B', kind: 'Person', layer: 'business', status: 'candidate' }], edges: [{ id: 'edge1', source: 'a', target: 'b', relation: 'knows', status: 'candidate' }] }
function button(text: string) { return [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === text)! }
function mount(component: typeof GraphCanvas | typeof IdentityCandidates | typeof RunIssues, props: Record<string, unknown>) { root = document.createElement('div'); document.body.append(root); app = createApp(component, props); app.mount(root) }
beforeEach(() => { api.request.mockReset(); vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }) })
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('业务图来源与归并裁决', () => {
  it('同名节点同时展示状态和范围，长范围保留完整悬浮说明', () => {
    const longScope = '集团合并、区域分拨、批发中心、配送服务、冷链物流'
    mount(GraphCanvas, { graph: { nodes: [
      { id: 'accepted', label: '销售收入', kind: 'ReportItem', status: 'accepted', scopes: ['集团合并'] },
      { id: 'candidate', label: '销售收入', kind: 'ReportItem', status: 'candidate', scopes: ['批发中心'] },
      { id: 'unresolved', label: '销售收入', kind: 'ReportItem', status: 'unresolved', scopes: [longScope] },
    ], edges: [] }, layer: 'business' })
    const nodes = [...root.querySelectorAll('.graph-node')]
    expect(nodes.map(node => node.querySelector('.node-kind')?.textContent)).toEqual([
      'ReportItem · 已接受', 'ReportItem · 候选', 'ReportItem · 未决',
    ])
    expect(nodes[0]!.querySelector('.node-scope')?.textContent).toBe('集团合并')
    expect(nodes[1]!.querySelector('.node-scope')?.textContent).toBe('批发中心')
    expect(nodes[2]!.querySelector('.node-scope')?.textContent).toContain('…')
    expect(nodes[2]!.querySelector('title')?.textContent).toContain(longScope)
  })
  it('同一画布展示超过240节点，搜索突出匹配且不移除上下文', async () => {
    const nodes = Array.from({ length: 241 }, (_, i) => ({ id: `n${i}`, label: `节点${i}`, layer: 'business', scopes: ['集团合并'] }))
    mount(GraphCanvas, { graph: { nodes, edges: [{ source: 'n0', target: 'n240' }], total_nodes: 4001, total_edges: 3966, truncated: true }, layer: 'business' })
    expect(root.querySelectorAll('.graph-node')).toHaveLength(241)
    expect(root.querySelectorAll('.graph-edge')).toHaveLength(1)
    expect(root.textContent).toContain('全图 4001')
    expect(root.textContent).toContain('尚未加载的节点和边不表示不存在')
    expect(button('下一页节点')).toBeUndefined()
    const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!
    input.value = '节点12'; input.dispatchEvent(new Event('input', { bubbles: true })); await nextTick()
    expect(root.querySelectorAll('.graph-node')).toHaveLength(241)
    expect(root.querySelectorAll('.graph-node:not(.dimmed)')).toHaveLength(11)
  })
  it('服务端搜索覆盖全图，发布代次变化时替换整个画布', async () => {
    const initial = { nodes: [{ id: 'first', label: '首页' }], edges: [], total_nodes: 1205, matched_nodes: 1205, offset: 0, page_size: 240, next_offset: 240, generation: 'old', kinds: ['Metric'] }
    api.request.mockImplementation(async (path: string) => {
      if (path.includes('/details')) return { facts: [], supports: [], rules: [], next_offset: null }
      const count = api.request.mock.calls.filter(c => !c[0].includes('/details')).length
      return { ...initial, nodes: [{ id: count === 1 ? 'stale' : 'fresh', label: count === 1 ? '旧分页' : '新节点' }], matched_nodes: 1, next_offset: null, generation: 'new' }
    })
    mount(GraphCanvas, { graph: initial, layer: 'business', projectId: 'p' })
    const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!
    input.value = '新节点'; input.dispatchEvent(new Event('input', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('.graph-node')?.getAttribute('aria-label')).toBe('新节点'))
    expect(root.querySelectorAll('.graph-node')).toHaveLength(1)
    const reads = api.request.mock.calls.filter(c => !c[0].includes('/details'))
    expect(reads).toHaveLength(2)
    expect(reads[1][0]).toContain('offset=0')
    expect(root.textContent).toContain('匹配 1 个节点')
    expect(root.textContent).toContain('全图 1205')
  })
  it('错误建议区分业务未决和审查协议错误，隐藏未知正文', () => {
    mount(RunIssues, { report: { issues: [
      { code: 'semantic_claim_unresolved', table: 'public.items' },
      { code: 'semantic_claim_unresolved', table: 'public.items' },
      { code: 'semantic_review_protocol_invalid' },
      { code: 'unknown', message: '不应显示的任意正文' },
    ] } })
    expect(root.querySelectorAll('.note')).toHaveLength(2)
    expect(root.textContent).toContain('现有证据不能确认业务含义')
    expect(root.textContent).toContain('不代表业务主张已被否定')
    expect(root.textContent).not.toContain('不应显示的任意正文')
  })
  it.each([
    ['excluded_column_projected', '提案使用了已排除的列'],
    ['model_response_truncated', '模型回复未完整返回'],
    ['unexplained_columns', '部分字段尚未被规则解释'],
    ['concept_scope_evidence_required', '概念的共享范围缺少证据'],
    ['semantic_review_unissued_evidence', '本次未签发或重复的引用'],
    ['concept_key_label_conflict', '同一概念编码对应了不同名称'],
    ['concept_binding_conflict', '同一联合上下文对应了不同的概念组合'],
  ])('为实际错误 %s 显示固定建议并隐藏任意错误正文', (code, guidance) => {
    mount(RunIssues, { report: { issues: [{ code, table: 'public.items', message: '秘密源值不能展示' }] } })
    expect(root.querySelectorAll('.note')).toHaveLength(1)
    expect(root.textContent).toContain('public.items')
    expect(root.textContent).toContain(guidance)
    expect(root.textContent).not.toContain('秘密源值不能展示')
  })
  it('同表不同字段诊断分别保留，并展示拒绝码、约束和次数', () => {
    const detail = { code: 'discovery_draft_field_diagnostic', table: 'public.items', reason_code: 'invalid_tool_arguments', constraint: 'enum', count: 3, status: 'diagnostic' }
    mount(RunIssues, { report: { issues: [
      { code: 'discovery_draft_rejected', table: 'public.items', reason_code: 'invalid_tool_arguments', count: 4 },
      { ...detail, field_path: '["claim","members",0,"role"]', message: '秘密输入正文', allowed_references: ['秘密引用'] },
      { ...detail, field_path: '["claim","members",0,"role"]' },
      { ...detail, field_path: '["claim","members",0]', constraint: 'additionalProperties' },
      { ...detail, field_path: '["claim","identity_evidence",0]', reason_code: 'dangling_evidence_reference', constraint: 'reference', count: 1 },
    ] } })
    expect(root.querySelectorAll('.note')).toHaveLength(4)
    expect(root.textContent).toContain('invalid_tool_arguments')
    expect(root.textContent).toContain('dangling_evidence_reference')
    expect(root.textContent).toContain('claim.members[0].role')
    expect(root.textContent).toContain('claim.identity_evidence[0]')
    expect(root.textContent).toContain('约束：enum')
    expect(root.textContent).toContain('约束：additionalProperties')
    expect(root.textContent).toContain('发生 3 次')
    expect(root.textContent).toContain('发生 4 次')
    expect(root.textContent).not.toContain('秘密输入正文')
    expect(root.textContent).not.toContain('秘密引用')
  })
  it('不显示畸形字段诊断中的原始正文', () => {
    mount(RunIssues, { report: { issues: [
      { code: 'discovery_draft_field_diagnostic', table: 'public.items', field_path: '{"secret":"秘密源值"}', reason_code: '拒绝秘密正文', constraint: '<script>秘密约束</script>', count: -4 },
    ] } })
    expect(root.querySelectorAll('.note')).toHaveLength(1)
    expect(root.textContent).not.toContain('秘密')
    expect(root.textContent).not.toContain('发生 -4 次')
  })
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
