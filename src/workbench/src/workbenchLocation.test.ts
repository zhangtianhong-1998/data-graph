import { describe, expect, it } from 'vitest'
import { WorkbenchLocation, workbenchLocationKey } from './workbenchLocation'

function memoryStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  }
}
const projects = [{ project_id: 'first' }, { project_id: 'selected' }]
const sources = [{ id: 'db', kind: 'gauss_database' }, { id: 'file', kind: 'csv_file' }]

describe('工作台刷新位置', () => {
  it('刷新后保留所选项目与新增连接窗口，浏览器只保存导航编号', () => {
    const storage = memoryStorage()
    new WorkbenchLocation(() => storage).openDatabase('selected', null)
    const restored = new WorkbenchLocation(() => storage)
    expect(restored.preferredProject(projects)).toBe('selected')
    expect(restored.restoreDatabase('selected', sources)).toEqual({ source: null })
    expect(JSON.parse(storage.getItem(workbenchLocationKey)!)).toEqual({ projectId: 'selected', databaseSourceId: null })
  })

  it('恢复已存在的来源时使用当前配置，不保存来源配置或连接密码', () => {
    const storage = memoryStorage(), location = new WorkbenchLocation(() => storage)
    location.openDatabase('selected', 'db')
    expect(location.restoreDatabase('selected', sources)?.source).toBe(sources[0])
    expect(JSON.parse(storage.getItem(workbenchLocationKey)!)).toEqual({ projectId: 'selected', databaseSourceId: 'db' })
  })

  it('手动关闭或保存成功只清除窗口标记，仍记住项目', () => {
    const storage = memoryStorage(), location = new WorkbenchLocation(() => storage)
    location.openDatabase('selected', 'db')
    location.closeDatabase('selected')
    expect(location.restoreDatabase('selected', sources)).toBeNull()
    expect(location.preferredProject(projects)).toBe('selected')
    expect(JSON.parse(storage.getItem(workbenchLocationKey)!)).toEqual({ projectId: 'selected' })
  })

  it('切换项目不恢复旧项目窗口，旧的关闭回调不影响新项目窗口', () => {
    const storage = memoryStorage(), location = new WorkbenchLocation(() => storage)
    location.openDatabase('selected', 'db')
    expect(location.restoreDatabase('first', sources)).toBeNull()
    location.selectProject('first')
    expect(location.restoreDatabase('first', sources)).toBeNull()
    location.openDatabase('first', null)
    location.closeDatabase('selected')
    expect(location.restoreDatabase('first', sources)).toEqual({ source: null })
  })

  it.each(['deleted-db', 'file'])('来源 %s 已删除或不再是数据库时，清除窗口标记', sourceId => {
    const storage = memoryStorage(), location = new WorkbenchLocation(() => storage)
    location.openDatabase('selected', sourceId)
    expect(location.restoreDatabase('selected', sources)).toBeNull()
    expect(JSON.parse(storage.getItem(workbenchLocationKey)!)).toEqual({ projectId: 'selected' })
  })

  it('项目被删除时回到第一个有效项目，不恢复其他项目的连接', () => {
    const storage = memoryStorage(), location = new WorkbenchLocation(() => storage)
    location.openDatabase('deleted-project', 'db')
    expect(location.preferredProject(projects)).toBe('first')
    expect(storage.getItem(workbenchLocationKey)).toBeNull()
    expect(location.preferredProject([])).toBeUndefined()
  })

  it.each(['{', 'null', '[]', '{}', '{"projectId":1}', '{"projectId":"selected","databaseSourceId":false}', '{"projectId":"selected","password":"forbidden"}'])('无效浏览器记录 %s 不影响启动并被清除', value => {
    const storage = memoryStorage(), location = new WorkbenchLocation(() => storage)
    storage.setItem(workbenchLocationKey, value)
    expect(location.preferredProject(projects)).toBe('first')
    expect(location.restoreDatabase('selected', sources)).toBeNull()
    expect(storage.getItem(workbenchLocationKey)).toBeNull()
  })

  it('浏览器禁用存储时仍可正常打开项目', () => {
    const location = new WorkbenchLocation(() => { throw new Error('SecurityError') })
    expect(() => {
      location.selectProject('selected'); location.openDatabase('selected', null); location.closeDatabase('selected')
    }).not.toThrow()
    expect(location.preferredProject(projects)).toBe('first')
    expect(location.restoreDatabase('selected', sources)).toBeNull()
  })
})
