import type { SourceConfig } from './types'
import { splitLines } from './state'

export interface DatabaseForm {
  id: string
  kind: 'gauss_database' | 'mysql_database'
  dsnEnv: string
  tables: string
  capabilityManifest: string
}
export const isDatabaseSource = (source: SourceConfig): boolean => ['gauss_database', 'mysql_database'].includes(source.kind)
export const sourceKindLabel = (kind: string): string => ({ gauss_database: 'Gauss 数据库', mysql_database: 'MySQL 数据库', excel_workbook: 'Excel 工作簿', csv_file: 'CSV 文件' } as Record<string, string>)[kind] || kind

/** Update only the form-owned fields; column permissions and other advanced restrictions survive edits. */
export function updateDatabaseSources(sources: SourceConfig[], originalId: string | null, form: DatabaseForm, uploadedManifests: string[]): SourceConfig[] {
  const id = form.id.trim()
  if (!originalId && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(id)) throw new Error('来源编号须以字母或数字开头，只能包含字母、数字、短横线和下划线，最多 64 个字符。')
  if (originalId && originalId !== id) throw new Error('已有来源编号不能在此修改，避免文档绑定和图谱引用失效。')
  const original = originalId ? sources.find(source => source.id === originalId) : undefined
  if (originalId && !original) throw new Error('此来源已不存在，请关闭窗口后刷新项目。')
  if (original && !isDatabaseSource(original)) throw new Error('文件来源不能改为数据库连接。')
  if (sources.some(source => source.id === id && source.id !== originalId)) throw new Error('来源编号已经存在，请使用其他编号。')
  const dsnEnv = form.dsnEnv.trim()
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(dsnEnv)) throw new Error('请填写合法的服务器环境变量名，不要填写数据库连接串或密码。')
  const tables = [...new Set(splitLines(form.tables))]
  if (!tables.length) throw new Error('请至少填写一张允许查询的表。')
  if (tables.some(table => table.includes('*'))) throw new Error('允许查询的表必须逐一列出，不能使用通配符。')
  if (form.capabilityManifest && !uploadedManifests.includes(form.capabilityManifest)) throw new Error('能力验证文件必须从此项目已上传的 YAML 文件中选择。')
  const source: SourceConfig = { ...original, id, kind: form.kind, dsn_env: dsnEnv, allowed_tables: tables, capability_manifest: form.capabilityManifest || null }
  return original ? sources.map(item => item.id === originalId ? source : item) : [...sources, source]
}
