import { describe, expect, it } from 'vitest'
import { databaseFactDataAllowed, updateDatabaseSources } from './database'
import type { DatabaseForm } from './database'

const form: DatabaseForm = { id: 'business_mysql', kind: 'mysql_database', dsnEnv: 'MYSQL_DSN', tables: 'analytics.metrics\nanalytics.metrics\nanalytics.org', capabilityManifest: 'files/capabilities.yaml' }
describe('数据库来源编辑', () => {
  it('明确保存事实读取开关，不用能力清单推翻关闭选择', () => {
    const source = { id: form.id, kind: 'mysql_database', allow_fact_data: true, capability_manifest: form.capabilityManifest }
    expect(updateDatabaseSources([source], source.id, { ...form, allowFactData: false }, [form.capabilityManifest])[0]).toMatchObject({ allow_fact_data: false, capability_manifest: form.capabilityManifest })
    expect(updateDatabaseSources([], null, { ...form, capabilityManifest: '', allowFactData: true }, [])[0]).toMatchObject({ allow_fact_data: true, capability_manifest: null })
    expect(updateDatabaseSources([], null, { ...form, capabilityManifest: '' }, [])[0]?.allow_fact_data).toBe(false)
  })
  it('旧来源仅在清单和原部署确认均存在时显示为事实可读', () => {
    const legacy = { id: 'db', kind: 'gauss_database', capability_manifest: 'files/old.yaml' }
    expect(databaseFactDataAllowed(null, true)).toBe(false)
    expect(databaseFactDataAllowed(legacy, false)).toBe(false)
    expect(databaseFactDataAllowed(legacy, true)).toBe(true)
    expect(databaseFactDataAllowed({ ...legacy, allow_fact_data: false }, true)).toBe(false)
    expect(databaseFactDataAllowed({ ...legacy, capability_manifest: null, allow_fact_data: true }, false)).toBe(true)
  })
  it('MySQL 与 Gauss、文件来源并存，去重授权表', () => {
    const original = [{ id: 'gauss', kind: 'gauss_database', dsn_env: 'GAUSS_DSN' }, { id: 'excel', kind: 'excel_workbook', path: 'files/data.xlsx' }]
    const sources = updateDatabaseSources(original, null, form, ['files/capabilities.yaml'])
    expect(sources.slice(0, 2)).toEqual(original)
    expect(sources[2]).toMatchObject({ kind: 'mysql_database', dsn_env: 'MYSQL_DSN', allowed_tables: ['analytics.metrics', 'analytics.org'] })
  })
  it('编辑保留已有列权限和表单未管理的字段', () => {
    const source = { id: form.id, kind: 'mysql_database', allowed_columns: { 'analytics.metrics': ['id', 'name'] }, schema_name: 'analytics', extension: { retained: true } }
    const [result] = updateDatabaseSources([source], form.id, form, ['files/capabilities.yaml'])
    expect(result).toMatchObject({ allowed_columns: source.allowed_columns, schema_name: 'analytics', extension: { retained: true } })
  })
  it('拒绝同编号、改编号、将文件来源改数据库', () => {
    expect(() => updateDatabaseSources([{ id: form.id, kind: 'csv_file' }], null, form, ['files/capabilities.yaml'])).toThrow('已经存在')
    expect(() => updateDatabaseSources([{ id: 'old', kind: 'mysql_database' }], 'old', form, ['files/capabilities.yaml'])).toThrow('不能在此修改')
    expect(() => updateDatabaseSources([{ id: form.id, kind: 'csv_file' }], form.id, form, ['files/capabilities.yaml'])).toThrow('文件来源不能')
  })
  it('拒绝明文连接串、通配表和项目外验证文件', () => {
    expect(() => updateDatabaseSources([], null, { ...form, dsnEnv: 'mysql://user:private@host/db' }, ['files/capabilities.yaml'])).toThrow('环境变量名')
    expect(() => updateDatabaseSources([], null, { ...form, tables: 'analytics.*' }, ['files/capabilities.yaml'])).toThrow('通配符')
    expect(() => updateDatabaseSources([], null, form, [])).toThrow('已上传')
  })
  it('从会话连接切换到环境变量时清除旧凭据绑定', () => {
    const source = { id: form.id, kind: 'mysql_database', credential_mode: 'session', credential_revision: 'old-revision' }
    expect(updateDatabaseSources([source], source.id, form, ['files/capabilities.yaml'])[0]).toMatchObject({ credential_mode: 'environment', credential_revision: null, dsn_env: 'MYSQL_DSN' })
  })
  it('允许暂未设置能力文件，以便先配置并读取元数据', () => {
    expect(updateDatabaseSources([], null, { ...form, kind: 'gauss_database', capabilityManifest: '' }, [])[0]).toMatchObject({ kind: 'gauss_database', capability_manifest: null })
  })
  it('保留高级配置中已有的合法来源编号，不强制重新命名', () => {
    const source = { id: '业务库.mysql', kind: 'mysql_database' }
    expect(updateDatabaseSources([source], source.id, { ...form, id: source.id, capabilityManifest: '' }, [])[0]?.id).toBe(source.id)
  })
})
