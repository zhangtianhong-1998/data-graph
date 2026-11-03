import { describe, expect, it } from 'vitest'
import { goalMarkdown } from './goalMarkdown'
describe('Markdown construction objective', () => {
  it('preserves explicit Markdown exactly, including an intentionally empty draft', () => {
    const text = '# 本次目标\n\n- 设备\n- 关系  \n'
    expect(goalMarkdown({ goal_markdown: text, goal: { purpose: '旧目标' } })).toBe(text)
    expect(goalMarkdown({ goal_markdown: '', goal: { purpose: '旧目标' } })).toBe('')
  })
  it('renders legacy intent without turning table access into document text', () => {
    const value = { goal: { purpose: '发现人员', include: ['对象与关系'], runtime_guidance: [{ text: '保留来源', injection: 'global' as const, stages: [] }], entity_kinds: ['Person'], excluded_columns: ['secret'] } }
    expect(goalMarkdown(value)).toBe('发现人员\n\n## 重点关注\n\n对象与关系\n\n## 运行指导\n\n保留来源\n\n## 指定对象类型\n\nPerson')
    expect(value.goal.excluded_columns).toEqual(['secret'])
  })
})
