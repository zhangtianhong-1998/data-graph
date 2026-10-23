import type { SourceConfig } from './types'
import { splitLines } from './state'

export type ConfigurationTab = 'inputs' | 'documents' | 'goal' | 'settings'
export type ResultTab = 'metadata' | 'business' | 'runs'
export type WorkbenchTab = ConfigurationTab | ResultTab
export const configurationTabs: Array<{ id: ConfigurationTab; label: string }> = [
  { id: 'inputs', label: '数据接入' }, { id: 'documents', label: '知识文档' }, { id: 'goal', label: '抽取目标' }, { id: 'settings', label: 'Agent 与模型' },
]
export const resultTabs: Array<{ id: ResultTab; label: string }> = [
  { id: 'metadata', label: '元数据图' }, { id: 'business', label: '业务图谱' }, { id: 'runs', label: '运行记录' },
]
export function isResultTab(tab: WorkbenchTab): tab is ResultTab { return resultTabs.some(item => item.id === tab) }
export type GaussSslMode = 'auto' | 'disable' | 'allow' | 'prefer' | 'require' | 'verify-ca' | 'verify-full'
export interface ConnectionForm { kind: 'gauss_database' | 'mysql_database'; format: 'fields' | 'dsn'; dsn: string; host: string; port: string; database: string; username: string; password: string; schema: string; sslmode?: GaussSslMode }
export interface DatabaseTable { schema: string; name: string; qualified_name: string; table_type?: string }
export function isGaussJdbcDsn(dsn: string): boolean { return /^jdbc:(gaussdb|postgresql):\/\//.test(dsn.trim()) }
/** The returned object goes only to the ephemeral connection endpoint, never config. */
export function connectionPayload(form: ConnectionForm): Record<string, unknown> {
  // Auto leaves any DSN setting intact; explicit choices are resolved by the backend.
  const tls = form.kind === 'gauss_database' && form.sslmode && form.sslmode !== 'auto' ? { sslmode: form.sslmode } : {}
  if (form.format === 'dsn') {
    const dsn = form.dsn.trim()
    if (!dsn) throw new Error('请填写数据库连接串。')
    const credentials: Record<string, string> = {}
    if (form.kind === 'gauss_database' && isGaussJdbcDsn(dsn)) {
      let embeddedUser = ''
      try {
        const url = new URL(dsn.slice('jdbc:'.length))
        if (!url.hostname || !url.pathname || url.pathname === '/' || /^jdbc:[^:]+:\/\/[^/?#]*@/.test(dsn)) throw new Error('invalid JDBC address')
        embeddedUser = url.searchParams.get('user')?.trim() || ''
      } catch { throw new Error('请检查 JDBC 连接串，格式为 jdbc:gaussdb://主机:端口/数据库。') }
      if (!form.username.trim() && !embeddedUser) throw new Error('请填写数据库用户名；JDBC 地址通常不包含登录账号。')
      if (form.username.trim()) credentials.username = form.username.trim()
      // Keep special characters unchanged; the backend builds the driver connection string.
      if (form.password) credentials.password = form.password
    }
    return { kind: form.kind, dsn, ...credentials, ...tls, ...(form.schema.trim() ? { schema: form.schema.trim() } : {}) }
  }
  if (!form.host.trim() || !form.database.trim() || !form.username.trim()) throw new Error('请填写主机、数据库名和用户名。')
  const port = Number(form.port)
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('端口须为 1 到 65535 之间的整数。')
  return { kind: form.kind, host: form.host.trim(), port, database: form.database.trim(), username: form.username.trim(), password: form.password, ...tls, ...(form.schema.trim() ? { schema: form.schema.trim() } : {}) }
}
export function selectedTables(manual: string, checked: string[]): string[] {
  const tables = [...new Set([...checked, ...splitLines(manual)])]
  if (!tables.length) throw new Error('请勾选或填写至少一张表。')
  if (tables.some(name => name.includes('*'))) throw new Error('请逐一指定表名，不能使用通配符。')
  return tables
}
export function validateSourceId(id: string, source: SourceConfig | null, sources: SourceConfig[]) {
  if (source) {
    if (id !== source.id) throw new Error('已有来源编号不能修改。')
  } else if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(id)) throw new Error('来源编号须以字母或数字开头，只能包含字母、数字、短横线和下划线。')
  if (sources.some(item => item.id === id && item.id !== source?.id)) throw new Error('来源编号已经存在，请使用其他编号。')
}
