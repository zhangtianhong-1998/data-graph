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
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); await nextTick() }

beforeEach(() => {
  vi.useFakeTimers()
  sessionStorage.clear()
  live = run('alpha'); failPoll = false; delayedPoll = undefined
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
    if (url.endsWith('/runs')) return { runs: [structuredClone(url.includes('/alpha/') ? live : run('beta'))] }
    if (url.endsWith('/runs/run_alpha')) {
      if (failPoll) throw new Error('temporary_network_failure')
      return delayedPoll ?? structuredClone(live)
    }
    if (url.endsWith('/runs/run_beta')) return run('beta')
    throw new Error(`Unexpected request ${url}`)
  })
  root = document.createElement('div'); document.body.append(root)
  app = createApp(App); app.mount(root)
})
afterEach(() => { app?.unmount(); root.remove(); vi.useRealTimers(); sessionStorage.clear() })

async function showRuns() {
  await flush()
  root.querySelector<HTMLButtonElement>('#results-phase')!.click()
  await flush()
}

describe('运行页面实时更新', () => {
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
