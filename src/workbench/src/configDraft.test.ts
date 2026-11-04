import { describe, expect, it } from 'vitest'
import { configTextChanged, mergeConfigDraft } from './configDraft'

describe('configuration edits', () => {
  it('ignores formatting and key order, but preserves array order', () => {
    expect(configTextChanged('{"b":2,"a":1}', '{\n"a":1,"b":2\n}')).toBe(false)
    expect(configTextChanged('{broken', '{}')).toBe(true)
    expect(configTextChanged('{"sources":[2,1]}', '{"sources":[1,2]}')).toBe(true)
  })
  it('combines additions, removals and independent nested edits', () => {
    const base = { model: { model: 'old', max_output_tokens: 100 }, goal_markdown: 'remove' }
    const form = { ...base, model: { ...base.model, model: 'new' } }
    expect(mergeConfigDraft(base, form, { model: { ...base.model, max_output_tokens: 200 } })).toEqual({ model: { model: 'new', max_output_tokens: 200 } })
  })
  it('refuses conflicting source lists without guessing or losing a source', () => {
    const base = { sources: [{ id: 'one', kind: 'excel_workbook' }] }
    expect(() => mergeConfigDraft(base, { sources: [] }, { sources: [{ id: 'two', kind: 'mysql_database' }] })).toThrow('sources')
  })
})
