// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp } from 'vue'
import type { App } from 'vue'
import RunIssues from './RunIssues.vue'
let app: App, root: HTMLDivElement
function mount(report: unknown) {
  root = document.createElement('div'); document.body.append(root)
  app = createApp(RunIssues, { report }); app.mount(root)
}
afterEach(() => { app?.unmount(); root?.remove() })

describe('累计读取额度和工作内存提示', () => {
  it('按运行快照显示已用、上限、剩余，零余额与超限用量均保留', () => {
    mount({ validation: { query_budget: {
      queries: 322, max_queries: 1000, remaining_queries: 678,
      returned_rows: 400001, max_returned_rows: 400000, remaining_returned_rows: 0,
      returned_bytes: 65 * 1048576, max_returned_bytes: 64 * 1048576, remaining_returned_bytes: 0,
    } } })
    const table = root.querySelector('[aria-label="本轮数据读取额度"]')!
    const rows = [...table.querySelectorAll('tbody tr')].map(row => [...row.children].map(cell => cell.textContent))
    expect(rows).toEqual([
      ['查询次数', '322', '1,000', '678'],
      ['累计返回行数', '400,001', '400,000', '0'],
      ['累计返回数据量', '65 MiB', '64 MiB', '0 B'],
    ])
    expect(table.textContent).toContain('所有来源和阶段累计')
  })
  it('明确展示不限和旧报告缺失值，不将未知限额当作不限或默认64MiB', () => {
    mount({ validation: { query_budget: {
      queries: 2, max_queries: null, remaining_queries: null,
      returned_rows: 42,
      returned_bytes: 4096, max_returned_bytes: null, remaining_returned_bytes: null,
    } } })
    const rows = [...root.querySelectorAll('tbody tr')].map(row => [...row.children].map(cell => cell.textContent))
    expect(rows).toEqual([
      ['查询次数', '2', '不限', '不限'],
      ['累计返回行数', '42', '未记录', '未记录'],
      ['累计返回数据量', '4,096 B', '不限', '不限'],
    ])
  })
  it.each([
    ['returned_rows_budget_exhausted', '分批验证不会重置本轮计数'],
    ['returned_bytes_budget_exhausted', '累计读取额度，不是内存、磁盘容量或 MCP 通信超时'],
    ['memory_budget_exhausted', '增加累计返回量额度不能解除这个限制'],
  ])('为 %s 给出对应配置建议，隐藏任意异常内容', (code, hint) => {
    mount({ issues: [{ code, message: 'RAW_DATABASE_RESULT_MUST_NOT_RENDER' }] })
    expect(root.textContent).toContain(hint)
    expect(root.textContent).not.toContain('RAW_DATABASE_RESULT')
  })
  it('忽略无效统计和未知字段，不展示原始值', () => {
    mount({ validation: { query_budget: {
      queries: Number.POSITIVE_INFINITY, max_queries: -1,
      returned_rows: 'PRIVATE_SOURCE_VALUE', max_returned_rows: Number.NaN,
      returned_bytes: true, max_returned_bytes: Number.MAX_SAFE_INTEGER + 1,
      raw_rows: 'PRIVATE_SOURCE_VALUE',
    } } })
    expect(root.querySelector('[aria-label="本轮数据读取额度"]')).toBeNull()
    expect(root.textContent).not.toContain('PRIVATE_SOURCE_VALUE')
    expect(root.textContent).not.toContain('Infinity')
  })
})
