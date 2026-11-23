// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App as VueApp } from 'vue'
import App from './App.vue'

const api = vi.hoisted(() => ({ request: vi.fn(), ensureSession: vi.fn(), clearSessions: vi.fn() }))
vi.mock('./api', async importOriginal => ({
  ...await importOriginal<typeof import('./api')>(), ...api,
}))

const projects = ['alpha', 'beta'].map(project_id => ({ project_id, name: project_id, files: [], created_at: '2026-11-02T00:00:00Z', updated_at: '2026-11-02T00:00:00Z' }))
function run(project: string, seq = 1) {
  return {
    run_id: `run_${project}`, project_id: project, status: 'running', stage: 'discovery',
    created_at: '2026-11-02T00:00:00Z', cleanup_status: 'pending', report: null,
    progress: {
      stage: 'discovery', completed: seq - 1, total: 5, unit: 'tables', updated_at: '2026-11-02T00:00:05Z', elapsed_seconds: 5,
      counters: { model_calls: seq }, dropped_events: 0,
      events: [{ seq, time: '2026-11-02T00:00:05Z', stage: 'discovery', code: 'table_started', level: 'info', table: `${project}.public.table_${seq}` }],
    },
  }
}
let app: VueApp | undefined
let root: HTMLDivElement
let live = run('alpha')
let failPoll = false
let delayedPoll: Promise<unknown> | undefined
let historicalRuns = 0
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); await nextTick() }

beforeEach(() => {
  vi.useFakeTimers()
  sessionStorage.clear()
  live = run('alpha'); failPoll = false; delayedPoll = undefined; historicalRuns = 0
  api.ensureSession.mockReset().mockResolvedValue(undefined)
  api.clearSessions.mockReset()
  api.request.mockReset().mockImplementation(async (url: string) => {
    if (url === '/projects') return { projects }
    if (url.endsWith('/config')) return { sources: [{ id: 'input', kind: 'csv_file' }], goal: { purpose: '验证运行面板' } }
    if (url.endsWith('/metadata')) return { tables: [], nodes: [], edges: [] }
    if (url.includes('/graph?')) return { nodes: [], edges: [] }
    if (url.endsWith('/credentials')) return { database_sources: [], model: false }
    const project = projects.find(item => url === `/projects/${item.project_id}`)
    if (project) return project
    if (url.endsWith('/runs?view=summary')) {
      const current = url.includes('/alpha/') ? live : run('beta')
      return { runs: [{...current, progress: undefined}, ...Array.from({length: historicalRuns}, (_, i) =>
        ({...current, progress: undefined, run_id: 'old_' + i, status: 'completed'}))] }
    }
    if (url.endsWith('/runs/run_alpha?view=summary')) return { ...live, progress: undefined }
    if (url.includes('/runs/old_') && url.endsWith('?view=workbench')) {
      const id = url.split('/').at(-1)!.split('?')[0]
      return { ...live, status: 'completed', run_id: id, progress: undefined, output_path: 'details/' + id }
    }
    if (url.endsWith('/runs/run_alpha?view=workbench')) {
      if (failPoll) throw new Error('temporary_network_failure')
      return delayedPoll ?? structuredClone(live)
    }
    if (url.endsWith('/runs/run_beta?view=workbench')) return run('beta')
    throw new Error(`Unexpected request ${url}`)
  })
  root = document.createElement('div'); document.body.append(root)
})
afterEach(() => { app?.unmount(); app = undefined; root.remove(); vi.useRealTimers(); sessionStorage.clear() })

async function showRuns() {
  if (!app) { app = createApp(App); app.mount(root) }
  await flush()
  root.querySelector<HTMLButtonElement>('#results-phase')!.click()
  await flush()
}

describe('运行页面实时更新', () => {
  it('连续切换时立即选中最新运行，取消旧详情并独立轮询后台运行摘要', async () => {
    historicalRuns = 2
    app = createApp(App); app.mount(root); await flush()
    expect(api.request.mock.calls.some(([url]) => String(url).includes('?view=workbench'))).toBe(false)
    let finish!: (value: unknown) => void
    delayedPoll = new Promise(resolve => { finish = resolve })
    await showRuns()
    const oldSignal = api.request.mock.calls.find(([url]) => url.endsWith('/runs/run_alpha?view=workbench'))![1].signal
    const choose = (id: string) => [...root.querySelectorAll('tr')].find(row => row.cells[0].textContent === id)!.querySelector('button')!
    choose('old_0').click(); await flush()
    choose('old_1').click(); await flush()
    expect(oldSignal.aborted).toBe(true)
    expect(root.querySelector('.chosen')?.textContent).toContain('old_1')
    expect(root.textContent).toContain('details/old_1')
    finish(run('alpha', 5)); await flush()
    expect(root.textContent).not.toContain('alpha.public.table_5')
    await vi.advanceTimersByTimeAsync(1000)
    expect(api.request.mock.calls.some(([url]) => url.endsWith('/runs/run_alpha?view=summary'))).toBe(true)
    expect(api.request.mock.calls.filter(([url]) => url.endsWith('/runs/run_alpha?view=workbench'))).toHaveLength(1)
    expect(api.request.mock.calls.some(([url]) => url.endsWith('/workflow'))).toBe(false)
    expect(root.textContent).toContain('details/old_1')
  })
  it('较长运行列表分页保留全部历史记录，离开运行页后释放详情并在返回时重读', async () => {
    historicalRuns = 51
    await showRuns()
    const rows = () => root.querySelectorAll('.run-history-table tbody tr')
    expect(rows()).toHaveLength(50)
    root.querySelector<HTMLButtonElement>('nav[aria-label="运行记录分页"] button:last-child')!.click()
    await flush()
    expect(rows()).toHaveLength(2)
    rows()[1].querySelector('button')!.click()
    await flush()
    expect(root.textContent).toContain('details/old_50')
    root.querySelector<HTMLButtonElement>('[role="tab"][aria-controls="phase-content"]:not(#results-phase)')!.click()
    await flush()
    await showRuns()
    expect(api.request.mock.calls.filter(([url]) => url.endsWith('/runs/old_50?view=workbench'))).toHaveLength(2)
    expect(root.textContent).toContain('details/old_50')
  })
  it('节点说明显示失败阶段、系统错误号及实际尝试次数，旧运行只显示错误码', async () => {
    await showRuns()
    live = Object.assign(run('alpha', 2), {
      status: 'partial', workflow: true,
      workflow_state: {
        units: { a: { unit_id: 'a', status: 'failed', model_calls: 37, error_code: 'workflow_unit_failed' } },
        merge_nodes: { m: { node_id: 'm', status: 'failed', error_code: 'workflow_access_context_register_failed',
          failure_detail: { stage: 'access_context_register', exception_type: 'PermissionError', errno: 13, winerror: 32, attempts: 4 } } },
      },
    })
    await vi.advanceTimersByTimeAsync(1000)
    await flush()
    const rows = [...root.querySelectorAll('tr')]
    const merge = rows.find(row => row.textContent?.includes('workflow_access_context_register_failed'))
    expect(merge?.textContent).toContain('登记访问范围 · PermissionError · errno 13 · WinError 32 · 共尝试 4 次')
    const extract = rows.find(row => row.textContent?.includes('workflow_unit_failed'))
    expect(extract).toBeDefined()
    expect(extract?.cells[5]?.textContent).toBe('37')
    expect(merge?.cells[5]?.textContent).toBe('—')
    expect(extract?.textContent).not.toContain('共尝试')
  })
  it('通过真实轮询入口更新阶段进度和新日志，避免重复追加旧快照', async () => {
    await showRuns()
    expect(root.textContent).toContain('alpha.public.table_1')
    live = run('alpha', 2)
    await vi.advanceTimersByTimeAsync(1000)
    await flush()
    expect(root.textContent).toContain('alpha.public.table_2')
    const bar = root.querySelector('[role="progressbar"]')
    expect(bar?.getAttribute('aria-valuenow')).toBe('20')
    await vi.advanceTimersByTimeAsync(1000)
    await flush()
    expect(root.querySelectorAll('[role="log"] .runtime-log-row')).toHaveLength(1)
  })

  it('轮询失败保留上次日志并标明未刷新，成功后清除提示', async () => {
    await showRuns()
    failPoll = true
    await vi.advanceTimersByTimeAsync(1000)
    await flush()
    expect(root.textContent).toContain('alpha.public.table_1')
    expect(root.textContent).toContain('进度刷新失败')
    failPoll = false; live = run('alpha', 3)
    await vi.advanceTimersByTimeAsync(1000)
    await flush()
    expect(root.textContent).toContain('alpha.public.table_3')
    expect(root.textContent).not.toContain('进度刷新失败')
  })

  it('切换项目后忽略旧请求的迟到日志', async () => {
    await showRuns()
    let finish!: (value: unknown) => void
    delayedPoll = new Promise(resolve => { finish = resolve })
    await vi.advanceTimersByTimeAsync(1000)
    const beta = [...root.querySelectorAll<HTMLButtonElement>('.project')].find(button => button.textContent?.includes('beta'))!
    beta.click(); await flush()
    expect(root.textContent).toContain('beta.public.table_1')
    finish(run('alpha', 5)); await flush()
    expect(root.textContent).not.toContain('alpha.public.table_5')
    expect(root.textContent).toContain('beta.public.table_1')
  })
})
