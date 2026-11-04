// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp } from 'vue'
import type { App } from 'vue'
import ConstructionReport from './ConstructionReport.vue'
import RunIssues from './RunIssues.vue'
let app: App, root: HTMLDivElement
function mount(component: typeof ConstructionReport | typeof RunIssues, report: unknown) {
  root = document.createElement('div'); document.body.append(root)
  app = createApp(component, { report }); app.mount(root)
}
afterEach(() => { app?.unmount(); root?.remove() })
describe('构建报告与程序统计', () => {
  it('用量包含缓存输入并单列已计入合计的摘要调用', () => {
    mount(ConstructionReport, { construction: { usage: { input_tokens: 1500, output_tokens: 120,
      total_tokens: 1620, cache_read_tokens: 700, cache_write_tokens: 50, max_input_tokens: 900,
      measured_calls: 3, dispatched_calls: 3, missing_usage_calls: 0, complete: true,
      by_purpose: { compaction: { total_tokens: 240 } } } } })
    const usage = root.querySelector('[aria-label="模型用量"]')!
    expect(usage.textContent).toContain('已记录输入 tokens（含缓存）1,500')
    expect(usage.textContent).toContain('已记录输出 tokens120')
    expect(usage.textContent).toContain('已记录合计 tokens1,620')
    expect(usage.textContent).toContain('其中摘要调用 tokens240')
    expect(usage.textContent).toContain('缓存读取 700 · 缓存写入 50')
    expect(usage.textContent).toContain('摘要调用已计入合计')
    expect(usage.textContent).toContain('用量统计完整：已记录 3 / 已派发 3 次调用')
  })
  it.each([0, 1])('缺失usage不等于零消费，已测调用数为%s时明确范围', measured => {
    mount(ConstructionReport, { construction: { usage: { input_tokens: measured ? 100 : 0,
      output_tokens: measured ? 10 : 0, total_tokens: measured ? 110 : 0,
      cache_read_tokens: 0, cache_write_tokens: 0, max_input_tokens: measured ? 100 : 0,
      measured_calls: measured, dispatched_calls: 2, missing_usage_calls: 2 - measured, complete: false,
      by_purpose: {} } } })
    const usage = root.querySelector('[aria-label="模型用量"]')!
    expect(usage.textContent).toContain(`已记录输入 tokens（含缓存）${measured ? '100' : '未记录'}`)
    expect(usage.textContent).toContain('其中摘要调用 tokens未记录')
    expect(usage.textContent).toContain('用量统计不完整')
    expect(usage.textContent).toContain(`${2 - measured} 次调用缺少用量`)
    expect(usage.textContent).toContain('缺失用量不计为零')
  })
  it('草稿包、映射、真实调用和独立验收分别展示', () => {
    mount(ConstructionReport, { validation: { E: 'not_run', D: 'not_run', I: 'not_run' }, construction: {
      summary: { draft_count: 1, published_drafts: 1, entity_type_count: 5, relation_type_count: 4, mapping_count: 19 },
      execution: { model_runtime: 'executed', data_validation: 'passed' },
      acceptance: { independent_gold: 'not_run', customer_database: 'not_run' },
      statistics: { complete: true, nodes: 462, edges: 930,
        entity_types: { 报表项: { nodes: 210, unique_labels: 24, unique_source_labels: 24, aliases: 225 } },
        relation_types: { 父子: { edges: 210, multiple_parent_nodes: 10, acyclic: true, cycle_nodes: 0, acyclic_declared: false } } },
      drafts: [{ draft_id: 'discovered', revision: 1, status: 'published', entity_types: 5, relation_types: 4, mappings: 19,
        unresolved: [], decisions: ['重复行不增加事实'], suggestions: ['是否清洗上游重复行'], validation: { passed: true } }],
    } })
    expect(root.textContent).toContain('1 个图谱草稿包 · 5 类实体 · 4 类关系 · 19 个字段映射')
    expect(root.textContent).toContain('已执行真实模型调用')
    expect(root.textContent).toContain('全量数据检查已通过')
    expect(root.textContent).toContain('独立真值评测尚未验收')
    expect(root.textContent).toContain('客户数据库验收尚未验收')
    expect(root.textContent).toContain('462 个节点 · 930 条关系')
    expect(root.querySelector('[aria-label="目标缺口"]')).toBeNull()
    expect(root.querySelector('[aria-label="可选建议"]')?.textContent).toContain('不影响本次目标完成状态')
    expect(root.querySelector('tbody tr')?.textContent).toBe('报表项2102424225')
    expect(root.textContent).toContain('无环未声明')
  })
  it('未执行数据检查和模拟调用不会变成验收通过', () => {
    mount(ConstructionReport, { construction: { execution: { model_runtime: 'test_double', data_validation: 'not_run' },
      drafts: [{ draft_id: 'draft', revision: 2, status: 'candidate', entity_types: 1, relation_types: 0, mappings: 1,
        unresolved: ['缺少身份依据'], validation: { passed: null } }] } })
    expect(root.textContent).toContain('仅模拟调用')
    expect(root.textContent).toContain('全量数据检查未执行')
    expect(root.textContent).toContain('独立真值评测尚未验收')
    expect(root.querySelector('[aria-label="目标缺口"]')?.textContent).toContain('缺少身份依据')
    expect(root.textContent).not.toContain('已验收')
  })
  it('报告分类原文可读且同草稿多个缺口不合并，未知错误码不隐藏', () => {
    mount(RunIssues, { issues: [
      { code: 'construction_goal_gap', category: 'goal_gap', draft_id: 'draft', status: 'unresolved', message: '渠道关系缺少依据' },
      { code: 'construction_goal_gap', category: 'goal_gap', draft_id: 'draft', status: 'unresolved', message: '跨表编码域待确认' },
      { code: 'construction_suggestion', category: 'suggestion', status: 'diagnostic', message: '可选建立主体实体' },
      { code: 'future_safe_error', message: 'PRIVATE DRIVER BODY' },
    ] })
    expect(root.querySelectorAll('.note')).toHaveLength(4)
    expect(root.textContent).toContain('渠道关系缺少依据')
    expect(root.textContent).toContain('跨表编码域待确认')
    expect(root.textContent).toContain('可选建立主体实体')
    expect(root.textContent).toContain('future_safe_error')
    expect(root.textContent).not.toContain('PRIVATE DRIVER BODY')
  })
})
