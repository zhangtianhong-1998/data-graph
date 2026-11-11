// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App as VueApp } from 'vue'
import App from './App.vue'
import type { Metadata, Project, RunConfig } from './types'

const api = vi.hoisted(() => ({ request: vi.fn(), ensureSession: vi.fn(), clearSessions: vi.fn(), clearSession: vi.fn() }))
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), ...api }))
let app: VueApp | undefined, root: HTMLDivElement, metadata: Metadata
const project: Project = { project_id: 'saved', name: '已保存项目', files: [], created_at: '2026-11-10', updated_at: '2026-11-10' }
const config: RunConfig = { goal: { purpose: '读取合成表结构' }, model: { mode: 'disabled' }, sources: [
  { id: 'warehouse', kind: 'gauss_database', credential_mode: 'session', credential_revision: 'saved-revision', allowed_tables: ['public.metrics'] },
] }
function mount() { root = document.createElement('div'); document.body.append(root); app = createApp(App); app.mount(root) }
function unmount() { app?.unmount(); app = undefined; root?.remove() }
function button(text: string) {
  const found = [...root.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.trim() === text)
  if (!found) throw new Error(`Missing ${text}`)
  return found
}
async function ready() { await vi.waitFor(() => expect(root.querySelector('.schema-table-list')?.textContent).toContain('metric_code')) }

beforeEach(() => {
  sessionStorage.clear()
  metadata = { tables: [{ source_id: 'warehouse', schema: 'public', name: 'metrics', columns: [{ name: 'metric_code', data_type: 'text' }] }], nodes: [], edges: [], using_saved_metadata: true }
  api.request.mockReset(); api.ensureSession.mockReset().mockResolvedValue(undefined); api.clearSessions.mockReset(); api.clearSession.mockReset()
  api.request.mockImplementation(async (url: string) => {
    if (url === '/projects') return { projects: [structuredClone(project)] }
    if (url === '/projects/saved') return structuredClone(project)
    if (url.endsWith('/config')) return structuredClone(config)
    if (url.endsWith('/metadata')) return structuredClone(metadata)
    if (url.includes('/metadata/yaml?')) return { files: [{ name: 'tables.yaml', content: 'table: metrics\ncolumns:\n  - metric_code' }] }
    if (url.includes('/graph')) throw new Error('图谱传输暂不可用')
    if (url.endsWith('/runs')) return { runs: [] }
    if (url.endsWith('/credentials')) return { database_sources: ['warehouse'], model: false }
    if (url.includes('/database/drafts/')) return { saved: false, revision: 0 }
    if (url.endsWith('/database/connections/warehouse')) return { saved: true, connection: {
      kind: 'gauss_database', host: 'fixture.invalid', port: 5432, database: 'synthetic', username: 'reader', password: 'synthetic-password', schema: 'public', sslmode: 'require',
    } }
    throw new Error(`Unexpected ${url}`)
  })
})
afterEach(() => { unmount(); sessionStorage.clear(); vi.restoreAllMocks() })

describe('页面重载后的数据库配置和元数据', () => {
  it('会话创建失败不会隐藏已经加载的数据库表和 YAML', async () => {
    api.ensureSession.mockRejectedValueOnce(new Error('会话暂不可用'))
    mount(); await ready()
    expect(root.querySelector('.error-strip')?.textContent).toContain('会话暂不可用')
    expect(root.querySelector('[aria-label="配置 warehouse"]')).not.toBeNull()
    expect(root.textContent).toContain('暂未恢复连接信息')
    button('YAML').click(); await nextTick()
    await vi.waitFor(() => expect(root.querySelector('.yaml-preview')?.textContent).toContain('metric_code'))
    expect(root.textContent).not.toContain('暂无项目')
  })

  it('重载后自动恢复连接状态；已保存连接仍可编辑，元数据浏览不重连', async () => {
    mount(); await ready(); unmount(); mount(); await ready()
    await vi.waitFor(() => expect(root.textContent).toContain('已恢复可用的连接信息'))
    button('YAML').click(); await nextTick()
    await vi.waitFor(() => expect(root.querySelector('.yaml-preview')?.textContent).toContain('table: metrics'))
    expect(api.request.mock.calls.some(([url]) => String(url).endsWith('/database/test') || String(url).endsWith('/database/tables'))).toBe(false)
    root.querySelector<HTMLButtonElement>('[aria-label="配置 warehouse"]')!.click()
    await vi.waitFor(() => expect(root.querySelector<HTMLInputElement>('#database-host')?.value).toBe('fixture.invalid'))
    expect(root.querySelector<HTMLInputElement>('#database-password')?.type).toBe('password')
    expect(root.textContent).not.toContain('synthetic-password')
    expect(root.querySelector<HTMLInputElement>('#database-name')?.value).toBe('synthetic')
  })

  it('旧元数据仍可查看，但明确提示刷新未成功', async () => {
    metadata.refresh_failed = true; metadata.requires_refresh = true
    mount(); await ready()
    expect(root.querySelector('.input-metadata')?.textContent).toContain('上次刷新元数据未完成')
    button('YAML').click(); await nextTick()
    await vi.waitFor(() => expect(root.querySelector('.yaml-preview')?.textContent).toContain('metric_code'))
  })

  it('项目缓存副本写入失败不会遮住有效元数据，并提示检查目录权限', async () => {
    metadata.metadata_cache_warning = 'metadata_preview_persistence_failed'
    mount(); await ready()
    expect(root.querySelector('.input-metadata')?.textContent).toContain('项目元数据副本未保存')
    button('YAML').click(); await nextTick()
    await vi.waitFor(() => expect(root.querySelector('.yaml-preview')?.textContent).toContain('metric_code'))
  })
})
