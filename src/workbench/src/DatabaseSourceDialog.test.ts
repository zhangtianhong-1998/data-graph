// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App } from 'vue'
import type { SourceConfig } from './types'
import DatabaseSourceDialog from './DatabaseSourceDialog.vue'

const api = vi.hoisted(() => ({ request: vi.fn(), draftRequest: vi.fn(), ensureSession: vi.fn() }))
vi.mock('./api', async importOriginal => ({
  ...await importOriginal<typeof import('./api')>(),
  request: (path: string, options: Record<string, unknown>) => path.includes('/database/drafts/') ? api.draftRequest(path, options) : api.request(path, options),
  ensureSession: api.ensureSession,
  projectPath: (id: string) => `/projects/${id}`,
  json: JSON.stringify,
}))

let app: App | undefined
let root: HTMLDivElement
let onClose: ReturnType<typeof vi.fn>
let onConnected: ReturnType<typeof vi.fn>

function mount(source: SourceConfig | null = null) {
  root = document.createElement('div')
  document.body.append(root)
  onClose = vi.fn(); onConnected = vi.fn()
  app = createApp(DatabaseSourceDialog, {
    projectId: 'dialog-test', source, sources: source ? [source] : [], files: [],
    retentionVerified: false, busy: false, error: '', configRevision: '"4"', onClose, onConnected,
  })
  app.mount(root)
}

function element<T extends Element = HTMLElement>(selector: string): T {
  const result = root.querySelector<T>(selector)
  if (!result) throw new Error(`Missing dialog element: ${selector}`)
  return result
}

function button(label: string): HTMLButtonElement {
  const result = [...root.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  if (!result) throw new Error(`Missing dialog button: ${label}`)
  return result
}

async function inputValue(selector: string, value: string) {
  const input = element<HTMLInputElement>(selector)
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}

async function useDsn(dsn: string) {
  element<HTMLInputElement>('.connection-format input[value="dsn"]').click()
  await nextTick()
  await inputValue('#database-dsn', dsn)
}

async function selectValue(selector: string, value: string) {
  const select = element<HTMLSelectElement>(selector)
  select.value = value
  select.dispatchEvent(new Event('change', { bubbles: true }))
  await nextTick()
}

async function fillConnection(kind = 'gauss_database') {
  const select = element<HTMLSelectElement>('#database-kind')
  select.value = kind
  select.dispatchEvent(new Event('change', { bubbles: true }))
  await nextTick()
  for (const [id, value] of Object.entries({
    'database-id': 'fake-source', 'database-host': 'example.invalid',
    'database-name': 'fixture_db', 'database-username': 'fake_reader',
    'database-password': 'fake-password-for-tests',
  })) {
    const input = element<HTMLInputElement>(`#${id}`)
    input.value = value
    input.dispatchEvent(new Event('input', { bubbles: true }))
  }
  await nextTick()
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

beforeEach(async () => {
  api.draftRequest.mockReset().mockImplementation((_path: string, options: RequestInit = {}) => Promise.resolve({ saved: options.method === 'PUT', revision: options.body ? JSON.parse(String(options.body)).revision : 0 }))
  api.request.mockReset()
  api.ensureSession.mockReset().mockResolvedValue(undefined)
  mount()
  await vi.waitFor(() => expect(button('测试连接').disabled).toBe(false))
  api.ensureSession.mockClear(); api.draftRequest.mockClear()
})
afterEach(() => { app?.unmount(); app = undefined; root.remove() })

describe('数据库连接弹窗', () => {
  it('移出弹窗、释放鼠标或点击遮罩不会关闭或清空填写内容', async () => {
    await fillConnection()
    const dialog = element('[role="dialog"]')
    dialog.dispatchEvent(new Event('pointerleave'))
    dialog.dispatchEvent(new Event('mouseleave'))
    dialog.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    const backdrop = element('.modal-backdrop')
    backdrop.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
    backdrop.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()
    expect(onClose).not.toHaveBeenCalled()
    expect(element<HTMLInputElement>('#database-host').value).toBe('example.invalid')
    expect(element<HTMLInputElement>('#database-password').value).toBe('fake-password-for-tests')
  })

  it('显式关闭会清除凭据并通知父组件', async () => {
    await fillConnection()
    element<HTMLButtonElement>('button[aria-label="关闭"]').click()
    await vi.waitFor(() => expect(onClose).toHaveBeenCalledOnce())
    expect(element<HTMLInputElement>('#database-password').value).toBe('')
  })

  it('缺少连接字段时立即显示错误，不请求会话或数据库', async () => {
    button('测试连接').click()
    await nextTick()
    const alert = element('[role="alert"]')
    expect(alert.textContent).toContain('请填写主机、数据库名和用户名')
    expect(alert.closest('.dialog-feedback')).not.toBeNull()
    expect(element('.panel-body').contains(alert)).toBe(false)
    expect(api.ensureSession).not.toHaveBeenCalled()
    expect(api.request).not.toHaveBeenCalled()
    expect(button('测试连接').disabled).toBe(false)
  })

  it('连接失败在正文滚动区域外显示错误，保留表单并允许重试', async () => {
    await fillConnection()
    api.request.mockRejectedValueOnce(new Error('数据库连接超时，请检查地址。'))
    button('测试连接').click()
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')).not.toBeNull())
    const alert = element('[role="alert"]')
    expect(alert.textContent).toContain('数据库连接超时')
    expect(alert.closest('.dialog-feedback')).not.toBeNull()
    expect(element('.panel-body').contains(alert)).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    expect(element<HTMLInputElement>('#database-host').value).toBe('example.invalid')
    expect(button('测试连接').disabled).toBe(false)
    expect(api.request).toHaveBeenCalledOnce()
  })

  it.each(['gauss_database', 'mysql_database'])('%s 测试成功自动读取表清单，并分别显示两个阶段的进度', async kind => {
    await fillConnection(kind)
    api.ensureSession.mockRejectedValue(new Error('会话服务暂不可用'))
    const testing = deferred<unknown>(), listing = deferred<unknown>()
    api.request.mockReturnValueOnce(testing.promise).mockReturnValueOnce(listing.promise)
    button('测试连接').click()
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledOnce())
    expect(element('.dialog-feedback').textContent).toContain('正在测试连接')
    expect(button('测试连接').disabled).toBe(true)
    expect(JSON.parse(api.request.mock.calls[0]![1].body).connection.kind).toBe(kind)
    testing.resolve({ ok: true })
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledTimes(2))
    expect(api.request.mock.calls.map(call => call[0])).toEqual([
      '/projects/dialog-test/database/test', '/projects/dialog-test/database/tables',
    ])
    expect(element('.dialog-feedback').textContent).toContain('正在读取表清单')
    expect(api.request.mock.calls[0]![1].timeoutMs).toBe(30000)
    expect(api.request.mock.calls[1]![1].timeoutMs).toBe(45000)
    listing.resolve({ tables: [{ schema: 'analytics', name: 'metrics', qualified_name: 'analytics.metrics', table_type: 'BASE TABLE' }], truncated: false })
    await vi.waitFor(() => expect(root.querySelector('.table-choice')?.textContent).toContain('analytics.metrics'))
    expect(element('.dialog-feedback').textContent).toContain('连接成功，已读取 1 张表')
    expect(button('测试连接').disabled).toBe(false)
    expect(api.ensureSession).not.toHaveBeenCalled()
  })

  it.each(['gaussdb', 'postgresql'])('粘贴 JDBC %s 地址后可单独填账号，测试与表发现使用同一凭据', async protocol => {
    const dsn = `jdbc:${protocol}://db.example.com:8000/analytics`
    await useDsn(dsn)
    expect(element<HTMLInputElement>('#database-dsn').type).toBe('text')
    expect(element<HTMLInputElement>('#database-dsn').autocomplete).toBe('off')
    await inputValue('#database-username', 'reader')
    await inputValue('#database-password', ' p@ss:/?#&%=+ ')
    api.request.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({
      tables: [{ schema: 'public', name: 'metrics', qualified_name: 'public.metrics' }], truncated: false,
    })
    button('测试连接').click()
    await vi.waitFor(() => expect(root.querySelector('.table-choice')?.textContent).toContain('public.metrics'))
    expect(api.request.mock.calls.map(call => call[0])).toEqual([
      '/projects/dialog-test/database/test', '/projects/dialog-test/database/tables',
    ])
    for (const call of api.request.mock.calls) expect(JSON.parse(call[1].body)).toEqual({
      connection: { kind: 'gauss_database', dsn, username: 'reader', password: ' p@ss:/?#&%=+ ' },
    })
    expect(element('.dialog-feedback').textContent).toContain('连接成功，已读取 1 张表')
  })

  it('JDBC 缺少用户名时直接提示，不发送连接请求', async () => {
    await useDsn('jdbc:gaussdb://db.example.com:8000/analytics')
    button('测试连接').click()
    await nextTick()
    expect(element('[role="alert"]').textContent).toContain('请填写数据库用户名')
    expect(api.request).not.toHaveBeenCalled()
  })

  it('普通 Gauss 连接串仍可连接，隐藏且不发送分项填写残留的凭据', async () => {
    await fillConnection()
    const dsn = 'postgresql://dsn_reader:dsn_password@db.example.com:8000/analytics'
    await useDsn(dsn)
    expect(root.querySelector('#database-username')).toBeNull()
    expect(root.querySelector('#database-password')).toBeNull()
    api.request.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ tables: [], truncated: false })
    button('测试连接').click()
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledTimes(2))
    for (const call of api.request.mock.calls) expect(JSON.parse(call[1].body)).toEqual({ connection: { kind: 'gauss_database', dsn } })
  })

  it.each(['fields', 'postgresql', 'jdbc'])('%s 连接传递显式 SSL 选择，自动模式不覆盖连接串', async format => {
    await fillConnection()
    if (format === 'postgresql') await useDsn('postgresql://reader:fake@db.example.com:8000/analytics?sslmode=verify-full')
    if (format === 'jdbc') await useDsn('jdbc:gaussdb://db.example.com:8000/analytics?sslmode=verify-full')
    expect(element<HTMLSelectElement>('#database-sslmode').value).toBe('auto')
    await selectValue('#database-sslmode', 'prefer')
    expect(element('#database-sslmode-hint').textContent).toContain('可能使用未加密连接')
    api.request.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ tables: [], truncated: false })
    button('测试连接').click()
    await vi.waitFor(() => expect(element('.dialog-feedback').textContent).toContain('连接成功，已读取'))
    expect(api.request).toHaveBeenCalledTimes(2)
    for (const call of api.request.mock.calls) {
      const payload = JSON.parse(call[1].body).connection
      expect(payload.sslmode).toBe('prefer')
      if (format !== 'fields') expect(payload.dsn).toContain('sslmode=verify-full')
    }
    api.request.mockClear().mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ tables: [], truncated: false })
    await selectValue('#database-sslmode', 'auto')
    button('测试连接').click()
    await vi.waitFor(() => expect(element('.dialog-feedback').textContent).toContain('连接成功，已读取'))
    expect(api.request).toHaveBeenCalledTimes(2)
    for (const call of api.request.mock.calls) expect(JSON.parse(call[1].body).connection).not.toHaveProperty('sslmode')
  })

  it('修改 SSL 模式会使连接成功状态与已选表失效，避免复用旧连接结果', async () => {
    await fillConnection()
    api.request.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({
      tables: [{ schema: 'public', name: 'metrics', qualified_name: 'public.metrics' }], truncated: false,
    })
    button('测试连接').click()
    await vi.waitFor(() => expect(root.querySelector('.table-choice')).not.toBeNull())
    element<HTMLInputElement>('.table-choice input').click()
    await nextTick()
    expect(element('.manual-tables').textContent).toContain('已选 1 张表')
    await selectValue('#database-sslmode', 'require')
    expect(root.querySelector('.table-choice')).toBeNull()
    expect(root.querySelector('.badge.green')).toBeNull()
    expect(element('.manual-tables').textContent).toContain('已选 0 张表')
    expect(element('#database-sslmode-hint').textContent).toContain('不校验服务器主机名')
  })

  it('切换 MySQL 后隐藏 SSL 控件且不提交 Gauss 的 SSL 值', async () => {
    await fillConnection()
    await selectValue('#database-sslmode', 'disable')
    await selectValue('#database-kind', 'mysql_database')
    expect(root.querySelector('#database-sslmode')).toBeNull()
    api.request.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ tables: [], truncated: false })
    button('测试连接').click()
    await vi.waitFor(() => expect(element('.dialog-feedback').textContent).toContain('连接成功，已读取'))
    expect(api.request).toHaveBeenCalledTimes(2)
    for (const call of api.request.mock.calls) expect(JSON.parse(call[1].body).connection).not.toHaveProperty('sslmode')
    await selectValue('#database-kind', 'gauss_database')
    expect(element<HTMLSelectElement>('#database-sslmode').value).toBe('disable')
  })

  it('请求等待期间重复点击不会发出第二次测试，关闭按钮也暂不可用', async () => {
    await fillConnection()
    const testing = deferred<unknown>()
    api.request.mockReturnValueOnce(testing.promise).mockResolvedValueOnce({ tables: [], truncated: false })
    const testButton = button('测试连接')
    // 两个事件处于同一轮，覆盖 Vue 尚未把 disabled 渲染到 DOM 的间隙。
    testButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    testButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledOnce())
    expect(api.ensureSession).not.toHaveBeenCalled()
    element<HTMLButtonElement>('button[aria-label="关闭"]').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(onClose).not.toHaveBeenCalled()
    expect(element<HTMLButtonElement>('button[aria-label="关闭"]').disabled).toBe(true)
    testing.resolve({ ok: true })
    await vi.waitFor(() => expect(button('测试连接').disabled).toBe(false))
    expect(api.request).toHaveBeenCalledTimes(2)
  })
})


const savedSource: SourceConfig = { id: 'saved-source', kind: 'gauss_database', allowed_tables: ['analytics.metrics'], dsn_env: 'PROJECT_DSN' }
const savedFields = { kind: 'gauss_database', host: 'saved.example.invalid', port: 8000, database: 'saved_db', username: 'saved_reader', password: ' test @:/password ', schema: 'analytics', sslmode: 'prefer' }
function reopenSource(source = savedSource) { app?.unmount(); root.remove(); mount(source) }

describe('数据库连接本机保存与恢复', () => {
  it('新建默认勾选保存，提交时传递选择，测试连接不会写入持久化设置', async () => {
    await fillConnection()
    expect(element<HTMLInputElement>('#remember-connection').checked).toBe(true)
    await inputValue('#database-tables', 'public.metrics')
    api.request.mockResolvedValueOnce({ config: {}, metadata: {} })
    element('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await vi.waitFor(() => expect(onConnected).toHaveBeenCalledOnce())
    expect(api.ensureSession).toHaveBeenCalledWith('dialog-test')
    expect(api.request.mock.calls[0]![0]).toBe('/projects/dialog-test/database/connect')
    expect(JSON.parse(api.request.mock.calls[0]![1].body).remember_connection).toBe(true)
  })

  it('关闭保存选项时提交 false，密码仍可用于当前连接', async () => {
    await fillConnection()
    element<HTMLInputElement>('#remember-connection').click()
    await inputValue('#database-tables', 'public.metrics')
    api.request.mockResolvedValueOnce({ config: {}, metadata: {} })
    element('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await vi.waitFor(() => expect(onConnected).toHaveBeenCalledOnce())
    const payload = JSON.parse(api.request.mock.calls[0]![1].body)
    expect(payload.remember_connection).toBe(false)
    expect(payload.connection.password).toBe('fake-password-for-tests')
  })

  it('重开来源先建立会话，再恢复字段、SSL 和账号，不改变来源或选表', async () => {
    const loading = deferred<unknown>()
    api.request.mockReturnValueOnce(loading.promise)
    reopenSource()
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledOnce())
    expect(api.ensureSession).toHaveBeenCalledWith('dialog-test')
    expect(api.request.mock.calls[0]![0]).toBe('/projects/dialog-test/database/connections/saved-source')
    expect(element('.dialog-feedback').textContent).toContain('正在加载已保存连接')
    expect(button('测试连接').disabled).toBe(true)
    loading.resolve({ saved: true, connection: savedFields })
    await vi.waitFor(() => expect(button('测试连接').disabled).toBe(false))
    expect(element<HTMLInputElement>('#database-id').value).toBe('saved-source')
    expect(element<HTMLTextAreaElement>('#database-tables').value).toBe('analytics.metrics')
    expect(element<HTMLInputElement>('#database-host').value).toBe(savedFields.host)
    expect(element<HTMLInputElement>('#database-port').value).toBe('8000')
    expect(element<HTMLInputElement>('#database-name').value).toBe(savedFields.database)
    expect(element<HTMLInputElement>('#database-schema').value).toBe(savedFields.schema)
    expect(element<HTMLInputElement>('#database-username').value).toBe(savedFields.username)
    expect(element<HTMLInputElement>('#database-password').value).toBe(savedFields.password)
    expect(element<HTMLSelectElement>('#database-sslmode').value).toBe('prefer')
    expect(button('删除已保存连接')).toBeTruthy()
    expect(onConnected).not.toHaveBeenCalled()
    expect(api.request).toHaveBeenCalledOnce()
  })

  it.each(['jdbc:gaussdb://db.example.invalid:8000/analytics', 'postgresql://reader:fake@db.example.invalid:8000/analytics'])('恢复保存的连接串：%s', async dsn => {
    api.request.mockResolvedValueOnce({ saved: true, connection: { kind: 'gauss_database', dsn, username: 'reader', password: 'fake', schema: 'analytics', sslmode: 'require' } })
    reopenSource()
    await vi.waitFor(() => expect(root.querySelector('#database-dsn')).not.toBeNull())
    expect(element<HTMLInputElement>('#database-dsn').value).toBe(dsn)
    expect(element<HTMLInputElement>('#dsn-schema').value).toBe('analytics')
    expect(element<HTMLSelectElement>('#database-sslmode').value).toBe('require')
    if (dsn.startsWith('jdbc:')) expect(element<HTMLInputElement>('#database-password').value).toBe('fake')
    expect(element<HTMLTextAreaElement>('#database-tables').value).toBe('analytics.metrics')
  })

  it('未保存过的来源仍可填写，不显示删除按钮', async () => {
    api.request.mockResolvedValueOnce({ saved: false })
    reopenSource()
    await vi.waitFor(() => expect(button('测试连接').disabled).toBe(false))
    expect(element<HTMLInputElement>('#database-host').value).toBe('')
    expect(root.textContent).not.toContain('删除已保存连接')
    expect(element<HTMLTextAreaElement>('#database-tables').value).toBe('analytics.metrics')
  })

  it('删除保存只调用连接接口，保留当前表单与数据源', async () => {
    api.request.mockResolvedValueOnce({ saved: true, connection: savedFields })
    reopenSource()
    await vi.waitFor(() => expect(root.textContent).toContain('删除已保存连接'))
    api.request.mockResolvedValueOnce({ saved: false })
    button('删除已保存连接').click()
    await vi.waitFor(() => expect(root.textContent).not.toContain('删除已保存连接'))
    expect(api.request.mock.calls[1]![0]).toBe('/projects/dialog-test/database/connections/saved-source')
    expect(api.request.mock.calls[1]![1].method).toBe('DELETE')
    expect(element<HTMLInputElement>('#remember-connection').checked).toBe(false)
    expect(element<HTMLInputElement>('#database-host').value).toBe(savedFields.host)
    expect(element<HTMLTextAreaElement>('#database-tables').value).toBe('analytics.metrics')
    expect(element('.dialog-feedback').textContent).toContain('已删除保存的连接')
    expect(onConnected).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('删除失败保留删除按钮并显示错误，暂停自动保存防止复活', async () => {
    api.request.mockResolvedValueOnce({ saved: true, connection: savedFields })
    reopenSource()
    await vi.waitFor(() => expect(root.textContent).toContain('删除已保存连接'))
    api.request.mockRejectedValueOnce(new Error('连接信息保存或加载失败'))
    button('删除已保存连接').click()
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')).not.toBeNull())
    expect(element('[role="alert"]').textContent).toContain('连接信息保存或加载失败')
    expect(element<HTMLInputElement>('#remember-connection').checked).toBe(false)
    expect(button('删除已保存连接').disabled).toBe(false)
    api.request.mockResolvedValueOnce({ saved: false })
    button('重试删除').click()
    await vi.waitFor(() => expect(root.textContent).not.toContain('删除已保存连接'))
    expect(api.request.mock.calls.at(-1)![1].method).toBe('DELETE')
  })

  it('加载失败显示错误且不阻止手工重新填写', async () => {
    api.request.mockRejectedValueOnce(new Error('保存的连接信息无法解密'))
    reopenSource()
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')).not.toBeNull())
    expect(button('测试连接').disabled).toBe(false)
    expect(element('[role="alert"]').textContent).toContain('无法解密')
    await inputValue('#database-host', 'manual.example.invalid')
    expect(element<HTMLInputElement>('#database-host').value).toBe('manual.example.invalid')
  })

  it.each(['saved_connection_corrupt', 'saved_connection_key_missing'])('损坏或密钥缺失的保存仍可删除：%s', async code => {
    api.request.mockRejectedValueOnce(Object.assign(new Error('保存的连接信息无法解密'), { code }))
    reopenSource()
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')).not.toBeNull())
    api.request.mockResolvedValueOnce({ saved: false })
    button('删除已保存连接').click()
    await vi.waitFor(() => expect(root.textContent).not.toContain('删除已保存连接'))
    expect(api.request.mock.calls[1]![1].method).toBe('DELETE')
  })

  it('弹窗卸载取消加载，不使用迟到响应或触发连接提交', async () => {
    const loading = deferred<unknown>()
    api.request.mockReturnValueOnce(loading.promise)
    reopenSource()
    await vi.waitFor(() => expect(api.request).toHaveBeenCalledOnce())
    const signal = api.request.mock.calls[0]![1].signal as AbortSignal
    app?.unmount(); app = undefined
    expect(signal.aborted).toBe(true)
    loading.resolve({ saved: true, connection: savedFields })
    await nextTick()
    expect(root.querySelector('#database-host')).toBeNull()
    expect(onConnected).not.toHaveBeenCalled()
    expect(api.request).toHaveBeenCalledOnce()
  })

  it('发现表时显示跳过数量，不影响有效表选择', async () => {
    await fillConnection()
    api.request.mockResolvedValueOnce({ tables: [{ schema: 'public', name: 'metrics', qualified_name: 'public.metrics' }], truncated: false, skipped_count: 2 })
    button('读取表清单').click()
    await vi.waitFor(() => expect(root.textContent).toContain('已跳过 2 张不支持表名的表'))
    element<HTMLInputElement>('.table-choice input').click()
    await nextTick()
    expect(element('.manual-tables').textContent).toContain('已选 1 张表')
  })
})

describe('未连接成功的数据库草稿', () => {
  it('未填写来源编号或有效 JDBC 地址也自动保存，不测试数据库，不写浏览器存储', async () => {
    const localWrite = vi.spyOn(Storage.prototype, 'setItem')
    await useDsn('jdbc:gaussdb://unfinished-host:')
    await vi.waitFor(() => expect(api.draftRequest.mock.calls.some(call => call[1]?.method === 'PUT')).toBe(true))
    const write = api.draftRequest.mock.calls.find(call => call[1]?.method === 'PUT')!
    expect(write[0]).toBe('/projects/dialog-test/database/drafts/new')
    const payload = JSON.parse(write[1].body)
    expect(payload.draft.form.id).toBe('')
    expect(payload.draft.connection.dsn).toBe('jdbc:gaussdb://unfinished-host:')
    expect(Number.isSafeInteger(payload.revision)).toBe(true)
    expect(api.request).not.toHaveBeenCalled()
    expect(localWrite).not.toHaveBeenCalled()
    expect(element('.draft-feedback').textContent).toContain('已保存到本机')
    localWrite.mockRestore()
  })

  it('刷新后恢复未完成连接的完整输入，不需要先添加数据源', async () => {
    await useDsn('jdbc:gaussdb://partial')
    await inputValue('#database-id', 'new-draft')
    await inputValue('#database-username', 'draft-reader')
    await inputValue('#database-password', ' secret :@+= ')
    await inputValue('#database-tables', 'public.metrics\npublic.organizations')
    await vi.waitFor(() => expect(element('.draft-feedback').textContent).toContain('已保存到本机'))
    const saved = JSON.parse(api.draftRequest.mock.calls.filter(call => call[1]?.method === 'PUT').at(-1)![1].body)
    app?.unmount(); root.remove()
    api.draftRequest.mockResolvedValueOnce({ saved: true, ...saved })
    mount()
    await vi.waitFor(() => expect(root.querySelector('#database-dsn')).not.toBeNull())
    expect(element<HTMLInputElement>('#database-dsn').value).toBe('jdbc:gaussdb://partial')
    expect(element<HTMLInputElement>('#database-id').value).toBe('new-draft')
    expect(element<HTMLInputElement>('#database-username').value).toBe('draft-reader')
    expect(element<HTMLInputElement>('#database-password').value).toBe(' secret :@+= ')
    expect(element<HTMLTextAreaElement>('#database-tables').value).toContain('public.organizations')
    expect(api.request).not.toHaveBeenCalled()
  })

  it('恢复已选表不会被连接字段的 watcher 清空，成功连接后清除草稿', async () => {
    await fillConnection()
    api.request.mockResolvedValueOnce({ tables: [{ schema: 'public', name: 'metrics', qualified_name: 'public.metrics' }], truncated: false })
    button('读取表清单').click()
    await vi.waitFor(() => expect(root.querySelector('.table-choice')).not.toBeNull())
    element<HTMLInputElement>('.table-choice input').click()
    await vi.waitFor(() => expect(element('.draft-feedback').textContent).toContain('已保存到本机'))
    const saved = JSON.parse(api.draftRequest.mock.calls.filter(call => call[1]?.method === 'PUT').at(-1)![1].body)
    expect(saved.draft.checked).toEqual(['public.metrics'])
    app?.unmount(); root.remove()
    api.draftRequest.mockResolvedValueOnce({ saved: true, ...saved }); mount()
    await vi.waitFor(() => expect(root.querySelector('.draft-feedback')?.textContent).toContain('已保存到本机'))
    expect(element('.manual-tables').textContent).toContain('已选 1 张表')
    api.request.mockResolvedValueOnce({ config: {}, metadata: {} })
    element('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await vi.waitFor(() => expect(onConnected).toHaveBeenCalledOnce())
    expect(JSON.parse(api.request.mock.calls.filter(call => call[0].endsWith('/connect')).at(-1)![1].body).allowed_tables).toEqual(['public.metrics'])
    expect(api.draftRequest.mock.calls.filter(call => call[1]?.method === 'DELETE')).toHaveLength(1)
  })

  it('数据库测试失败仍保存连接草稿', async () => {
    await fillConnection()
    api.request.mockRejectedValueOnce(new Error('数据库连接失败'))
    button('测试连接').click()
    await vi.waitFor(() => expect(root.textContent).toContain('数据库连接失败'))
    await vi.waitFor(() => expect(element('.draft-feedback').textContent).toContain('已保存到本机'))
    expect(api.draftRequest.mock.calls.some(call => call[1]?.method === 'DELETE')).toBe(false)
    const payload = JSON.parse(api.draftRequest.mock.calls.filter(call => call[1]?.method === 'PUT').at(-1)![1].body)
    expect(payload.draft.connection.password).toBe('fake-password-for-tests')
  })

  it('立即刷新发送 keepalive 最新草稿且不被组件卸载取消', async () => {
    await useDsn('jdbc:gaussdb://last-character')
    window.dispatchEvent(new Event('pagehide'))
    const write = api.draftRequest.mock.calls.find(call => call[1]?.method === 'PUT')!
    expect(write).toBeTruthy()
    expect(write[1].keepalive).toBe(true)
    expect(write[1].signal).toBeUndefined()
    expect(JSON.parse(write[1].body).draft.connection.dsn).toBe('jdbc:gaussdb://last-character')
    app?.unmount(); app = undefined
    await nextTick()
    expect(write[1].signal).toBeUndefined()
  })

  it('显式关闭先等待保存完成', async () => {
    await fillConnection()
    const saving = deferred<unknown>()
    api.draftRequest.mockReturnValueOnce(saving.promise)
    element<HTMLButtonElement>('button[aria-label="关闭"]').click()
    await vi.waitFor(() => expect(api.draftRequest).toHaveBeenCalledOnce())
    expect(onClose).not.toHaveBeenCalled()
    saving.resolve({ saved: true, revision: JSON.parse(api.draftRequest.mock.calls[0]![1].body).revision })
    await vi.waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  })

  it('取消保存等待旧写入后删除，迟到响应不触发新保存', async () => {
    await fillConnection()
    const saving = deferred<unknown>()
    api.draftRequest.mockReturnValueOnce(saving.promise)
    element('#database-password').dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
    await vi.waitFor(() => expect(api.draftRequest).toHaveBeenCalledOnce())
    const previousRevision = JSON.parse(api.draftRequest.mock.calls[0]![1].body).revision
    element<HTMLInputElement>('#remember-connection').click()
    await nextTick()
    expect(api.draftRequest).toHaveBeenCalledOnce()
    saving.resolve({ saved: true, revision: previousRevision })
    await vi.waitFor(() => expect(api.draftRequest).toHaveBeenCalledTimes(2))
    const deletion = api.draftRequest.mock.calls[1]!
    expect(deletion[1].method).toBe('DELETE')
    expect(JSON.parse(deletion[1].body).revision).toBeGreaterThan(previousRevision)
    await inputValue('#database-password', 'not-persisted')
    window.dispatchEvent(new Event('pagehide'))
    await new Promise(resolve => setTimeout(resolve, 300))
    expect(api.draftRequest).toHaveBeenCalledTimes(2)
    expect(element('.draft-feedback').textContent).toContain('仅用于本次会话')
  })

  it('保存失败可见并可重试，不阻止继续编辑', async () => {
    await useDsn('jdbc:gaussdb://partial')
    api.draftRequest.mockRejectedValueOnce(new Error('本机存储权限不足'))
    await vi.waitFor(() => expect(element('.draft-feedback').textContent).toContain('本机存储权限不足'))
    expect(element<HTMLInputElement>('#database-dsn').disabled).toBe(false)
    button('重试保存').click()
    await vi.waitFor(() => expect(element('.draft-feedback').textContent).toContain('已保存到本机'))
    expect(api.draftRequest.mock.calls.filter(call => call[1]?.method === 'PUT')).toHaveLength(2)
  })

  it('损坏的新增草稿能清除并重新保存', async () => {
    app?.unmount(); root.remove()
    api.draftRequest.mockRejectedValueOnce(Object.assign(new Error('草稿无法解密'), { code: 'saved_connection_corrupt' }))
    mount()
    await vi.waitFor(() => expect(root.textContent).toContain('清除损坏草稿'))
    await inputValue('#database-host', 'restored.invalid')
    button('清除损坏草稿').click()
    await vi.waitFor(() => expect(element('.draft-feedback').textContent).toContain('已保存到本机'))
    expect(api.draftRequest.mock.calls.some(call => call[1]?.method === 'DELETE')).toBe(true)
    expect(element<HTMLInputElement>('#database-host').value).toBe('restored.invalid')
  })

  it('其他页面已更新草稿时不关闭，刷新也不会反向覆盖', async () => {
    await useDsn('jdbc:gaussdb://older')
    api.draftRequest.mockImplementationOnce((_path, options) => Promise.resolve({ saved: true, revision: JSON.parse(options.body).revision + 100 }))
    element<HTMLButtonElement>('button[aria-label="关闭"]').click()
    await vi.waitFor(() => expect(element('.draft-feedback').textContent).toContain('其他页面更新了连接草稿'))
    expect(onClose).not.toHaveBeenCalled()
    window.dispatchEvent(new Event('pagehide'))
    await nextTick()
    expect(api.draftRequest).toHaveBeenCalledOnce()
    button('重试保存').click()
    await vi.waitFor(() => expect(element('.draft-feedback').textContent).toContain('已保存到本机'))
    expect(api.draftRequest).toHaveBeenCalledTimes(2)
  })
})


describe('数据库能力清单入口', () => {
  it('上传后选中文件并保留服务端验证提示，连接提交绑定路径', async () => {
    mount(); await fillConnection();
    api.request.mockResolvedValueOnce({ file: { file_id: 'capability', name: 'capabilities.yaml', kind: 'yaml', relative_path: 'files/capabilities.yaml' }, runtime_ready: false, missing_capabilities: ['query_cancel'] })
    const input = element<HTMLInputElement>('input[aria-label="上传数据库能力清单"]')
    Object.defineProperty(input, 'files', { value: [new File(['db: {}'], 'capabilities.yaml')] })
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await vi.waitFor(() => expect(element<HTMLSelectElement>('#database-manifest').value).toBe('files/capabilities.yaml'))
    const uploaded = api.request.mock.calls.find(call => call[0].includes('/database/capabilities'))!
    expect(uploaded[0]).toContain('source_id=fake-source&kind=gauss_database')
    expect(uploaded[1].headers).toEqual({ 'If-Match': '"4"' })
    expect(uploaded[1].body).toBeInstanceOf(FormData)
    expect(root.textContent).toContain('query_cancel'); expect(root.textContent).toContain('不会绕过验证')
  })
})
