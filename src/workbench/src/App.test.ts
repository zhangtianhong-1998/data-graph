// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App as VueApp } from 'vue'
import App from './App.vue'
import { WorkbenchLocation, workbenchLocationKey } from './workbenchLocation'

const api = vi.hoisted(() => ({ request: vi.fn(), ensureSession: vi.fn(), clearSessions: vi.fn() }))
const dialogFixture = vi.hoisted(() => ({ real: false, initialBusy: false, completeDraft: vi.fn() }))
vi.mock('./api', async importOriginal => ({
  ...await importOriginal<typeof import('./api')>(),
  request: api.request, ensureSession: api.ensureSession, clearSessions: api.clearSessions,
}))
vi.mock('./DatabaseSourceDialog.vue', async importOriginal => {
  const actual = await importOriginal<typeof import('./DatabaseSourceDialog.vue')>()
  const { h, ref } = await import('vue')
  return { default: {
    props: ['projectId', 'source', 'busy', 'sources', 'files', 'retentionVerified', 'error'], emits: ['close', 'connected', 'save'],
    setup(props: { projectId: string; source: { id: string } | null; busy: boolean }, { emit, expose }: { emit: (name: string, ...args: unknown[]) => void; expose: (value: object) => void }) {
      dialogFixture.initialBusy = props.busy
      const instance = ref<{ completeDraft: () => Promise<void> } | null>(null)
      expose({ completeDraft: async () => { await dialogFixture.completeDraft(); await instance.value?.completeDraft() } })
      if (dialogFixture.real) return () => h(actual.default, {
        ...props as InstanceType<typeof actual.default>['$props'], ref: instance,
        onClose: () => emit('close'),
        onConnected: (...args: unknown[]) => emit('connected', ...args),
        onSave: (...args: unknown[]) => emit('save', ...args),
      })
      return () => h('section', { 'data-test': 'database-dialog', 'data-project': props.projectId, 'data-source': props.source?.id ?? 'new' }, [
        h('button', { disabled: props.busy, onClick: () => emit('close') }, '关闭连接窗口'),
        h('button', { onClick: () => emit('connected', { sources: [] }, { tables: [], nodes: [], edges: [] }) }, '连接完成'),
        h('button', { onClick: () => emit('save', [], false) }, '保存环境变量来源'),
      ])
    },
  } }
})

const projects = ['first', 'selected'].map(project_id => ({ project_id, name: project_id, files: [], created_at: '2026-10-23T00:00:00Z', updated_at: '2026-10-23T00:00:00Z' }))
let app: VueApp | undefined
let root: HTMLDivElement
const dialog = () => root.querySelector<HTMLElement>('[data-test="database-dialog"]')

function mount() {
  root = document.createElement('div'); document.body.append(root)
  app = createApp(App); app.mount(root)
}
function unmount() { app?.unmount(); app = undefined; root?.remove() }
function button(text: string): HTMLButtonElement {
  const value = [...root.querySelectorAll('button')].find(button => button.textContent?.trim() === text)
  if (!value) throw new Error(`Missing button ${text}`)
  return value
}

beforeEach(() => {
  sessionStorage.clear()
  dialogFixture.real = false
  dialogFixture.initialBusy = false
  dialogFixture.completeDraft.mockReset().mockResolvedValue(undefined)
  api.ensureSession.mockReset().mockResolvedValue(undefined)
  api.clearSessions.mockReset()
  api.request.mockReset().mockImplementation(async (url: string, options?: { method: string; body: string }) => {
    if (url === '/projects') return { projects }
    if (url.endsWith('/config')) return options?.method === 'PUT' ? JSON.parse(options.body) : { sources: [{ id: 'db', kind: 'gauss_database', allowed_tables: [] }] }
    if (url.endsWith('/metadata')) return { tables: [], nodes: [], edges: [] }
    if (url.includes('/graph?')) return { nodes: [], edges: [] }
    if (url.endsWith('/runs')) return { runs: [] }
    if (url.endsWith('/credentials')) return { database_sources: [], model: false }
    if (url.includes('/database/drafts/')) return { saved: false, revision: 0 }
    const project = projects.find(project => url === `/projects/${project.project_id}`)
    if (project) return project
    throw new Error(`Unexpected request ${url}`)
  })
})
afterEach(() => { unmount(); sessionStorage.clear() })

describe('刷新恢复数据库填写窗口', () => {
  it('已加载项目配置与会话后恢复原项目及来源，窗口可交互', async () => {
    new WorkbenchLocation().openDatabase('selected', 'db')
    mount()
    await vi.waitFor(() => expect(dialog()?.dataset.project).toBe('selected'))
    expect(dialog()?.dataset.source).toBe('db')
    expect(api.ensureSession).toHaveBeenCalledWith('selected')
    expect(api.request).not.toHaveBeenCalledWith('/projects/first')
    expect(button('关闭连接窗口').disabled).toBe(false)
  })

  it('填写新连接后刷新仍打开新连接窗口，主动关闭后刷新不再打开', async () => {
    mount()
    await vi.waitFor(() => expect(root.querySelector('.page-empty')).toBeNull())
    button('＋ 连接').click()
    await vi.waitFor(() => expect(dialog()?.dataset.source).toBe('new'))
    unmount(); mount()
    await vi.waitFor(() => expect(dialog()?.dataset.source).toBe('new'))
    button('关闭连接窗口').click(); await nextTick()
    expect(dialog()).toBeNull()
    unmount(); mount()
    await vi.waitFor(() => expect(root.querySelector('.page-empty')).toBeNull())
    expect(dialog()).toBeNull()
  })

  it.each(['连接完成', '保存环境变量来源'])('%s 后清除窗口恢复标记', async label => {
    new WorkbenchLocation().openDatabase('selected', 'db')
    mount()
    await vi.waitFor(() => expect(dialog()).not.toBeNull())
    button(label).click()
    await vi.waitFor(() => expect(dialog()).toBeNull())
    expect(JSON.parse(sessionStorage.getItem(workbenchLocationKey)!)).toEqual({ projectId: 'selected' })
    expect(dialogFixture.completeDraft).toHaveBeenCalledTimes(label === '保存环境变量来源' ? 1 : 0)
  })

  it('保存环境来源后草稿删除失败时保留窗口与恢复标记，允许重试', async () => {
    dialogFixture.completeDraft.mockRejectedValueOnce(new Error('草稿删除失败'))
    new WorkbenchLocation().openDatabase('selected', 'db')
    mount()
    await vi.waitFor(() => expect(dialog()).not.toBeNull())
    button('保存环境变量来源').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('草稿删除失败'))
    expect(dialog()).not.toBeNull()
    expect(JSON.parse(sessionStorage.getItem(workbenchLocationKey)!)).toEqual({ projectId: 'selected', databaseSourceId: 'db' })
    button('保存环境变量来源').click()
    await vi.waitFor(() => expect(dialog()).toBeNull())
  })

  it('来源已删除时只恢复项目，不打开不存在的来源', async () => {
    new WorkbenchLocation().openDatabase('selected', 'deleted')
    mount()
    await vi.waitFor(() => expect(root.querySelector('.page-empty')).toBeNull())
    expect(root.querySelector('.project.active')?.textContent).toContain('selected')
    expect(dialog()).toBeNull()
    expect(JSON.parse(sessionStorage.getItem(workbenchLocationKey)!)).toEqual({ projectId: 'selected' })
  })

  it('切换项目后刷新不会打开上一个项目的连接窗口', async () => {
    new WorkbenchLocation().openDatabase('selected', 'db')
    mount()
    await vi.waitFor(() => expect(dialog()).not.toBeNull())
    const first = [...root.querySelectorAll<HTMLButtonElement>('.project')].find(item => item.textContent?.includes('first'))!
    first.click()
    await vi.waitFor(() => {
      expect(root.querySelector('.page-empty')).toBeNull()
      expect(root.querySelector('.project.active')?.textContent).toContain('first')
    })
    expect(dialog()).toBeNull()
    unmount(); mount()
    await vi.waitFor(() => expect(root.querySelector('.page-empty')).toBeNull())
    expect(root.querySelector('.project.active')?.textContent).toContain('first')
    expect(dialog()).toBeNull()
  })

  it('从 App 操作中打开真实连接窗口时，父组件 busy 不会跳过草稿加载', async () => {
    dialogFixture.real = true
    const originalRequest = api.request.getMockImplementation()!
    api.request.mockImplementation(async (...args) => args[0] === '/projects/first/database/drafts/new' ? {
      saved: true, revision: 1, draft: {
        form: { id: 'draft-id', kind: 'gauss_database', dsnEnv: '', tables: 'public.metrics', capabilityManifest: '' },
        connection: { kind: 'gauss_database', format: 'dsn', dsn: 'jdbc:gaussdb://test.invalid:8000/sample', host: '', port: '8000', database: '', username: 'fixture_reader', password: 'fixture-only-secret', schema: '', sslmode: 'prefer' },
        mode: 'direct', retention: false, checked: [],
      },
    } : originalRequest(...args))
    mount()
    await vi.waitFor(() => expect(root.querySelector('.page-empty')).toBeNull())
    button('＋ 连接').click()
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledWith('/projects/first/database/drafts/new', expect.any(Object)))
    await vi.waitFor(() => expect(root.querySelector<HTMLInputElement>('#database-id')?.disabled).toBe(false))
    expect(dialogFixture.initialBusy).toBe(true)
    expect(root.querySelector<HTMLInputElement>('#database-id')?.value).toBe('draft-id')
    expect(root.querySelector<HTMLInputElement>('#database-dsn')?.value).toBe('jdbc:gaussdb://test.invalid:8000/sample')
    expect(sessionStorage.getItem(workbenchLocationKey)).not.toContain('fixture-only-secret')
    expect(root.querySelector('[aria-labelledby="database-title"]')).not.toBeNull()
  })

  it('真实环境变量来源保存成功后删除草稿，卸载不会重新保存它', async () => {
    dialogFixture.real = true
    const originalRequest = api.request.getMockImplementation()!
    api.request.mockImplementation(async (...args) => {
      if (args[0] !== '/projects/first/database/drafts/new') return originalRequest(...args)
      if (args[1]?.method === 'DELETE') return { saved: false, revision: 2 }
      return { saved: true, revision: 1, draft: {
        form: { id: 'new-db', kind: 'gauss_database', dsnEnv: 'LIGHTGRAPH_GAUSS_DSN', tables: 'public.metrics', capabilityManifest: '' },
        connection: { kind: 'gauss_database', format: 'fields', dsn: '', host: '', port: '5432', database: '', username: '', password: '', schema: '', sslmode: 'auto' },
        mode: 'environment', retention: false, checked: [],
      } }
    })
    mount()
    await vi.waitFor(() => expect(root.querySelector('.page-empty')).toBeNull())
    button('＋ 连接').click()
    await vi.waitFor(() => expect(root.querySelector<HTMLInputElement>('#database-env')?.disabled).toBe(false))
    button('保存数据库来源').click()
    await vi.waitFor(() => expect(root.querySelector('[aria-labelledby="database-title"]')).toBeNull())
    await nextTick()
    const draftCalls = api.request.mock.calls.filter(call => call[0] === '/projects/first/database/drafts/new')
    expect(draftCalls.map(call => call[1]?.method ?? 'GET')).toEqual(['GET', 'DELETE'])
    expect(JSON.parse(sessionStorage.getItem(workbenchLocationKey)!)).toEqual({ projectId: 'first' })
  })
})
