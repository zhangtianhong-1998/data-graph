import { describe, expect, it } from 'vitest'
import { wrapGraphLabel } from './graphLabelLayout'

describe('business name layout', () => {
  it('keeps a long Chinese business name across two lines instead of truncating at 15 characters', () => {
    const name = '经营活动产生的短期合同资产原值合计'
    const lines = wrapGraphLabel(name)
    expect(lines).toHaveLength(2)
    expect(lines.join('')).toBe(name)
  })
  it('fits measured width and preserves Unicode code points', () => {
    const measure = (value: string) => Array.from(value).length * 10
    const lines = wrapGraphLabel('名称🍐abcdefghijklm', 50, 2, measure)
    expect(lines).toHaveLength(2)
    expect(lines.every(line => measure(line) <= 50)).toBe(true)
    expect(lines[1]?.endsWith('…')).toBe(true)
    expect(lines.join('')).toContain('🍐')
  })
  it('does not infer a different name from a generated caption', () => {
    const caption = 'Indicator（cn_name：利润）'
    expect(wrapGraphLabel(caption, 500).join('')).toBe(caption)
  })
})
