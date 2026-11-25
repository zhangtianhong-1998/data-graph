// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App as VueApp } from 'vue'
import App from './App.vue'
import { clearSessions } from './api'
import type { Project, RetrievalStatusResponse, RunConfig } from './types'

let app: VueApp | undefined
let root: HTMLDivElement
let config: RunConfig
let revision: number
let vectorizedState: RetrievalStatusResponse
let retrievalFixture: Record<string, unknown> | undefined
const requests: Array<{ url: string; method: string; body?: string; headers: Headers }> = []

const project: Project = {
  project_id: 'wf_proj',
  name: '并行工作流测试项目',
  created_at: '2026-11-15',
  updated_at: '2026-11-15',
  files: [
    {
      file_id: 'f_1_doc_ab',
      name: 'doc_ab.md',
      kind: 'markdown',
      relative_path: 'files/f_1_doc_ab.md',
      size: 120,
      purpose: 'analysis_knowledge',
      scope: { source_ids: [], tables: [], columns: [] },
    },
    {
      file_id: 'f_2_doc_ac',
      name: 'doc_ac.md',
      kind: 'markdown',
      relative_path: 'files/f_2_doc_ac.md',
      size: 160,
      purpose: 'data_description',
      scope: { source_ids: [], tables: [], columns: [] },
    },
  ],
}

const otherProject: Project = {
  project_id: 'base_proj',
  name: '历史基准图谱',
  created_at: '2026-11-10',
  updated_at: '2026-11-10',
  files: [],
}

const response = (value: unknown, status = 200, etag?: string) =>
  new Response(JSON.stringify(value), { status, headers: etag ? { ETag: etag } : {} })

function button(label: string) {
  const el = [...root.querySelectorAll('button')].find(b => b.textContent?.trim() === label)
  if (!el) throw new Error(`Missing button: ${label}`)
  return el
}

async function mountReady() {
  root = document.createElement('div')
  document.body.append(root)
  app = createApp(App)
  app.mount(root)
  await vi.waitFor(() => expect(root.querySelector('.page-empty')).toBeNull())
}

beforeEach(() => {
  clearSessions()
  retrievalFixture = undefined
  sessionStorage.clear()
  requests.length = 0
  revision = 1
  vectorizedState = {
    status: 'not_vectorized',
    vectorized: false,
    stale_reason: 'index_missing',
    has_graph: true,
    graph_nodes: 2,
    graph_edges: 1,
    configured_provider: 'openai_compatible',
    configured_model: 'Qwen/Qwen3-Embedding-0.6B',
    configured_base_url: 'http://127.0.0.1:8002/v1',
    vector_dim: null,
    vector_cards: null,
    nodes_indexed: null,
    edges_indexed: null,
    embedder_model: null,
    embedder_provider: null,
    synced_at: null,
    generation: 'gen-1',
    config_error: null,
  }
  config = {
    goal: { purpose: '提取业务实体与关系', entity_kinds: [], relation_kinds: [] },
    goal_markdown: '# 提取业务实体与关系',
    scopes: ['public'],
    sources: [
      { id: 'db1', kind: 'gauss_database', allowed_tables: ['dm.table_a', 'dm.table_b', 'dm.table_c'] },
    ],
    documents: ['files/f_1_doc_ab.md', 'files/f_2_doc_ac.md'],
    model: { mode: 'disabled' },
    embedding: { provider: 'none', base_url: '', model: '' },
    inherited_graphs: [],
    workflow: null,
  }

  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const method = options.method || 'GET'
    const headers = new Headers(options.headers)
    const body = typeof options.body === 'string' ? options.body : undefined
    requests.push({ url, method, body, headers })

    if (url === '/api/projects') return response({ projects: [project, otherProject] })
    if (url.endsWith('/api/projects/wf_proj')) return response(project)
    if (url.endsWith('/session')) return response({ session_id: 'sess-1' })
    if (url.endsWith('/config')) {
      if (method === 'PUT') {
        config = JSON.parse(body!)
        revision++
      }
      return response(config, 200, `"${revision}"`)
    }
    if (url.endsWith('/credentials')) return response({ database_sources: [], model: false })
    if (url.endsWith('/credentials/embedding')) {
      if (method === 'PUT') return response({ embedding: true })
      if (method === 'DELETE') return response({ embedding: false })
      return response({ embedding: false })
    }
    if (url.endsWith('/metadata') || url.endsWith('/metadata/refresh')) {
      return response({
        nodes: [],
        edges: [],
        tables: [
          { source_id: 'db1', schema: 'dm', name: 'table_a', columns: [] },
          { source_id: 'db1', schema: 'dm', name: 'table_b', columns: [] },
          { source_id: 'db1', schema: 'dm', name: 'table_c', columns: [] },
        ],
      })
    }
    if (url.includes('/graph/identity-candidates')) return response({ candidates: [], decisions: [], revision: 0, total_candidates: 0, total_decisions: 0 })
    if (url.includes('/graph?')) {
      return response({
        nodes: [
          { id: 'n1', label: '华东设备采购合同', kind: 'Contract', layer: 'business', status: 'accepted' },
          { id: 'n2', label: '结算付款单', kind: 'Settlement', layer: 'business', status: 'accepted' },
        ],
        edges: [{ id: 'e1', source: 'n1', target: 'n2', relation: 'SETTLED_BY', status: 'accepted' }],
      })
    }
    if (url.endsWith('/runs') && method === 'POST') return response({ run_id: 'run_canvas', project_id: 'wf_proj', status: 'completed', stage: 'finished', created_at: '2026-11-15T00:00:00Z', cleanup_status: 'completed', workflow: true }, 202)
    if (url.endsWith('/runs?view=summary') || url.endsWith('/runs/run_parent_1?view=workbench')) {
      const header = {
        run_id: 'run_parent_1',
        project_id: 'wf_proj',
        status: 'completed_with_gaps',
        stage: 'finished',
        created_at: '2026-11-15T00:00:00Z',
        cleanup_status: 'completed',
        workflow: true,
      }
      if (url.endsWith('?view=summary')) return response({ runs: [header] })
      return response({
        ...header,
        current_workflow_run_id: 'run_retry_u2',
        workflow_state: {
          parent_run_id: 'run_parent_1',
          status: 'completed_with_gaps',
          units: {
            unit_1: { unit_id: 'unit_1', label: 'A+B 链路', status: 'completed', run_id: 'run_u1', nodes: 12, edges: 8 },
            unit_2: { unit_id: 'unit_2', label: 'A+C 链路', status: 'failed', run_id: 'run_u2', error_code: 'run_time_budget_exhausted', nodes: 0, edges: 0 },
          },
          merge_nodes: {
            merge_1: { node_id: 'merge_1', label: '全节点合并抽取', merge_mode: 'merge_and_extract', status: 'completed', run_id: 'run_m1',
              nodes: 20, edges: 14, merge_counts: { input_nodes: 16, output_nodes: 10,
                source_replay_status: 'ran', source_replay_collapsed_nodes: 0, hierarchy_replay_collapsed_nodes: 4, observed_dictionary_collapsed_nodes: 2 },
              delta_merge_counts: { source_replay_status: 'ran', source_replay_collapsed_nodes: 2, input_edges: 15, output_edges: 14,
                output_directed_endpoint_pairs: 10, output_multi_edge_pairs: 3, output_endpoint_pair_excess: 4, output_self_loop_edges: 1 } },
            merge_2: { node_id: 'merge_2', label: '确定性合并', merge_mode: 'deterministic', status: 'completed', run_id: null },
            reviewed: { node_id: 'reviewed', label: '并行子图融合', merge_mode: 'overlap_review', status: 'completed',
              overlap_review: { status: 'partial', reason: 'model_response_truncated', pattern_count: 7, eligible_patterns: 2, submitted_patterns: 6, reviewed_patterns: 5,
                failed_patterns: 1, omitted_patterns: 3, analysis_omitted_patterns: 2, selected_patterns: 1,
                proposed_patterns: 3, verified_proposal_groups: 5, applied_proposal_groups: 4, proposal_collapsed_nodes: 6,
                proposed_relation_patterns: 2, verified_relation_facts: 3, grouped_relation_edges: 7,
                added_entity_rules: 2, model_calls: 2 } },
            merge_3: { node_id: 'merge_3', label: '从产物恢复', merge_mode: 'merge_and_extract', status: 'completed', run_id: 'run_old' },
          },
          recomposed_nodes: ['merge_3'],
          merge: { status: 'completed', nodes: 12, edges: 8, candidate_identity_merges: 1, candidate_column_alignments: 2 },
        }
      })
    }
    if (url.endsWith('/workflow')) {
      return response({
        enabled: true,
        units: ['unit_1', 'unit_2'],
        state: {
          parent_run_id: 'run_retry_u2',
          status: 'running',
          units: {
            unit_1: { unit_id: 'unit_1', label: 'A+B 链路', status: 'completed', run_id: 'run_u1', nodes: 12, edges: 8 },
            unit_2: { unit_id: 'unit_2', label: 'A+C 链路', status: 'queued', run_id: 'run_retry_u2', nodes: 0, edges: 0 },
          },
          merge_nodes: {
            merge_1: { node_id: 'merge_1', label: '全节点合并抽取', merge_mode: 'merge_and_extract', status: 'queued', nodes: 0, edges: 0 },
          },
          merge: { status: 'pending' },
        },
        merge_summary: null,
      })
    }
    if (url.endsWith('/workflow/units/unit_2/retry')) {
      return response({
        run_id: 'run_retry_u2',
        project_id: 'wf_proj',
        status: 'completed',
        stage: 'finished',
        created_at: '2026-11-15T00:01:00Z',
        cleanup_status: 'completed',
        workflow: true,
      }, 202)
    }
    if (url.endsWith('/workflow/merge')) {
      return response({ status: 'completed', nodes: 20, edges: 14 })
    }
    if (url.endsWith('/retrieval/embedding/test')) {
      return response({
        status: 'ok',
        provider: 'openai_compatible',
        base_url: 'http://127.0.0.1:8002/v1',
        model: 'Qwen/Qwen3-Embedding-0.6B',
        dimensions: 1024,
        latency_ms: 14.2,
      })
    }
    if (url.endsWith('/retrieval/status')) {
      return response(vectorizedState)
    }
    if (url.endsWith('/retrieval/jobs')) {
      if (method === 'GET') return response({ status: vectorizedState.indexed || vectorizedState.vectorized ? 'completed' : 'idle' })
      if (vectorizedState.configured_provider === 'none') {
        Object.assign(vectorizedState, { status: 'indexed', indexed: true, vectorized: false, stale_reason: null })
        return response({ status: 'completed', job_id: 'lexical-build' })
      }
      vectorizedState = {
        ...vectorizedState,
        status: 'vectorized',
        vectorized: true,
        stale_reason: null,
        vector_dim: 1024,
        vector_cards: 2,
        nodes_indexed: 2,
        edges_indexed: 1,
        embedder_model: 'Qwen/Qwen3-Embedding-0.6B',
        embedder_provider: 'openai_compatible',
        synced_at: '2026-11-15T01:05:00Z',
      }
      return response({
        status: 'running',
        vectorized: true,
        generation: 'gen-1',
        build: 'b-1',
        nodes_indexed: 2,
        edges_indexed: 1,
        vector_cards: 2,
        vector_dim: 1024,
        embedder_model: 'Qwen/Qwen3-Embedding-0.6B',
        synced_at: '2026-11-15T01:05:00Z',
      })
    }
    if (url.split('?')[0]?.endsWith('/retrieval/query')) {
      const payload = JSON.parse(body || '{}')
      if (retrievalFixture) return response({
        query: payload.query, scopes: ['public'], slots: [], nodes: [], edges: [], bridge_nodes: [],
        connected_components: [], truncated: false, truncation_reasons: [], ...retrievalFixture,
      })
      const drilldown = payload.mode === 'drilldown' ? {
        status: 'found', equally_ranked_paths: 2, uncovered_seed_ids: [],
        paths: [{ node_ids: ['n1', 'n2'], edge_ids: ['e1'], business_context: { status: 'scoped', dimensions: { project: 'M' } }, stop_reason: 'target_reached' }],
      } : undefined
      return response({
        drilldown,
        ...(payload.query === '华东收入' ? {
          plan: {intent: 'dimensional', conditions: [{text: '华东', role: '地区'},
            {text: '收入', role: '指标', match: 'semantic', candidates: [{display_name: '营业收入'}]}], excluded_terms: [], unresolved: []},
          dimensional: {status: 'no_common_fact', children: [], fact_ids: [], witnesses: [], matched_conditions: []},
        } : {}),
        query: '采购合同 结算单',
        scopes: ['public'],
        slots: [
          {
            surface: '采购合同',
            source: 'chunk',
            candidates: payload.node_id ? [{ node_id: 'n1', kind: 'Contract', vec_score: null, vec_rank: null, lex_rank: null, rrf_score: null, match_type: 'exact_id' }] : [{ node_id: 'n1', kind: 'Contract', vec_score: 0.91, vec_rank: 1, lex_rank: 1, rrf_score: 0.032, match_type: 'exact_name' }],
          },
        ],
        bridge_nodes: [{ node_id: 'n_mid', display_name: '结算中心', kind: 'Org', bridge_role: 'intermediate_bridge', matched_slots: ['采购合同', '结算单'] }],
        nodes: [
          { id: 'n1', display_name: '华东设备采购合同', kind: 'Contract', english_name: 'east_contract', identity: { code: 'HT-01' }, is_seed: true },
          { id: 'n2', display_name: '结算付款单', kind: 'Settlement', english_name: 'settlement', identity: { code: 'JS-99' }, is_seed: false },
        ],
        edges: [{ id: 'e1', relation: 'SETTLED_BY', source: 'n1', target: 'n2' }],
        connected_components: [['n1', 'n2']],
        truncated: false,
        truncation_reasons: [],
      })
    }
    throw new Error(`Unexpected ${method} ${url}`)
  }))
})

afterEach(() => {
  app?.unmount()
  root?.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  clearSessions()
  sessionStorage.clear()
})

describe('拖拽式工作流编排、可替换本地向量服务与手动延迟向量化', () => {
  it('可在画布编排抽取节点（直出）与添加合并节点（合并抽取），分别配置 A+B / A+C 表与各自知识库', async () => {
    await mountReady()
    button('并行工作流').click()
    await vi.waitFor(() => expect(root.querySelector('.wf-canvas')).not.toBeNull())

    const addButtons = [...root.querySelectorAll<HTMLButtonElement>('.palette-item button')]
    expect(addButtons).toHaveLength(2)

    // 1. 添加 2 个抽取节点（此时无合并节点，直接输出到最终图谱）
    addButtons[0]!.click()
    await nextTick()
    addButtons[0]!.click()
    await nextTick()
    expect(root.querySelectorAll('.wf-node-extract')).toHaveLength(2)
    expect(root.querySelectorAll('.wf-node-merge')).toHaveLength(0)
    expect(root.textContent).toContain('未放置合并节点')

    // 选中第 1 个抽取节点：配置 dm.table_a, dm.table_b + 仅 doc_ab.md
    const extNodes = [...root.querySelectorAll<HTMLElement>('.wf-node-extract')]
    extNodes[0]!.click()
    await nextTick()
    let inspChecks = [...root.querySelectorAll<HTMLInputElement>('.wf-inspector .source-box input[type="checkbox"]')]
    expect(inspChecks.every(input => input.checked)).toBe(true)
    inspChecks[2]!.click() // exclude C, keep A+B
    const radios1 = [...root.querySelectorAll<HTMLInputElement>('.wf-inspector input[type="radio"]')]
    radios1[1]!.click()
    await nextTick()
    const docChecks1 = [...root.querySelectorAll<HTMLInputElement>('.wf-inspector .inspector-doc-list input[type="checkbox"]')]
    docChecks1[1]!.click() // uncheck doc_ac.md
    await nextTick()

    // 选中第 2 个抽取节点：配置 dm.table_a, dm.table_c + 仅 doc_ac.md
    extNodes[1]!.click()
    await nextTick()
    inspChecks = [...root.querySelectorAll<HTMLInputElement>('.wf-inspector .source-box input[type="checkbox"]')]
    inspChecks[1]!.click() // exclude B, keep A+C
    const radios2 = [...root.querySelectorAll<HTMLInputElement>('.wf-inspector input[type="radio"]')]
    radios2[1]!.click()
    await nextTick()
    const docChecks2 = [...root.querySelectorAll<HTMLInputElement>('.wf-inspector .inspector-doc-list input[type="checkbox"]')]
    docChecks2[0]!.click() // uncheck doc_ab.md
    await nextTick()

    // 2. 添加 1 个合并节点：自动把两个抽取节点重新路由汇聚到合并节点，并切换为「全节点合并抽取」
    addButtons[1]!.click()
    await nextTick()
    expect(root.querySelectorAll('.wf-node-merge')).toHaveLength(1)
    const modeSelect = root.querySelector<HTMLSelectElement>('#inspector-merge-mode-0')!
    expect(modeSelect.value).toBe('overlap_review')
    expect(modeSelect.querySelector('option[value="deterministic"]')).not.toBeNull()
    modeSelect.value = 'merge_and_extract'
    modeSelect.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(root.textContent).toContain('合并后用模型补充跨分支关系')

    root.querySelectorAll<HTMLInputElement>('.wf-inspector input[type=radio]')[1]!.click()
    await nextTick()
    root.querySelectorAll<HTMLInputElement>('.inspector-doc-list input')[1]!.click()
    await nextTick()
    root.querySelectorAll<HTMLInputElement>('.wf-inspector .source-box input')[0]!.click()
    const budget = root.querySelector<HTMLInputElement>('#inspector-merge-0-calls')!
    budget.value = '17'
    budget.dispatchEvent(new Event('input', { bubbles: true }))
    expect(root.querySelector('#merge-conflict-mode')).toBeNull()
    expect(root.querySelector('#merge-shared-types')).toBeNull()
    await nextTick()

    button('保存工作流配置').click()
    await vi.waitFor(() => expect(requests.some(r => r.method === 'PUT' && r.url.endsWith('/config'))).toBe(true))

    expect(config.workflow?.enabled).toBe(true)
    expect(config.workflow?.units).toHaveLength(2)
    expect(config.workflow?.units[0]?.source_table_overrides.db1).toEqual(['dm.table_a', 'dm.table_b'])
    expect(config.workflow?.units[0]?.document_ids).toEqual(['files/f_1_doc_ab.md'])
    expect(config.workflow?.units[1]?.source_table_overrides.db1).toEqual(['dm.table_a', 'dm.table_c'])
    expect(config.workflow?.units[1]?.document_ids).toEqual(['files/f_2_doc_ac.md'])
    expect(config.workflow?.merge_nodes).toHaveLength(1)
    expect(config.workflow?.merge_nodes?.[0]?.merge_mode).toBe('merge_and_extract')
    expect(config.workflow?.merge_nodes?.[0]).toMatchObject({
      max_calls: 17, document_ids: ['files/f_1_doc_ab.md'], source_table_overrides: { db1: ['dm.table_b', 'dm.table_c'] },
    })
    expect(config.workflow?.merge_policy).toBeUndefined()
    expect(config.workflow?.merge_nodes?.[0]?.merge_policy).toBeUndefined()
    expect(config.workflow?.edges?.map(e => [e.source, e.target])).toEqual([
      ['extract_1', 'merge_1'], ['extract_2', 'merge_1'], ['merge_1', 'output'],
    ])

    // 3. 等待保存完成解锁后，测试删除连线与端口点击重新连线，以及一键拆分直出（无合并节点）
    await vi.waitFor(() => expect(button('按表拆分直出').disabled).toBe(false))
    const deleteEdgeBtn = root.querySelector<SVGGElement>('.wf-edge-delete')!
    deleteEdgeBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()
    expect(root.querySelectorAll('.wf-edge-path')).toHaveLength(2)

    // 点击抽取节点1的输出端口 ●，再点击合并节点的输入端口 ○ 重新连线
    root.querySelector<HTMLButtonElement>('.wf-node-extract .wf-port-out')!.click()
    await nextTick()
    root.querySelector<HTMLButtonElement>('.wf-node-merge .wf-port-in')!.click()
    await nextTick()
    expect(root.querySelectorAll('.wf-edge-path')).toHaveLength(3)

    button('按表拆分直出').click()
    await nextTick()
    expect(root.querySelectorAll('.wf-node-extract')).toHaveLength(3)
    expect(root.querySelectorAll('.wf-node-merge')).toHaveLength(0)
  })

  it('真正拖入节点和拖线，保存当前修订后启动运行', async () => {
    await mountReady()
    button('并行工作流').click()
    await vi.waitFor(() => expect(root.querySelector('.wf-canvas')).not.toBeNull())
    const canvas = root.querySelector<HTMLElement>('.wf-canvas')!
    const transfer = new DataTransfer()
    const dragStart = new Event('dragstart', { bubbles: true })
    Object.defineProperty(dragStart, 'dataTransfer', { value: transfer })
    root.querySelector('.palette-item.extract')!.dispatchEvent(dragStart)
    expect(transfer.getData('application/x-lightgraph-node')).toBe('extract')
    const drop = new MouseEvent('drop', { bubbles: true, clientX: 183, clientY: 149 })
    Object.defineProperty(drop, 'dataTransfer', { value: transfer })
    canvas.dispatchEvent(drop)
    await nextTick()
    expect(root.querySelectorAll('.wf-node-extract')).toHaveLength(1)
    root.querySelector<SVGGElement>('.wf-edge-delete')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()
    const source = root.querySelector<HTMLButtonElement>('.wf-port-out')!
    source.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0, clientX: 290, clientY: 160 }))
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 400, clientY: 170 }))
    await nextTick()
    expect(root.querySelector('.wf-edge-preview')).not.toBeNull()
    root.querySelector('[data-wire-target="output"]')!.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
    await nextTick()
    expect(root.querySelectorAll('.wf-edge-path')).toHaveLength(1)
    button('保存并开始运行').click()
    await vi.waitFor(() => expect(requests.some(r => r.method === 'POST' && r.url.endsWith('/runs'))).toBe(true))
    const writes = requests.filter(r => r.method === 'PUT' && r.url.endsWith('/config') || r.method === 'POST' && r.url.endsWith('/runs'))
    expect(writes.map(r => r.headers.get('If-Match'))).toEqual(['"1"', '"2"'])
    expect(config.workflow?.units[0]?.position).toEqual({ x: 75, y: 100 })
    expect(config.workflow?.edges?.map(e => [e.source, e.target])).toEqual([['extract_1', 'output']])
    expect(config.workflow?.merge_nodes).toEqual([])
  })

  it('可在模型配置中一键填入本地 Qwen3-0.6B 服务、测试 1024 维连通性并保存密钥', async () => {
    await mountReady()
    button('模型配置').click()
    await nextTick()

    button('填入本地 Qwen3-0.6B 默认参数').click()
    await nextTick()

    expect(root.querySelector<HTMLSelectElement>('#embedding-provider')!.value).toBe('openai_compatible')
    expect(root.querySelector<HTMLInputElement>('#embedding-base-url')!.value).toBe('http://127.0.0.1:8002/v1')
    expect(root.querySelector<HTMLInputElement>('#embedding-model')!.value).toBe('Qwen/Qwen3-Embedding-0.6B')
    expect(root.querySelector<HTMLInputElement>('#embedding-timeout')!.value).toBe('120')

    button('测试向量服务连通性').click()
    await vi.waitFor(() => expect(root.textContent).toContain('向量维度：1024'))
    expect(JSON.parse(requests.find(r => r.url.endsWith('/retrieval/embedding/test'))!.body!).timeout_seconds).toBe(120)

    const embKeyInput = root.querySelector<HTMLInputElement>('#embedding-key')!
    embKeyInput.value = 'local-emb-key'
    embKeyInput.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    button('保存配置').click()
    await vi.waitFor(() => expect(requests.some(r => r.method === 'PUT' && r.url.endsWith('/credentials/embedding'))).toBe(true))
    expect(config.embedding?.provider).toBe('openai_compatible')
    expect(JSON.stringify(config)).not.toContain('local-emb-key')
  })

  it('从父报告按需读取节点轨迹，切换时取消旧请求', async () => {
    let finishFirst!: (response: Response) => void
    let firstSignal: AbortSignal | null | undefined
    const fetchBase = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
      if (url.includes('/runs/run_parent_1/nodes/unit_1/trace?')) {
        firstSignal = options.signal
        return new Promise<Response>(resolve => { finishFirst = resolve })
      }
      if (url.includes('/runs/run_parent_1/nodes/merge_1/trace?')) {
        return response({ parent_run_id: 'run_parent_1', node_id: 'merge_1', run_id: 'run_m1', reused: true,
          status: 'available', active: false, total_events: 1, next_offset: null,
          events: [{ seq: 53, time: '', kind: 'model_request', data: {} }] })
      }
      return fetchBase(url, options)
    }))
    await mountReady()
    button('运行与结果').click()
    await vi.waitFor(() => expect(root.textContent).toContain('全节点合并抽取'))
    expect(vi.mocked(fetch).mock.calls.every(([url]) => !String(url).includes('/trace?'))).toBe(true)
    const traceButtons = [...root.querySelectorAll('button')].filter(b => b.textContent === '查看节点轨迹')
    expect(traceButtons).toHaveLength(3)
    traceButtons[0]!.click()
    await vi.waitFor(() => expect(firstSignal).toBeDefined())
    traceButtons[2]!.click()
    const panel = () => root.querySelector('[aria-label="节点轨迹"]')
    await vi.waitFor(() => expect(panel()?.textContent).toContain('记录 #53'))
    expect(firstSignal?.aborted).toBe(true)
    finishFirst(response({ parent_run_id: 'run_parent_1', node_id: 'unit_1', run_id: 'run_u1', status: 'available',
      events: [{ seq: 99, kind: 'tool_call', data: {} }], total_events: 1 }))
    await nextTick()
    expect(panel()?.textContent).toContain('本次复用的子运行 run_m1')
    expect(panel()?.textContent).not.toContain('记录 #99')
    expect(panel()?.textContent).not.toMatch(/清理轨迹|导出|进程诊断/)
    expect(button('查看运行轨迹')).toBeDefined()
    button('关闭节点轨迹').click(); await nextTick()
    expect(panel()).toBeNull()
  })

  it('仅建词法索引时启用查询，并隐藏向量匹配', async () => {
    Object.assign(vectorizedState, { configured_provider: 'none', configured_model: '' })
    await mountReady()
    button('运行与结果').click()
    await nextTick()
    button('子图召回').click()
    await vi.waitFor(() => expect(root.textContent).toContain('尚未建立检索索引'))
    button('建立检索索引').click()
    await vi.waitFor(() => expect(root.textContent).toContain('关键词与关系索引已就绪'))
    button('调试检索算子').click()
    await nextTick()
    const selector = root.querySelector<HTMLSelectElement>('#retrieval-method')!
    expect([...selector.options].map(option => option.value)).toEqual(['keyword'])
    const input = root.querySelector<HTMLInputElement>('#retrieval-query')!
    input.value = '采购'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    button('检索').click()
    await vi.waitFor(() => expect(requests.some(r => r.url.endsWith('/retrieval/query')
      && JSON.parse(r.body || '{}').method === 'keyword')).toBe(true))
  })

  it('抽取完成后默认未向量化，用户点击立即向量化后显示已完成向量化并可执行子图召回', async () => {
    await mountReady()
    button('运行与结果').click()
    await nextTick()

    await vi.waitFor(() => expect(root.textContent).toContain('A+B 链路'))
    expect(root.textContent).toContain('全节点合并抽取')
    expect(root.textContent).toContain('上游合并 16 → 10 个节点，同源去重 0 个，同源层级归并 4 个，按字典成员合并 2 个')
    expect(root.textContent).toContain('补充抽取后同源去重 2 个')
    const rows = [...root.querySelectorAll('tr')]
    const current = rows.find(row => row.textContent?.includes('全节点合并抽取'))!
    expect(current.textContent).toContain('补充抽取后合并关系记录 15 → 14 条')
    expect(current.textContent).toContain('有向端点对 10 组，其中 3 组有多条关系')
    const review = rows.find(row => row.textContent?.includes('并行子图融合'))?.textContent
    expect(review).toContain('7 种重叠模式，合并节点的模型审查 5 种，选用整类合并 1 种，生效规则 2 条')
    expect(review).toContain('其中 2 种符合整类合并条件')
    expect(review).toContain('3 个字段与角色对应方案，逐键核实 5 组，实际合并 4 组，减少 6 个节点')
    expect(review).toContain('2 个关系对应方案，核实 3 组同一层级事实，共有 7 条原始关系作依据')
    expect(review).toContain('模型请求 2 次，送审 6 种，已列出但未送审 1 种')
    expect(review).toContain('1 种已送审的模式未取得有效裁决')
    expect(review).toContain('索引分析另省略 2 种模式')
    expect(review).toContain('部分模式未完成审查（模型回复被截断）')
    expect(current.textContent).toContain('每对端点只计一次，记录数相差 4 条。自环 1 条')
    expect(rows.find(row => row.textContent?.includes('确定性合并'))?.textContent).toContain('此运行未记录有向端点统计')

    // 业务图谱页显示「未向量化」，不主动触发 /retrieval/sync
    button('业务图谱').click()
    await vi.waitFor(() => expect(root.textContent).toContain('未向量化'))
    expect(requests.some(r => r.method === 'POST' && r.url.endsWith('/retrieval/jobs'))).toBe(false)

    // 切换到子图召回页：初始状态是「未向量化」，点击「立即向量化」后变成「已完成向量化」
    button('子图召回').click()
    await vi.waitFor(() => expect(root.textContent).toContain('未向量化'))
    await vi.waitFor(() => expect(button('立即向量化')).toBeTruthy())
    button('立即向量化').click()
    await vi.waitFor(() => expect(root.textContent).toContain('已完成向量化'))
    expect(root.textContent).toContain('Qwen/Qwen3-Embedding-0.6B')
    expect(root.textContent).toContain('1024')

    const queryInput = root.querySelector<HTMLInputElement>('#retrieval-query')!
    queryInput.value = '采购合同 结算单'
    queryInput.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    button('检索').click()
    await vi.waitFor(() => expect(root.textContent).toContain('检索结果'))
    expect(root.textContent).toContain('华东设备采购合同')
    expect(root.textContent).toContain('结算中心')
    expect(root.textContent).toContain('SETTLED_BY')
  })
  it('分别展示部分条件的依据、来源原文和未确认的实体候选', async () => {
    vectorizedState = { ...vectorizedState, vectorized: true, status: 'vectorized' }
    await mountReady()
    button('运行与结果').click()
    await nextTick()
    button('子图召回').click()
    await vi.waitFor(() => expect(root.querySelector('#retrieval-query')).not.toBeNull())
    const query = root.querySelector<HTMLTextAreaElement>('#retrieval-query')!
    const emptyContext = { nodes: [], edges: [], witnesses: [], passages: [] }
    const passage = { node_id: 'one', text: '故障代号', display_name: '检修报告', excerpt: '设备报告中提到故障代号。',
      evidence_refs: ['ref-2'], source_tables: ['检修表'] }
    async function search(fixture: Record<string, unknown>, expectedText: string) {
      retrievalFixture = fixture
      query.value = '查找故障代号对应的检修记录'
      query.dispatchEvent(new Event('input', { bubbles: true }))
      await nextTick()
      button('检索').click()
      await vi.waitFor(() => expect(root.querySelector('[aria-label="可用依据"]')?.textContent).toContain(expectedText))
    }
    await search({
      plan: { intent: 'clarify', conditions: [], unresolved: [], excluded_terms: [],
        suggestions: [{ text: '故障代号', reason: 'related', candidates: [{node_id: 'c', display_name: '相似故障'}] }] },
      available_context: { status: 'partial', nodes: [{id: 'a', display_name: '生产车间'}, {id: 'b', display_name: '检修记录'}],
        edges: [{ id: 'e', source: 'b', relation: '发生于', target: 'a' }], passages: [passage],
        witnesses: [{ fact_id: 'b', row_id: 'repair-91', conditions: [{text: '生产车间', node_id: 'a'}] }],
        candidate_path: {assumptions: [{text: '空气压缩机', display_name: '空压机', node_id: 'c', kind: '设备'}],
          unverified_terms: ['空气压缩机'], requested_level: '零件', level_node_ids: ['bearing'],
          nodes: [{id: 'c', display_name: '空压机'}, {id: 'bearing', display_name: '止推轴承'}],
          edges: [{id: 'ce', source: 'c', relation: '包含', target: 'bearing'}],
          witnesses: [{fact_id: 'b', row_id: 'candidate-row', conditions: []}]} },
    }, '只符合部分条件')
    const context = root.querySelector('[aria-label="可用依据"]')!
    expect(context.textContent).toContain('检修记录 → 发生于 → 生产车间')
    expect(context.textContent).toContain('来源行 repair-91')
    expect(context.querySelector('blockquote')?.textContent).toBe(passage.excerpt)
    expect(context.textContent).toContain('来源表 检修表')
    expect(context.textContent).toContain('引用 ref-2')
    const path = context.querySelector('[aria-label="候选路径"]')!
    expect(path.textContent).toContain('“空气压缩机”指“空压机”')
    expect(path.textContent).toContain('假设成立时')
    expect(path.textContent).toContain('空压机 → 包含 → 止推轴承')
    expect(path.textContent).toContain('来源行 candidate-row')
    expect(root.textContent).toContain('相关候选（尚未确认） · 相似故障')
    expect(context.textContent).not.toContain('相似故障')
    expect(root.querySelector('.retrieval-detail-grid')).toBeNull()
    expect(root.querySelector('.retrieval-summary-panel .report-summary')).toBeNull()

    await search({
      nodes: [{id: 'verified', kind: '设备', display_name: '已核实设备'}],
      available_context: { ...emptyContext, status: 'partial', nodes: [{id: 'p', display_name: '部分匹配的设备'}] },
    }, '部分匹配的设备')
    expect(root.querySelector('.retrieval-detail-grid')?.textContent).toContain('已核实设备')
    expect(root.querySelector('.retrieval-detail-grid')?.textContent).not.toContain('部分匹配的设备')
    await search({
      plan: { intent: 'clarify', conditions: [], unresolved: [], excluded_terms: [], parser_fallback: 'model_interpretation_unavailable' },
      grouping: { axis: '年代', paths: [{ node_ids: ['work', 'era'], edge_ids: ['catalog'], stop_reason: 'target_reached' }] },
      available_context: { ...emptyContext, status: 'partial',
        nodes: [{ id: 'work', display_name: '馆藏' }, { id: 'era', display_name: '馆藏年代' }],
        edges: [{ id: 'catalog', source: 'era', target: 'work', relation: '分类适用于' }] },
    }, '馆藏年代')
    expect(root.querySelector('.retrieved-paths')?.textContent).toContain('分组候选路径')
    expect(root.querySelector('.path-relation')?.textContent).toContain('← 分类适用于')
    expect(root.textContent).toContain('模型未返回可用的解析结果')
    await search({ available_context: { ...emptyContext, status: 'source_text_only', passages: [passage] } }, '尚未确认对应实体')
    expect(root.querySelector('[aria-label="来源原文"]')?.textContent).toContain(passage.excerpt)
    expect(root.textContent).not.toContain('只符合部分条件')
    await search({ available_context: { ...emptyContext, status: 'no_confirmed_match' } }, '当前可见范围内未能确认匹配')
    expect(root.querySelector('[aria-label="来源原文"]')).toBeNull()
    await search({ available_context: { ...emptyContext, status: 'scope_unresolved' } }, '尚未确认问题所指的适用范围')
    await search({ available_context: { ...emptyContext, status: 'search_unavailable' } }, '语义检索当前不可用')
  })
  it('以原始问题试写 SQL，显示本次子图上下文与未执行的草稿', async () => {
    vectorizedState = { ...vectorizedState, vectorized: true, status: 'vectorized' }
    retrievalFixture = { agent_context: { graph_version: 'archive-v2', nodes: [], edges: [] },
      sql_preview: { status: 'generated', sql: 'SELECT SUM(weight) FROM archive.finds' } }
    await mountReady()
    button('运行与结果').click()
    await nextTick()
    button('子图召回').click()
    await vi.waitFor(() => expect(root.querySelector('#retrieval-query')).not.toBeNull())
    const question = root.querySelector<HTMLTextAreaElement>('#retrieval-query')!
    question.value = '馆藏总重量'
    question.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    button('召回并试写 SQL').click()
    await vi.waitFor(() => expect(root.querySelector('.sql-preview code')?.textContent).toContain('SUM(weight)'))
    expect(root.querySelector('.agent-context')?.textContent).toContain('archive-v2')
    const posts = requests.filter(r => r.url.includes('/retrieval/query'))
    expect(posts).toHaveLength(1)
    expect(posts[0]!.url).toContain('sql_preview=true')
    expect(JSON.parse(posts[0]!.body!)).toEqual({ query: question.value })
  })
  it('按完整码选择同名起点，上下级答案为每个对象保留有方向的原边', async () => {
    vectorizedState = { ...vectorizedState, vectorized: true, status: 'vectorized' }
    await mountReady()
    button('运行与结果').click()
    await nextTick()
    button('子图召回').click()
    await vi.waitFor(() => expect(root.querySelector('#retrieval-query')).not.toBeNull())
    button('直接上下级').click()
    await nextTick()
    const candidates = ['101', '202'].map(code => ({
      node_id: 'n' + code, kind: '科目', display_name: '设备', identity: { code },
      source_bindings: [{ source_id: 'finance', schema: 'public', table: 'stock', key_columns: ['asset_code'] }],
    }))
    retrievalFixture = {
      slots: [{ surface: '设备', candidates }],
      hierarchy: { direction: 'parents', status: 'ambiguous', anchor_id: null, items: [] },
    }
    const direction = root.querySelector<HTMLSelectElement>('#hierarchy-direction')!
    direction.value = 'parents'; direction.dispatchEvent(new Event('change'))
    const anchor = root.querySelector<HTMLInputElement>('#hierarchy-anchor')!
    anchor.value = '设备'; anchor.dispatchEvent(new Event('input'))
    await nextTick()
    button('检索').click()
    await vi.waitFor(() => expect(root.querySelectorAll('.hierarchy-answer button')).toHaveLength(2))
    expect(JSON.parse(requests.filter(r => r.url.endsWith('/retrieval/query')).at(-1)!.body!))
      .toEqual({ mode: 'hierarchy', query: '设备', hierarchy_direction: 'parents' })
    expect(root.querySelector('.hierarchy-answer')?.textContent).toContain('code · 202')
    expect(root.querySelector('.hierarchy-answer')?.textContent).toContain('finance / public.stock · asset_code')
    expect(root.querySelector('.hierarchy-answer li:nth-child(2) details:not([open]) code')?.textContent).toContain('n202')
    retrievalFixture = {
      hierarchy: { direction: 'parents', status: 'found', anchor_id: 'n202',
        items: [{ node_id: 'p', edge_ids: ['forward', 'inverse'] }] },
      nodes: [{ id: 'n202', display_name: '设备' }, { id: 'p', display_name: '收入' }],
      edges: [{ id: 'forward', source: 'p', target: 'n202', relation: '包含科目' },
        { id: 'inverse', source: 'n202', target: 'p', relation: '隶属科目' }],
    }
    root.querySelectorAll<HTMLButtonElement>('.hierarchy-answer button')[1]!.click()
    await vi.waitFor(() => expect(root.querySelector('.hierarchy-answer h3')?.textContent).toContain('设备的直接上级 · 1 个'))
    expect(JSON.parse(requests.filter(r => r.url.endsWith('/retrieval/query')).at(-1)!.body!))
      .toEqual({ mode: 'hierarchy', node_id: 'n202', hierarchy_direction: 'parents' })
    expect(root.querySelectorAll('.hierarchy-items li')).toHaveLength(1)
    expect([...root.querySelectorAll('.hierarchy-edge p')].map(p => p.textContent)).toEqual(['收入 → 设备', '设备 → 收入'])
  })
  it('按字段检索、精确 ID 及下钻分别传递对应参数，切换时清除旧结果', async () => {
    vectorizedState = { ...vectorizedState, vectorized: true, status: 'vectorized' }
    await mountReady()
    button('运行与结果').click()
    await nextTick()
    button('子图召回').click()
    await vi.waitFor(() => expect(root.querySelector('#retrieval-query')).not.toBeNull())
    async function setValue(selector: string, value: string) {
      const el = root.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(selector)!
      el.value = value
      el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }))
      await nextTick()
    }
    async function submit() {
      const count = requests.filter(r => r.url.endsWith('/retrieval/query')).length
      button('检索').click()
      await vi.waitFor(() => expect(requests.filter(r => r.url.endsWith('/retrieval/query'))).toHaveLength(count + 1))
      return JSON.parse(requests.filter(r => r.url.endsWith('/retrieval/query')).at(-1)!.body!)
    }
    expect(root.querySelector('#retrieval-field')).toBeNull()
    expect(root.querySelector('#retrieval-query')?.tagName).toBe('TEXTAREA')
    await setValue('#retrieval-query', '华东收入')
    expect(await submit()).toEqual({ query: '华东收入' })
    expect(root.textContent).toContain('当前范围内未找到能同时支持这些条件的事实')
    expect(root.textContent).toContain('收入 → 营业收入（语义匹配）')
    button('调试检索算子').click()
    await nextTick()
    await setValue('#retrieval-query', '采购')
    await setValue('#retrieval-field', 'description')
    await setValue('#retrieval-method', 'keyword')
    await setValue('#retrieval-hops', '0')
    expect(await submit()).toMatchObject({ query: '采购', field: 'description', method: 'keyword', hops: 0 })

    button('按节点 ID').click()
    await nextTick()
    expect(root.textContent).not.toContain('检索结果')
    await setValue('#retrieval-node-id', ' n1 ')
    expect(await submit()).toEqual({ node_id: 'n1', hops: 0, business_context: null })
    expect(root.textContent).toContain('精确 ID')
    await setValue('#retrieval-id-hops', '2')
    expect(await submit()).toMatchObject({ node_id: 'n1', hops: 2 })

    button('路径下钻').click()
    await nextTick()
    expect(root.querySelector('#include-endpoint-variants')).toBeNull()
    expect(await submit()).toEqual({ query: '采购', mode: 'drilldown', field: 'description', method: 'keyword',
      direction: 'outgoing', max_depth: 12, business_context: null })
    await setValue('#drilldown-start', 'ids')
    await setValue('#drilldown-seeds', 'n1，n2,n1')
    await setValue('#drilldown-target', 'n2')
    await setValue('#drilldown-relations', 'SETTLED_BY')
    await setValue('#drilldown-depth', '7')
    expect(await submit()).toEqual({ mode: 'drilldown', seed_ids: ['n1', 'n2'], target_id: 'n2', direction: 'outgoing',
      relation_types: ['SETTLED_BY'], max_depth: 7, business_context: null })
    expect(root.textContent).toContain('已到指定终点')
    expect(root.textContent).toContain('2 条并列路径，当前展示其中一条')
    expect(root.textContent).toContain('project：M')
    const variants = root.querySelector<HTMLInputElement>('#include-endpoint-variants')!
    expect(variants.checked).toBe(false)
    variants.click()
    await nextTick()
    expect(await submit()).toMatchObject({ mode: 'drilldown', relation_types: ['SETTLED_BY'], include_endpoint_variants: true })

    button('多层关联').click()
    await nextTick()
    retrievalFixture = { traverse: { status: 'found', uncovered_seed_ids: [], paths: [
      {node_ids: ['n1','n2'], edge_ids:['e1'], stop_reason:'anchors_connected'}] },
      nodes:[{id:'n1', kind:'文献', display_name:'海图'}, {id:'n2', kind:'展品', display_name:'罗盘'}],
      edges:[{id:'e1', source:'n2', target:'n1', relation:'参考'}] }
    expect(await submit()).toEqual({ mode: 'traverse', seed_ids: ['n1','n2'], target_id: 'n2', direction: 'both',
      relation_types:['SETTLED_BY'], include_endpoint_variants: true, max_depth: 7, business_context: null })
    const path = root.querySelector('.retrieved-paths')!
    expect(path.querySelector('h3')?.textContent).toBe('关联路径')
    expect(path.textContent).toContain('已连接指定节点')
    expect(path.textContent).toContain('← 参考')

    button('按节点 ID').click()
    await nextTick()
    expect(root.querySelector('#include-endpoint-variants')).toBeNull()
    expect(await submit()).toEqual({ node_id: 'n1', hops: 2, business_context: null })
    button('多层关联').click()
    await nextTick()
    await setValue('#drilldown-relations', ' ， ')
    expect(root.querySelector('#include-endpoint-variants')).toBeNull()
    const unfiltered = await submit()
    expect(unfiltered).not.toHaveProperty('relation_types')
    expect(unfiltered).not.toHaveProperty('include_endpoint_variants')
    await setValue('#drilldown-relations', 'SETTLED_BY')
    expect(root.querySelector<HTMLInputElement>('#include-endpoint-variants')!.checked).toBe(false)
  })

})
