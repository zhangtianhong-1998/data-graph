// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import IdentityCandidates from './IdentityCandidates.vue'
import RunIssues from './RunIssues.vue'
import type { Graph } from './types'
import { nodeProperties } from './graphLabels'
const api = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), request: api.request }))
let app: App, root: HTMLDivElement
const graph: Graph = { nodes: [{ id: 'a', label: 'A', kind: 'Person', layer: 'business', status: 'accepted' }, { id: 'b', label: 'B', kind: 'Person', layer: 'business', status: 'candidate' }], edges: [{ id: 'edge1', source: 'a', target: 'b', relation: 'knows', status: 'candidate' }] }
function button(text: string) { return [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === text)! }
function mount(component: typeof GraphCanvas | typeof IdentityCandidates | typeof RunIssues, props: Record<string, unknown>) { root = document.createElement('div'); document.body.append(root); app = createApp(component, props); app.mount(root) }
beforeEach(() => { api.request.mockReset(); vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} }) })
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('业务图来源与归并裁决', () => {
  it('列定义展示元数据与声明来源，证据引用无需展开JSON', async () => {
    const definition = { id: 'measure', label: '去年同期季度完成', label_origin: 'metadata_declared',
      identity_origin: 'physical_column', kind: '度量定义', status: 'accepted',
      identity: { source_id: 'facts', schema: 'public', table: 'observations', column: 'comparison_total' },
      properties: { physical_column: ['comparison_total'], catalog_comment: ['去年同期季度完成'] } }
    api.request.mockResolvedValue({ facts: [{ id: 'measure', fact_kind: 'NodeExistence', status: 'accepted', payload: { entity_id: 'measure', kind: '度量定义' } }],
      supports: [
        { id: 'catalog', fact_id: 'measure', status: 'accepted', origin: 'metadata', source_binding: 'facts.public.observations', scope_ref: 'public',
          binding: { source_kind: 'metadata_column', evidence_refs: ['e-metadata'] } },
        { id: 'meaning', fact_id: 'measure', status: 'accepted', origin: 'agent_declared', source_binding: 'facts.public.observations', scope_ref: 'public',
          binding: { source_kind: 'metadata_column', evidence_refs: ['doc@revision:measure-definition'] } },
      ], rules: [], next_offset: null, supports_truncated: false })
    mount(GraphCanvas, { graph: { nodes: [definition], edges: [] }, layer: 'business', projectId: 'p' })
    root.querySelector('.graph-node')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelectorAll('.support-detail')).toHaveLength(2))
    const tables = [...root.querySelectorAll('.support-detail .property-table')].map(n => n.textContent).join(' ')
    expect(tables).toContain('授权字段元数据'); expect(tables).toContain('Agent 依据材料声明')
    expect(tables).toContain('e-metadata'); expect(tables).toContain('doc@revision:measure-definition')
    expect(tables).not.toContain('事实数据查询')
    expect(root.querySelector('.graph-detail')?.textContent).toContain('此节点代表列定义')
    expect(nodeProperties(definition)).not.toHaveProperty('identity_origin')
    const propertyTables = [...root.querySelectorAll('.graph-detail .property-table')].map(n => n.textContent).join(' ')
    expect(propertyTables).toContain('物理字段'); expect(propertyTables).toContain('字段注释')
    expect(propertyTables).not.toContain('physical_column（未提供中文名）')
    expect(root.querySelector('.support-detail')?.textContent).toContain('不是经营数值的查询结果')
    expect(root.querySelectorAll('.support-detail')[1]?.textContent).toContain('不代表已独立确认业务解释')
  })
  it.each([
    ['schema_applies_to_rows', '声明适用于实际行对象', '未声明已观察到非空值', '不证明经营数值非空'],
    ['observed_non_null', '实际行中该字段非空', '已观察到非空值；未保留经营数值', '空值不代表该定义不适用'],
  ])('行到列定义关系单独展示依据 %s', async (basis, title, observation, note) => {
    const column = { source_id: 'facts', schema: 'public', table: 'observations', column: 'comparison_total', evidence_refs: ['e-column'] }
    api.request.mockResolvedValue({ facts: [{ id: 'edge1', fact_kind: 'Edge', status: 'accepted', payload: { source: 'a', target: 'b', relation: 'uses_measure' } }],
      supports: [{ id: 'pair', fact_id: 'edge1', status: 'accepted', origin: 'source_query', source_binding: 'facts.public.observations', scope_ref: 'public',
        binding: { metadata_endpoint: { endpoint: 'target', basis, column, meaning_origin: 'agent_declared', row_membership: 'observed_projection', non_null_observed: basis === 'observed_non_null' } } }],
      rules: [], next_offset: null, supports_truncated: false })
    mount(GraphCanvas, { graph, layer: 'business', projectId: 'p' })
    root.querySelector('.graph-node')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('.metadata-relation-basis')).not.toBeNull())
    const values = root.querySelector('.metadata-relation-basis .property-table')?.textContent
    expect(values).toContain(title); expect(values).toContain(observation)
    expect(values).toContain('comparison_total'); expect(values).toContain('e-column')
    expect(values).toContain('来自实际投影行'); expect(values).toContain('Agent 依据材料声明')
    expect(root.querySelector('.metadata-relation-basis')?.textContent).toContain(note)
    expect(root.querySelector('.support-detail .property-table')?.textContent).toContain('事实数据查询')
  })
  it('来源详情展示位置与同一行路径上下文，不要求展开原始JSON', async () => {
    api.request.mockResolvedValue({ facts: [{ id: 'a', fact_kind: 'NodeExistence', status: 'accepted', payload: { entity_id: 'a', kind: '报表项' } }],
      supports: [{ id: 's1', fact_id: 'a', status: 'accepted', origin: 'source_query', source_binding: 's.public.paths', scope_ref: 'public',
        binding: { constant_properties: { position: 'slot_b' }, row_context: { id: 'row-1', values: { path: 'path_7', parent: 'P10', child: 'P20' } } } }],
      rules: [], next_offset: null, supports_truncated: false, total_incident_edges: 5 })
    mount(GraphCanvas, { graph, layer: 'business', projectId: 'p' })
    root.querySelector('.graph-node')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('.support-detail')?.textContent).toContain('同一行路径上下文'))
    const tables = [...root.querySelectorAll('.support-detail .property-table')].map(n => n.textContent).join(' ')
    expect(tables).toContain('slot_b'); expect(tables).toContain('path_7'); expect(tables).toContain('P10'); expect(tables).toContain('P20')
    expect(root.querySelector('[aria-label="节点关联范围"]')?.textContent).toContain('当前授权图中共有 5 条')
  })
  it('主名称按宽度换行，详情保留全名，查找证据单独显示', async () => {
    const name = '经营活动产生的短期合同资产原值合计'
    api.request.mockResolvedValue({ facts: [{ id: 'a', fact_kind: 'NodeExistence', status: 'accepted', payload: { entity_id: 'a' } }],
      supports: [{ id: 's', fact_id: 'a', status: 'accepted', origin: 'source_query', source_binding: 'orders.public.order', scope_ref: 'public',
        binding: { lookup_matches: [{ endpoint: 'target', source: 'crm.public.customers', input_columns: ['buyer_mail'], input_values: ['a@example.test'], match_columns: ['email'], key_columns: ['customer_id'], resolved_key: ['C02'], scope_ref: 'public' }] } }],
      rules: [], next_offset: null, supports_truncated: false })
    mount(GraphCanvas, { graph: { nodes: [{ id: 'a', label: name, kind: 'Metric', status: 'accepted' }], edges: [] }, layer: 'business', projectId: 'p' })
    const node = root.querySelector('.graph-node')!
    expect(node.querySelectorAll('.node-label tspan')).toHaveLength(2)
    expect(node.querySelector('.node-label')?.textContent).toBe(name)
    node.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(root.textContent).toContain('记录匹配依据'))
    expect(root.querySelector('.detail-name')?.textContent).toBe(name)
    const table = root.querySelector('.support-detail')?.textContent
    expect(table).toContain('crm.public.customers'); expect(table).toContain('C02'); expect(table).toContain('a@example.test')
  })
  it('业务身份用于定位，可见范围与来源支持不混入业务属性', async () => {
    mount(GraphCanvas, { graph: { nodes: [{ id: 'hash', label: '采购物流成本', kind: '报表项', status: 'accepted',
      identity: { 经营主体: '鲜果连锁', 报表项编码: 'RTL_PNL_1410' }, aliases: ['Procurement Logistics'],
      properties: { 单位: ['元'] }, scopes: ['public'], provenance: [{ secret_id: 'source-marker' }], supports: [{ fact_id: 'support-marker' }],
    }], edges: [] }, layer: 'business' })
    const node = root.querySelector('.graph-node')!
    expect(node.querySelector('title')?.textContent).toContain('经营主体：鲜果连锁')
    node.dispatchEvent(new MouseEvent('click', { bubbles: true })); await nextTick()
    const detail = root.querySelector('.graph-detail')!
    expect(detail.textContent).toContain('业务身份')
    expect(detail.textContent).toContain('可见范围：public')
    const tables = [...detail.querySelectorAll('.property-table')].map(table => table.textContent).join(' ')
    expect(tables).toContain('RTL_PNL_1410')
    expect(tables).not.toContain('source-marker')
    expect(tables).not.toContain('support-marker')
    const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!
    for (const value of ['鲜果连锁', 'rtl_pnl_1410', 'procurement logistics']) {
      input.value = value; input.dispatchEvent(new Event('input', { bubbles: true })); await nextTick()
      expect(root.querySelectorAll('.graph-node:not(.dimmed)')).toHaveLength(1)
    }
  })

  it('组合节点显示已验证端点名称，保留原始属性并支持画布内搜索', async () => {
    const caption = '主体=A · 指标=占用 [M0] · 产品=梨 [P1]'
    mount(GraphCanvas, { graph: { nodes: [
      { id: 'joint', label: 'A/M0/P1', display_label: caption, kind: 'Assignment', status: 'accepted' },
      { id: 'other', label: '其他', kind: 'Assignment', status: 'accepted' },
    ], edges: [] }, layer: 'business' })
    const node = root.querySelector('.graph-node')!
    expect(node.getAttribute('aria-label')).toBe(caption)
    expect(node.querySelector('title')?.textContent).toContain(caption)
    node.dispatchEvent(new MouseEvent('click', { bubbles: true })); await nextTick()
    expect(root.querySelector('.detail-name')?.textContent).toBe(caption)
    expect(root.querySelector('.detail-code')?.textContent).toContain('A/M0/P1')
    const input = root.querySelector<HTMLInputElement>('[aria-label="搜索图节点"]')!
    input.value = '占用'; input.dispatchEvent(new Event('input', { bubbles: true })); await nextTick()
    expect(root.querySelectorAll('.graph-node:not(.dimmed)')).toHaveLength(1)
  })
  it('同名节点同时展示状态和范围，长范围保留完整悬浮说明', () => {
    const longScope = '集团合并、区域分拨、批发中心、配送服务、冷链物流'
    mount(GraphCanvas, { graph: { nodes: [
      { id: 'accepted', label: '销售收入', kind: 'ReportItem', status: 'accepted', scopes: ['集团合并'] },
      { id: 'candidate', label: '销售收入', kind: 'ReportItem', status: 'candidate', scopes: ['批发中心'] },
      { id: 'unresolved', label: '销售收入', kind: 'ReportItem', status: 'unresolved', scopes: [longScope] },
    ], edges: [] }, layer: 'business' })
    const nodes = [...root.querySelectorAll('.graph-node')]
    expect(nodes.map(node => node.querySelector('.node-kind')?.textContent)).toEqual([
      '报表项 · 已接受', '报表项 · 候选', '报表项 · 未决',
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
    expect(reads).toHaveLength(3)
    expect(reads[1][0]).toContain('offset=0')
    expect(reads[2][0]).toContain('focus=fresh')
    expect(new URL(reads[2][0], 'http://test').searchParams.has('q')).toBe(false)
    expect(root.textContent).toContain('匹配 1 个节点')
    expect(root.textContent).toContain('全图 1205')
  })
  it('错误建议区分业务未决和审查协议错误，未知代码可见但不透传异常正文', () => {
    mount(RunIssues, { report: { issues: [
      { code: 'semantic_claim_unresolved', table: 'public.items' },
      { code: 'semantic_claim_unresolved', table: 'public.items' },
      { code: 'semantic_review_protocol_invalid' },
      { code: 'unknown', message: '不应显示的任意正文' },
    ] } })
    expect(root.querySelectorAll('.note')).toHaveLength(3)
    expect(root.textContent).toContain('unknown')
    expect(root.textContent).toContain('暂未提供专门说明')
    expect(root.textContent).toContain('现有证据不能确认业务含义')
    expect(root.textContent).toContain('不代表业务主张已被否定')
    expect(root.textContent).not.toContain('不应显示的任意正文')
  })
  it.each([
    ['excluded_column_projected', '提案使用了已排除的列'],
    ['model_response_truncated', '模型回复未完整返回'],
    ['model_context_window_exceeded', 'token 窗口'],
    ['model_reference_collision', '短引用发生映射冲突'],
    ['unexplained_columns', '部分字段尚未被规则解释'],
    ['concept_scope_evidence_required', '概念的共享范围缺少证据'],
    ['semantic_review_unissued_evidence', '本次未签发或重复的引用'],
    ['concept_key_label_conflict', '同一概念编码对应了不同名称'],
    ['concept_binding_conflict', '同一联合上下文对应了不同的概念组合'],
    ['recovery_state_invalid', '历史恢复约束存在程序错误'],
    ['semantic_recovery_shape_changed', '恢复提案改变了保留的身份、范围或位置约束'],
    ['semantic_recovery_obligation_unresolved', '审查已完成，但现有证据仍不能支持新结构'],
  ])('为实际错误 %s 显示固定建议并隐藏任意错误正文', (code, guidance) => {
    mount(RunIssues, { report: { issues: [{ code, table: 'public.items', message: '秘密源值不能展示' }] } })
    expect(root.querySelectorAll('.note')).toHaveLength(1)
    expect(root.textContent).toContain('public.items')
    expect(root.textContent).toContain(guidance)
    expect(root.textContent).not.toContain('秘密源值不能展示')
  })
  it('只展示受控的问题类别与下一步操作，程序错误不能提示补业务证据', () => {
    mount(RunIssues, { report: { issues: [
      { code: 'recovery_state_invalid', category: 'internal_error', next_action: 'fix_code' },
      { code: 'semantic_claim_refuted', category: 'semantic_decision', next_action: 'continue_other_candidates' },
      { code: 'semantic_claim_unresolved', category: '秘密类别', next_action: '秘密操作' },
    ] } })
    expect(root.textContent).toContain('问题类型：程序错误')
    expect(root.textContent).toContain('处理：修复代码后续跑')
    expect(root.textContent).toContain('问题类型：业务判断')
    expect(root.textContent).toContain('处理：继续处理其他候选')
    expect(root.textContent).not.toContain('秘密')
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
  it('区分已拒绝、分析未决和执行中断，保留各自状态', () => {
    mount(RunIssues, { report: { issues: [
      { code: 'relation_endpoint_kind_mismatch', table: 'public.items', status: 'rejected' },
      { code: 'relation_discovery_unresolved', table: 'public.items', status: 'unresolved' },
      { code: 'relation_discovery_pending', table: 'public.other', status: 'deferred' },
    ] } })
    const notes = [...root.querySelectorAll('.note')]
    expect(notes).toHaveLength(3)
    expect(notes[0]!.textContent).toContain('已拒绝，提案已结束')
    expect(notes[0]!.classList.contains('amber')).toBe(false)
    expect(notes[1]!.textContent).toContain('关系探索已结束')
    expect(notes[2]!.textContent).toContain('工作尚未完成')
  })
  it('协议诊断区分阶段，过程记录不会显示为未完成', () => {
    mount(RunIssues, { report: { issues: [
      { code: 'model_protocol_diagnostic', status: 'diagnostic', stage: 'provider_usage', field_path: '["usage","prompt_tokens"]', constraint: 'invalid_usage', count: 2 },
      { code: 'model_protocol_diagnostic', status: 'diagnostic', stage: 'repair_contract', field_path: '[]', constraint: 'meaning_changed', count: 1 },
      { code: 'llm_schema_invalid', status: 'deferred' },
    ] } })
    const notes = [...root.querySelectorAll('.note')]
    expect(notes).toHaveLength(3)
    expect(notes[0]!.textContent).toContain('阶段：用量字段')
    expect(notes[0]!.textContent).toContain('usage.prompt_tokens')
    expect(notes[0]!.classList.contains('amber')).toBe(false)
    expect(notes[1]!.textContent).toContain('阶段：格式修复结果')
    expect(notes[2]!.textContent).toContain('已发布的结果继续保留')
  })
  it('请求重试与最终任务状态分开显示', () => {
    mount(RunIssues, { report: { status: 'COMPLETED', counts: {
      request_retry_attempts: 2, request_retry_succeeded: 2, request_retry_exhausted: 0,
    }, issues: [{ code: 'model_protocol_diagnostic', stage: 'response_json',
      field_path: '[]', constraint: 'json_invalid', count: 2, status: 'diagnostic' }] } })
    expect(root.querySelector('[aria-label="请求重试"]')!.textContent).toContain('2 个请求恢复成功')
    expect(root.textContent).toContain('任务状态按最终结果判断')
    expect(root.querySelectorAll('.amber')).toHaveLength(0)
  })
  it('覆盖问题展示受控列名，其他问题不接受任意列名正文', () => {
    mount(RunIssues, { report: { issues: [
      { code: 'unexplained_columns', table: 'public.items', count: 3, column_names: '["指标名称","产品名称","渠道名称"]', status: 'unresolved' },
      { code: 'coverage_shared_label_unresolved', column_names: '["产品名称"]', status: 'unresolved' },
      { code: 'semantic_claim_unresolved', column_names: '["秘密源值"]' },
      { code: 'unexplained_columns', table: 'public.other', column_names: '{"secret":"秘密源值"}' },
    ] } })
    expect(root.textContent).toContain('相关列：指标名称、产品名称、渠道名称')
    expect(root.textContent).toContain('重复名称列尚未通过共享编码核验')
    expect(root.textContent).not.toContain('秘密源值')
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
  it('关联列表一次读取页外端点名称，属性与来源默认按表展示，技术ID可展开', async () => {
    const edge = { source: 'long_source_hash', target: 'long_target_hash', relation: 'custom_relation', qualifiers: { source_level: 1, target_level: 2 } }
    api.request.mockResolvedValue({
      facts: [{ id: 'edge_fact_hash', fact_kind: 'Edge', payload: edge, status: 'accepted' }],
      nodes: [{ id: 'long_source_hash', label: '经营利润' }, { id: 'long_target_hash', label: '销售收入' }],
      display_schema: { relations: [{ name: 'custom_relation', description: '经营支撑关系' }] },
      supports: [], rules: [], next_offset: null, supports_truncated: false,
    })
    mount(GraphCanvas, { graph: { nodes: [{ id: 'long_source_hash', label: '经营利润', kind: 'ReportItem', properties: { business_code: 'M01', english_name: ['Operating profit'], source_position: [1, 2] } }], edges: [] }, layer: 'business', projectId: 'p' })
    root.querySelector('.graph-node')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(root.querySelector('.relation-selection')?.textContent).toContain('销售收入'))
    expect(api.request).toHaveBeenCalledTimes(1)
    expect(root.querySelector('.relation-selection')?.textContent).toContain('经营支撑关系')
    expect(root.querySelector('.relation-selection')?.textContent).not.toContain('long_')
    const table = root.querySelector('.graph-detail .property-table')!
    expect(table.textContent).toContain('业务编码'); expect(table.textContent).toContain('M01')
    expect(table.textContent).toContain('英文名'); expect(table.textContent).toContain('Operating profit')
    expect(table.textContent).toContain('来源层位'); expect(table.textContent).not.toContain('long_source_hash')
    expect([...root.querySelectorAll('.graph-detail pre')].every(node => !!node.closest('details:not([open])'))).toBe(true)
    ;(root.querySelector('.relation-selection') as HTMLButtonElement).click()
    await vi.waitFor(() => expect(root.querySelector('.graph-detail h2[title]')?.textContent).toBe('经营支撑关系'))
    expect(root.querySelector('.relation-endpoints')?.textContent).toBe('经营利润 → 销售收入')
    expect(root.querySelector('.graph-detail .property-table')?.textContent).toContain('来源层位')
    expect(root.querySelector('.graph-detail .technical-details')?.textContent).toContain('long_target_hash')
  })
  it('未知关系不猜测中文语义，明确提示缺少定义；长定义缩短显示并保留完整说明', async () => {
    const description = '这是一个来自项目定义的较长关系说明，用来说明指标与业务对象之间的关系含义'
    mount(GraphCanvas, { graph: { ...graph, edges: [{ source: 'a', target: 'b', relation: 'unknown_relation' }] }, layer: 'business' })
    root.querySelector('.graph-edge')!.dispatchEvent(new MouseEvent('click', { bubbles: true })); await nextTick()
    expect(root.querySelector('.graph-detail h2[title]')?.textContent).toBe('unknown_relation（未提供中文名）')
    app.unmount()
    app = createApp(GraphCanvas, { graph: { ...graph, display_schema: { relations: [{ name: 'knows', description }] } }, layer: 'business' }); app.mount(root)
    root.querySelector('.graph-edge')!.dispatchEvent(new MouseEvent('click', { bubbles: true })); await nextTick()
    expect(root.querySelector('.graph-detail h2[title]')?.getAttribute('title')).toBe(description)
    expect(root.querySelector('.graph-detail h2[title]')?.textContent?.length).toBeLessThanOrEqual(24)
    expect(root.querySelector('.graph-detail')?.textContent).toContain(description)
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
