import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearSessions, ensureSession, request } from './api'
import { configurationTabs, resultTabs, isResultTab, connectionPayload, selectedTables, validateSourceId } from './onboarding'
import type { ConnectionForm } from './onboarding'
const form: ConnectionForm = { kind: 'mysql_database', format: 'fields', dsn: 'unused secret', host: ' db.local ', port: '3306', database: 'app', username: 'reader', password: ' p a s s ', schema: '' }
afterEach(() => { clearSessions(); vi.unstubAllGlobals() })
describe('配置与运行分离', () => {
  it('模型和目标只在配置阶段，图谱和日志只在结果阶段', () => {
    expect(configurationTabs.map(item => item.id)).toEqual(['inputs', 'documents', 'goal', 'settings'])
    expect(resultTabs.map(item => item.id)).toEqual(['metadata', 'business', 'runs'])
    expect(isResultTab('settings')).toBe(false)
    expect(isResultTab('metadata')).toBe(true)
  })
})
describe('直接连接输入', () => {
  it('保留密码原文，不将未采用的连接串一并发送', () => {
    expect(connectionPayload(form)).toEqual({ kind: 'mysql_database', host: 'db.local', port: 3306, database: 'app', username: 'reader', password: ' p a s s ' })
    expect(connectionPayload({ ...form, format: 'dsn', dsn: 'mysql://reader:private@db.local/app', schema: ' scope ' })).toEqual({ kind: 'mysql_database', dsn: 'mysql://reader:private@db.local/app', schema: 'scope' })
  })
  it('非法端口与缺少地址先在本地报告', () => {
    expect(() => connectionPayload({ ...form, port: '99999' })).toThrow('端口')
    expect(() => connectionPayload({ ...form, host: '' })).toThrow('主机')
  })
  it.each(['gaussdb', 'postgresql'])('JDBC %s 地址单独传递账号，保留密码特殊字符', protocol => {
    const dsn = `jdbc:${protocol}://db.example.com:8000/analytics`
    expect(connectionPayload({ ...form, kind: 'gauss_database', format: 'dsn', dsn: ` ${dsn} `, username: ' reader ', password: ' p@ss:/?#&%=+ ' })).toEqual({
      kind: 'gauss_database', dsn, username: 'reader', password: ' p@ss:/?#&%=+ ',
    })
  })
  it('JDBC 连接串可包含账号，不重复追加空凭据', () => {
    const dsn = 'jdbc:gaussdb://db.example.com:8000/analytics?user=reader&password=fake%40password'
    expect(connectionPayload({ ...form, kind: 'gauss_database', format: 'dsn', dsn, username: '', password: '' })).toEqual({ kind: 'gauss_database', dsn })
    expect(() => connectionPayload({ ...form, kind: 'gauss_database', format: 'dsn', dsn: 'jdbc:gaussdb://db.example.com:8000/analytics', username: '', password: '' })).toThrow('用户名')
  })
  it.each([
    'jdbc:gaussdb://reader:password@db.example.com:8000/analytics',
    'jdbc:gaussdb://db.example.com:8000',
    'jdbc:gaussdb://',
  ])('JDBC 不接受 authority 凭据或不完整地址：%s', dsn => {
    expect(() => connectionPayload({ ...form, kind: 'gauss_database', format: 'dsn', dsn })).toThrow('请检查 JDBC 连接串')
  })
  it('原生 Gauss 连接串不携带分项填写残留的账号密码', () => {
    const dsn = 'postgresql://dsn_reader:dsn_password@db.example.com:8000/analytics'
    expect(connectionPayload({ ...form, kind: 'gauss_database', format: 'dsn', dsn })).toEqual({ kind: 'gauss_database', dsn })
  })
  it.each(['disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'] as const)('Gauss 分项填写明确传递 SSL 模式 %s', sslmode => {
    expect(connectionPayload({ ...form, kind: 'gauss_database', sslmode })).toMatchObject({ kind: 'gauss_database', sslmode })
  })
  it.each(['postgresql://reader:fake@db.example.com:8000/analytics?sslmode=verify-full', 'jdbc:gaussdb://db.example.com:8000/analytics?sslmode=verify-full'])(
    'SSL 选择独立于连接串，自动模式保留原参数：%s', dsn => {
      const config: ConnectionForm = { ...form, kind: 'gauss_database', format: 'dsn', dsn }
      expect(connectionPayload({ ...config, sslmode: 'auto' })).not.toHaveProperty('sslmode')
      expect(connectionPayload({ ...config, sslmode: 'prefer' })).toMatchObject({ dsn, sslmode: 'prefer' })
    },
  )
  it('自动模式不覆盖已有默认，MySQL 不发送残留的 Gauss SSL 选择', () => {
    expect(connectionPayload({ ...form, kind: 'gauss_database', sslmode: 'auto' })).not.toHaveProperty('sslmode')
    expect(connectionPayload({ ...form, sslmode: 'disable' })).not.toHaveProperty('sslmode')
    expect(connectionPayload({ ...form, format: 'dsn', dsn: 'mysql://reader:fake@db.local/app', sslmode: 'disable' })).not.toHaveProperty('sslmode')
  })
  it('合并发现选表、手工表名和导入表单，不默默扩大范围', () => {
    expect(selectedTables('app.manual\napp.selected\n', ['app.selected'])).toEqual(['app.selected', 'app.manual'])
    expect(() => selectedTables('', [])).toThrow('至少')
    expect(() => selectedTables('app.*', [])).toThrow('通配符')
    expect(() => validateSourceId('source', null, [{ id: 'source', kind: 'csv_file' }])).toThrow('已经存在')
  })
})
describe('会话凭据只在内存中按项目隔离', () => {
  it('并行请求只建立一次会话，不向其他项目或项目列表发送 token', async () => {
    const fetcher = vi.fn().mockImplementation(async (url: string) => new Response(JSON.stringify(url.endsWith('/session') ? { session_id: 'private-token' } : {})))
    vi.stubGlobal('fetch', fetcher)
    const storage = { getItem: vi.fn(), setItem: vi.fn() }
    vi.stubGlobal('localStorage', storage); vi.stubGlobal('sessionStorage', storage)
    await Promise.all([ensureSession('project-a'), ensureSession('project-a')])
    await request('/projects/project-a/database/test', { method: 'POST', body: '{}' })
    await request('/projects/project-b/credentials')
    await request('/projects')
    expect(fetcher.mock.calls.filter(call => call[0].endsWith('/session'))).toHaveLength(1)
    expect(fetcher.mock.calls[1]?.[1].headers).toMatchObject({ 'X-Workbench-Session': 'private-token' })
    expect(fetcher.mock.calls[2]?.[1].headers).not.toHaveProperty('X-Workbench-Session')
    expect(fetcher.mock.calls[3]?.[1].headers).not.toHaveProperty('X-Workbench-Session')
    expect(storage.getItem).not.toHaveBeenCalled(); expect(storage.setItem).not.toHaveBeenCalled()
    clearSessions()
    await request('/projects/project-a/credentials')
    expect(fetcher.mock.calls[4]?.[1].headers).not.toHaveProperty('X-Workbench-Session')
  })
  it('失败后可重建会话，API 错误不反射凭据', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('{"error":{"code":"session_invalid","message":"private-token"}}', { status: 403 })).mockResolvedValueOnce(new Response('{"session_id":"new-token"}')))
    await expect(ensureSession('a')).rejects.toThrow('会话')
    await expect(ensureSession('a')).resolves.toBeUndefined()
  })
})
