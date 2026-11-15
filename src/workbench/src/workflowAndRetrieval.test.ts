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
    embedding: { provider: 'local_hash', base_url: '', model: '' },
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
    if (url.endsWith('/runs')) {
      if (method === 'POST') return response({ run_id: 'run_canvas', project_id: 'wf_proj', status: 'completed', stage: 'finished', created_at: '2026-11-15T00:00:00Z', cleanup_status: 'completed', workflow: true }, 202)
      return response({
        runs: [
          {
            run_id: 'run_parent_1',
            project_id: 'wf_proj',
            status: 'completed_with_gaps',
            stage: 'finished',
            created_at: '2026-11-15T00:00:00Z',
            cleanup_status: 'completed',
            workflow: true,
            workflow_state: {
              parent_run_id: 'run_parent_1',
              status: 'completed_with_gaps',
              units: {
                unit_1: { unit_id: 'unit_1', label: 'A+B 链路', status: 'completed', run_id: 'run_u1', nodes: 12, edges: 8 },
                unit_2: { unit_id: 'unit_2', label: 'A+C 链路', status: 'failed', run_id: 'run_u2', error_code: 'run_time_budget_exhausted', nodes: 0, edges: 0 },
              },
              merge_nodes: {
                merge_1: { node_id: 'merge_1', label: '全节点合并抽取', merge_mode: 'merge_and_extract', status: 'completed', nodes: 20, edges: 14 },
              },
              merge: { status: 'completed', nodes: 12, edges: 8, candidate_identity_merges: 1, candidate_column_alignments: 2 },
            },
          },
        ],
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
      if (method === 'GET') return response({ status: vectorizedState.vectorized ? 'completed' : 'idle' })
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
    if (url.endsWith('/retrieval/query')) {
      return response({
        query: '采购合同 结算单',
        scopes: ['public'],
        slots: [
          {
            surface: '采购合同',
            source: 'chunk',
            candidates: [{ node_id: 'n1', kind: 'Contract', vec_score: 0.91, vec_rank: 1, lex_rank: 1, rrf_score: 0.032, match_type: 'exact_name' }],
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
    await nextTick()

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
    modeSelect.value = 'merge_and_extract'
    modeSelect.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(root.textContent).toContain('全节点合并抽取')

    root.querySelectorAll<HTMLInputElement>('.wf-inspector input[type=radio]')[1]!.click()
    await nextTick()
    root.querySelectorAll<HTMLInputElement>('.inspector-doc-list input')[1]!.click()
    await nextTick()
    root.querySelectorAll<HTMLInputElement>('.wf-inspector .source-box input')[0]!.click()
    const budget = root.querySelector<HTMLInputElement>('#inspector-merge-0-calls')!
    budget.value = '17'
    budget.dispatchEvent(new Event('input', { bubbles: true }))
    const conflict = root.querySelector<HTMLSelectElement>('#merge-conflict-mode')!
    conflict.value = 'strict'
    conflict.dispatchEvent(new Event('change', { bubbles: true }))
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
      merge_policy: { conflict_mode: 'strict' },
    })
    expect(config.workflow?.merge_policy.conflict_mode).toBe('quarantine')
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
    await nextTick()
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

    button('测试向量服务连通性').click()
    await vi.waitFor(() => expect(root.textContent).toContain('向量维度：1024'))

    const embKeyInput = root.querySelector<HTMLInputElement>('#embedding-key')!
    embKeyInput.value = 'local-emb-key'
    embKeyInput.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    button('保存配置').click()
    await vi.waitFor(() => expect(requests.some(r => r.method === 'PUT' && r.url.endsWith('/credentials/embedding'))).toBe(true))
    expect(config.embedding?.provider).toBe('openai_compatible')
    expect(JSON.stringify(config)).not.toContain('local-emb-key')
  })

  it('抽取完成后默认未向量化，用户点击立即向量化后显示已完成向量化并可执行子图召回', async () => {
    await mountReady()
    button('运行与结果').click()
    await nextTick()

    expect(root.textContent).toContain('A+B 链路')
    expect(root.textContent).toContain('全节点合并抽取')

    // 业务图谱页显示「未向量化」，不主动触发 /retrieval/sync
    button('业务图谱').click()
    await vi.waitFor(() => expect(root.textContent).toContain('未向量化'))
    expect(requests.some(r => r.method === 'POST' && r.url.endsWith('/retrieval/jobs'))).toBe(false)

    // 切换到子图召回页：初始状态是「未向量化」，点击「立即向量化」后变成「已完成向量化」
    button('子图召回').click()
    await vi.waitFor(() => expect(root.textContent).toContain('未向量化（抽取已完成，待用户确认执行）'))
    button('立即向量化').click()
    await vi.waitFor(() => expect(root.textContent).toContain('已完成向量化'))
    expect(root.textContent).toContain('Qwen/Qwen3-Embedding-0.6B')
    expect(root.textContent).toContain('1024')

    const queryInput = root.querySelector<HTMLInputElement>('#retrieval-query')!
    queryInput.value = '采购合同 结算单'
    queryInput.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    button('执行子图召回').click()
    await vi.waitFor(() => expect(root.textContent).toContain('召回结果总览'))
    expect(root.textContent).toContain('华东设备采购合同')
    expect(root.textContent).toContain('结算中心')
    expect(root.textContent).toContain('SETTLED_BY')
  })
})
