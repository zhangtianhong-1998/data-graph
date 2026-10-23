// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import type { App } from 'vue'
import DatabaseSourceDialog from './DatabaseSourceDialog.vue'

const api = vi.hoisted(() => ({ request: vi.fn(), ensureSession: vi.fn() }))
vi.mock('./api', () => ({
  request: api.request,
  ensureSession: api.ensureSession,
  projectPath: (id: string) => `/projects/${id}`,
  json: JSON.stringify,
}))

let app: App | undefined
let root: HTMLDivElement
let onClose: ReturnType<typeof vi.fn>

function mount() {
  root = document.createElement('div')
  document.body.append(root)
  onClose = vi.fn()
  app = createApp(DatabaseSourceDialog, {
    projectId: 'dialog-test', source: null, sources: [], files: [],
    retentionVerified: false, busy: false, error: '', onClose,
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

beforeEach(() => {
  api.request.mockReset()
  api.ensureSession.mockReset().mockResolvedValue(undefined)
  mount()
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
    await nextTick()
    expect(onClose).toHaveBeenCalledOnce()
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
    listing.resolve({ tables: [{ schema: 'analytics', name: 'metrics', qualified_name: 'analytics.metrics', table_type: 'BASE TABLE' }], truncated: false })
    await vi.waitFor(() => expect(root.querySelector('.table-choice')?.textContent).toContain('analytics.metrics'))
    expect(element('.dialog-feedback').textContent).toContain('连接成功，已读取 1 张表')
    expect(button('测试连接').disabled).toBe(false)
    expect(api.ensureSession).not.toHaveBeenCalled()
  })

  it.each(['gaussdb', 'postgresql'])('粘贴 JDBC %s 地址后可单独填账号，测试与表发现使用同一凭据', async protocol => {
    const dsn = `jdbc:${protocol}://db.example.com:8000/analytics`
    await useDsn(dsn)
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
