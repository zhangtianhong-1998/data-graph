// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App as VueApp } from 'vue'
import App from './App.vue'
import type { Metadata, Project, ProjectFile, RunConfig } from './types'
import { WorkbenchLocation, workbenchLocationKey } from './workbenchLocation'

const api = vi.hoisted(() => ({ request: vi.fn(), ensureSession: vi.fn(), clearSessions: vi.fn(), clearSession: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), ...api }))
let app: VueApp | undefined, root: HTMLDivElement
let projects: Project[], config: RunConfig, metadata: Metadata, running: boolean, deletionFailure: boolean
const file = (file_id: string, kind: ProjectFile['kind'], name: string): ProjectFile => ({ file_id, kind, name, relative_path: `files/${name}`, size: 20, purpose: 'data_description', scope: { source_ids: [], tables: [], columns: [] } })
const table = (source_id: string, name: string) => ({ source_id, schema: 'public', name, columns: [{ name: `${name}_name`, data_type: 'text' }] })
function mount() { root = document.createElement('div'); document.body.append(root); app = createApp(App); app.mount(root) }
function button(text: string) { const found = [...root.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.trim() === text); if (!found) throw new Error(`Missing ${text}`); return found }
function labelled(label: string) { const found = root.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`); if (!found) throw new Error(`Missing ${label}`); return found }
const dialog = () => root.querySelector('[role="alertdialog"]')
const deletes = () => api.request.mock.calls.filter(call => call[1]?.method === 'DELETE')
async function ready() { await vi.waitFor(() => expect(root.querySelector('.page-empty')).toBeNull()) }
async function input(id: string, value: string) { const el = root.querySelector<HTMLInputElement | HTMLTextAreaElement>(`#${id}`)!; el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); await nextTick() }

beforeEach(() => {
  sessionStorage.clear(); running = false; deletionFailure = false
  projects = ['first', 'next'].map(project_id => ({ project_id, name: project_id, files: project_id === 'first' ? [file('csv', 'csv', 'metrics.csv'), file('excel', 'excel', 'people.xlsx'), file('yaml', 'yaml', 'tables.yaml'), file('md', 'markdown', 'guide.md')] : [], created_at: '2026-10-24T00:00:00Z', updated_at: '2026-10-24T00:00:00Z' }))
  config = { goal: { purpose: '原目标' }, model: { mode: 'disabled' }, documents: ['files/guide.md'], sources: [{ id: 'db', kind: 'gauss_database', allowed_tables: ['public.metrics', 'public.people'] }, { id: 'csv', kind: 'csv_file', path: 'files/metrics.csv' }, { id: 'excel', kind: 'excel_file', path: 'files/people.xlsx' }] }
  metadata = { tables: [table('db', 'metrics'), table('db', 'people'), table('csv', 'csv_metrics'), table('excel', 'excel_people')], nodes: [], edges: [] }
  api.request.mockReset(); api.ensureSession.mockReset().mockResolvedValue(undefined); api.clearSessions.mockReset(); api.clearSession.mockReset()
  api.request.mockImplementation(async (url: string, options?: { method: string; body?: string }) => {
    if (options?.method === 'DELETE') {
      if (deletionFailure) throw new Error('删除失败测试')
      const parts = url.split('/')
      if (parts.length === 3) projects = projects.filter(project => project.project_id !== parts[2])
      else if (parts[3] === 'files') {
        const removed = projects[0]!.files.find(file => file.file_id === parts[4])!
        projects[0]!.files = projects[0]!.files.filter(file => file.file_id !== parts[4])
        if (config.agent_config === removed.relative_path) { delete config.agent_config; config.model = { ...config.model, mode: 'disabled' } }
        config.sources = config.sources?.filter(source => source.path !== removed.relative_path)
        config.documents = (config.documents as string[]).filter(path => path !== removed.relative_path)
        metadata.tables = metadata.tables.filter(table => config.sources?.some(source => source.id === table.source_id))
      } else if (parts[3] === 'sources') {
        if (parts[5] === 'tables') {
          const selectedTable = JSON.parse(options.body!).table
          const source = config.sources!.find(source => source.id === parts[4])!
          source.allowed_tables = (source.allowed_tables as string[]).filter(name => name !== selectedTable)
          metadata.tables = metadata.tables.filter(table => table.source_id !== source.id || `${table.schema}.${table.name}` !== selectedTable)
        } else {
          config.sources = config.sources?.filter(source => source.id !== parts[4]); metadata.tables = metadata.tables.filter(table => table.source_id !== parts[4])
        }
        config.documents = []
      }
      return structuredClone({ deleted: true, ...(parts.length > 3 ? { project: projects[0], config, detached_rule_paths: ['rules/relations.yaml'] } : {}) })
    }
    if (url === '/projects') return structuredClone({ projects })
    if (url.endsWith('/config')) return structuredClone(config)
    if (url.endsWith('/metadata')) return structuredClone(metadata)
    if (url.includes('/metadata/yaml?')) return { files: [] }
    if (url.includes('/graph?')) return { nodes: [], edges: [] }
    if (url.endsWith('/runs')) return { runs: running ? [{ run_id: 'active', status: 'running', stage: 'starting' }] : [] }
    if (url.endsWith('/credentials')) return { database_sources: [], model: false }
    const project = projects.find(project => url === `/projects/${project.project_id}`)
    if (project) return structuredClone(project)
    throw new Error(`Unexpected ${url}`)
  })
})
afterEach(() => { app?.unmount(); app = undefined; root?.remove(); sessionStorage.clear(); vi.restoreAllMocks() })

describe('删除项目与输入', () => {
  it('项目删除先确认；取消不发请求；失败可重试，成功清理会话并切换项目', async () => {
    mount(); await ready(); button('删除项目').click(); await nextTick()
    expect(dialog()?.textContent).toContain('运行记录和图谱结果')
    await vi.waitFor(() => expect(document.activeElement).toBe(button('取消')))
    button('取消').click(); await nextTick(); expect(dialog()).toBeNull(); expect(deletes()).toHaveLength(0)
    deletionFailure = true; button('删除项目').click(); await nextTick(); button('确认删除').click()
    await vi.waitFor(() => expect(dialog()?.textContent).toContain('删除失败测试'))
    expect(root.querySelector('.project.active')?.textContent).toContain('first')
    deletionFailure = false; button('确认删除').click()
    await vi.waitFor(() => expect(root.querySelector('.project.active')?.textContent).toContain('next'))
    expect(dialog()).toBeNull(); expect(api.clearSession).toHaveBeenCalledWith('first')
    expect(JSON.parse(sessionStorage.getItem(workbenchLocationKey)!)).toEqual({ projectId: 'next' })
  })
  it('删除最后一个项目后显示空工作台，刷新位置与旧图谱清空', async () => {
    projects = [projects[0]!]; new WorkbenchLocation().selectProject('first')
    mount(); await ready(); button('删除项目').click(); await nextTick(); button('确认删除').click()
    await vi.waitFor(() => expect(root.textContent).toContain('从一个项目开始'))
    expect(root.querySelector('.input-workspace')).toBeNull(); expect(sessionStorage.getItem(workbenchLocationKey)).toBeNull()
  })
  it('移除选中的数据库表后自动选下一张，暂停的绑定文档可见', async () => {
    mount(); await ready(); labelled('移除数据表 db.public.metrics').click(); await nextTick()
    expect(dialog()?.textContent).toContain('外部数据表和已有图谱结果保留')
    button('确认删除').click()
    await vi.waitFor(() => expect(root.querySelector('.input-item.active')?.textContent).toContain('public.people'))
    expect(deletes()[0]).toEqual(['/projects/first/sources/db/tables', { method: 'DELETE', body: JSON.stringify({ table: 'public.metrics' }) }])
    expect(root.querySelector('.input-metadata')?.textContent).not.toContain('metrics_name')
    button('知识文档').click(); await nextTick(); expect(root.textContent).toContain('未参与抽取')
  })
  it('删除数据库来源，输入区自动切到剩余本地文件', async () => {
    mount(); await ready(); labelled('删除数据库来源 db').click(); await nextTick(); button('确认删除').click()
    await vi.waitFor(() => expect(root.querySelector('.input-item.active')?.textContent).toContain('metrics.csv'))
    expect(deletes()[0]?.[0]).toBe('/projects/first/sources/db')
    expect(root.querySelector('.source-heading')).toBeNull()
  })
  it.each([['数据文件 metrics.csv', 'csv'], ['数据文件 people.xlsx', 'excel'], ['配置文件 tables.yaml', 'yaml'], ['文档 guide.md', 'md']])('删除%s，成功后移除列表项', async (label, id) => {
    mount(); await ready()
    if (id === 'md') button('知识文档').click()
    else [...root.querySelectorAll<HTMLButtonElement>('.input-types button')].find(button => button.textContent?.includes('本地文件'))!.click()
    await nextTick(); labelled(`删除${label}`).click(); await nextTick(); button('确认删除').click()
    await vi.waitFor(() => expect(root.querySelector(`[aria-label="删除${label}"]`)).toBeNull())
    expect(deletes()[0]?.[0]).toBe(`/projects/first/files/${id}`)
    if (id === 'csv') expect(root.querySelector('.input-item.active')?.textContent).toContain('people.xlsx')
  })
  it('删除输入后保留未保存的目标与模型，不会发送保存配置请求', async () => {
    mount(); await ready(); button('抽取目标').click(); await nextTick(); await input('purpose', '还没保存的目标')
    button('Agent 与模型').click(); await nextTick(); await input('model-name', 'unsaved-model')
    button('数据接入').click(); await nextTick(); labelled('移除数据表 db.public.metrics').click(); await nextTick(); button('确认删除').click()
    await vi.waitFor(() => expect(dialog()).toBeNull()); await vi.waitFor(() => expect(button('删除项目').disabled).toBe(false))
    button('抽取目标').click(); await nextTick(); expect(root.querySelector<HTMLTextAreaElement>('#purpose')?.value).toBe('还没保存的目标')
    button('Agent 与模型').click(); await nextTick(); expect(root.querySelector<HTMLInputElement>('#model-name')?.value).toBe('unsaved-model')
    expect(api.request.mock.calls.some(call => call[1]?.method === 'PUT')).toBe(false)
  })
  it('删除 Agent 配置文件时保留未保存的目标，但接受服务端关闭模型的设置', async () => {
    config.agent_config = 'files/tables.yaml'; config.model = { mode: 'approved_external' }
    mount(); await ready(); button('抽取目标').click(); await nextTick(); await input('purpose', '保留目标草稿')
    button('数据接入').click(); await nextTick()
    ;[...root.querySelectorAll<HTMLButtonElement>('.input-types button')].find(button => button.textContent?.includes('本地文件'))!.click(); await nextTick()
    labelled('删除配置文件 tables.yaml').click(); await nextTick(); button('确认删除').click()
    await vi.waitFor(() => expect(dialog()).toBeNull()); await vi.waitFor(() => expect(button('删除项目').disabled).toBe(false))
    button('抽取目标').click(); await nextTick(); expect(root.querySelector<HTMLTextAreaElement>('#purpose')?.value).toBe('保留目标草稿')
    button('Agent 与模型').click(); await nextTick(); expect(root.querySelector<HTMLSelectElement>('#model-mode')?.value).toBe('disabled')
  })
  it('删除文件失败后保留文件与元数据，取消确认不改动列表', async () => {
    mount(); await ready()
    ;[...root.querySelectorAll<HTMLButtonElement>('.input-types button')].find(button => button.textContent?.includes('本地文件'))!.click(); await nextTick()
    deletionFailure = true; labelled('删除数据文件 metrics.csv').click(); await nextTick(); button('确认删除').click()
    await vi.waitFor(() => expect(dialog()?.textContent).toContain('删除失败测试'))
    expect(root.querySelector('.input-item.active')?.textContent).toContain('metrics.csv')
    expect(root.querySelector('.input-metadata')?.textContent).toContain('csv_metrics_name')
    button('取消').click(); await nextTick(); expect(dialog()).toBeNull()
    expect(labelled('删除数据文件 metrics.csv').disabled).toBe(false)
  })
  it('删除已成功但元数据刷新失败时立即移除条目，提示关联规则停用，不显示重试删除', async () => {
    const originalRequest = api.request.getMockImplementation()!
    api.request.mockImplementation(async (...args) => {
      if (deletes().length && args[0] === '/projects/first/metadata') throw new Error('元数据服务暂不可用')
      return originalRequest(...args)
    })
    mount(); await ready()
    labelled('移除数据表 db.public.metrics').click(); await nextTick(); button('确认删除').click()
    await vi.waitFor(() => expect(root.querySelector('.error-strip')?.textContent).toContain('已删除，但列表刷新失败'))
    expect(root.querySelector('[aria-label="移除数据表 db.public.metrics"]')).toBeNull()
    expect(root.querySelector('.input-item.active')?.textContent).toContain('public.people')
    expect(dialog()).toBeNull(); expect(deletes()).toHaveLength(1)
    expect(root.querySelector('.toast')?.textContent).toContain('已停用 1 个关联规则文件')
    expect(root.querySelector('.input-metadata')?.textContent).not.toContain('metrics_name')
  })
  it('完整 JSON 尚未保存时阻止局部删除，避免之后保存复活旧来源', async () => {
    mount(); await ready(); button('Agent 与模型').click(); await nextTick(); await input('advanced-config', '{}')
    button('数据接入').click(); await nextTick(); labelled('删除数据库来源 db').click(); await nextTick()
    expect(dialog()).toBeNull(); expect(root.querySelector('.error-strip')?.textContent).toContain('完整项目配置有未保存修改')
    expect(deletes()).toHaveLength(0)
  })
  it('运行中禁用项目、来源、表、文件与文档删除', async () => {
    running = true; mount(); await ready()
    expect(button('删除项目').disabled).toBe(true)
    expect(labelled('删除数据库来源 db').disabled).toBe(true); expect(labelled('移除数据表 db.public.metrics').disabled).toBe(true)
    ;[...root.querySelectorAll<HTMLButtonElement>('.input-types button')].find(button => button.textContent?.includes('本地文件'))!.click(); await nextTick()
    expect(labelled('删除数据文件 metrics.csv').disabled).toBe(true)
    button('知识文档').click(); await nextTick(); expect(labelled('删除文档 guide.md').disabled).toBe(true)
    expect(deletes()).toHaveLength(0)
  })
})
