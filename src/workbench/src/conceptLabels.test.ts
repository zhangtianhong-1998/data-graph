import { describe, expect, it } from 'vitest'
import { nodeCaption, nodeProperties, vocabularyLabel } from './graphLabels'

describe('concept context display', () => {
  it('uses the authorized short caption and leaves codes and original labels in details', () => {
    const node = {
      id: 'context-id', kind: 'ConceptBinding', label: 'subject="A" | metric=占用 ["M01"] 〔hash〕',
      display_label: '联合适用：占用 · 梨 · 批发',
      properties: { business_code: 'ctx_unique_code' }, scopes: ['A'],
    }
    expect(nodeCaption(node)).toBe('联合适用：占用 · 梨 · 批发')
    expect(nodeProperties(node)).toMatchObject({ label: node.label, business_code: 'ctx_unique_code' })
    expect(nodeProperties(node)).not.toHaveProperty('scopes')
    expect(nodeCaption({ id: 'ordinary', label: '收入 | 费用-差额' })).toBe('收入 | 费用-差额')
  })

  it('uses a business key for an unnamed node and keeps conflicting source names visible', () => {
    const node = { id: 'n_1', kind: 'Metric', display_label: 'Metric__def_abc（parent_of：某指标；code：M1）',
      identity: { code: 'M1' }, label_origin: 'relations' }
    expect(nodeCaption(node)).toBe('M1')
    expect(nodeCaption({ ...node, identity: {} })).toBe('未提供名称')
    expect(nodeCaption({ id: 'n_1', label: '拨备', label_conflict: true, label_variants: ['拨备', '拨备13'] }))
      .toBe('拨备 / 拨备13')
  })

  it('gives the built-in concept types and component relations Chinese captions', () => {
    expect(vocabularyLabel('entity_types', 'ConceptBinding')).toBe('联合适用')
    expect(vocabularyLabel('entity_types', 'Product')).toBe('产品')
    expect(vocabularyLabel('entity_types', 'Channel')).toBe('渠道')
    expect(vocabularyLabel('relations', 'binds_metric')).toBe('关联指标')
    expect(vocabularyLabel('relations', 'binds_product')).toBe('关联产品')
    expect(vocabularyLabel('relations', 'binds_channel')).toBe('关联渠道')
  })
})
