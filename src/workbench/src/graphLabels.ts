import type { GraphNode } from './types'

export type DisplayDefinition = { name: string; description?: string; display_label?: string }
export type DisplaySchema = Partial<Record<'entity_types' | 'properties' | 'relations', DisplayDefinition[]>>
const relations: Record<string, string> = {
  parent_of: '父子关系', member_of: '隶属于', uses_measure: '使用度量', same_entity: '同一对象',
  analyzes: '分析对象', maps_to_concept: '映射到概念', has_schema: '包含模式', has_table: '包含表',
  table_has_column: '包含字段', member_of_group: '属于字段组', expresses_role: '表达角色',
  declared_fk: '声明外键', technical_link: '技术关联', value_overlap: '取值重叠',
  same_attribute_role: '相同属性角色', verified_reference: '已验证引用', defined_by: '定义依据',
  documented_by: '文档说明', supported_by_rule: '规则支持', has_sheet: '包含工作表',
  sheet_has_column: '包含字段', has_section: '包含章节', has_constraint: '具有约束',
  constraint_column: '约束字段', constraint_target: '约束目标',
  binds_metric: '关联指标', binds_product: '关联产品', binds_channel: '关联渠道',
}
const kinds: Record<string, string> = {
  Metric: '指标', Measure: '度量', Dimension: '维度', DimensionMember: '维度成员', ReportItem: '报表项',
  AnalysisMethod: '分析方法', AnalysisQuestion: '分析问题', BusinessConcept: '业务概念',
  ConceptBinding: '联合适用', Product: '产品', Channel: '渠道',
  Database: '数据库', Schema: '模式', Table: '表', Column: '字段', ColumnGroup: '字段组',
  SemanticSlot: '语义角色', Workbook: '工作簿', Worksheet: '工作表', Constraint: '约束',
  Document: '文档', DocumentSection: '文档章节', Rule: '规则', GraphFact: '图谱事实',
  business: '业务对象', metadata: '元数据', provenance: '来源依据',
}
const properties: Record<string, string> = {
  label: '名称', aliases: '别名', alias: '别名', english_name: '英文名', business_code: '业务编码',
  description: '说明', definition: '定义', unit: '单位', formula: '公式', period: '期间',
  caliber: '口径', granularity: '粒度', level: '层级', level_no: '层级序号', dimension: '维度',
  taxonomy: '分类体系', source_ref: '来源引用', schema: '模式', table: '表', column: '字段',
  data_type: '数据类型', nullable: '允许为空', primary_key: '主键', comment: '备注', role: '角色',
  technical_status: '技术状态', coverage: '覆盖率', target_kind: '目标类型', modality: '模态',
  frequency: '频率', domain: '编码域', normalization: '规范化', semantic_namespace: '语义命名空间',
  aggregation: '聚合方式', document_ref: '文档引用', section_ref: '章节引用', column_position: '字段位置',
  qualified_name: '完整名称', name: '名称', kind: '类型', scope: '范围', scopes: '可见范围',
  rule_ref: '规则引用', revision: '修订', source_position: '来源层位', valid_period: '有效期间',
  source_level: '来源层位', target_level: '目标层位', relation_group: '关系组', namespace: '命名空间',
  condition: '条件', source_group: '来源组', semantic_level: '语义层级', hierarchy_version: '层级版本',
  allow_dag: '允许多个父节点', source_id: '数据来源', source_binding: '来源绑定',
  single_valued: '单值属性', multi_valued: '多值属性', name_columns: '名称字段', alias_columns: '别名字段',
  json_array_columns: '数组字段', source: '来源', identity_columns: '身份字段', attribute_columns: '属性字段',
  properties: '属性', conflicts: '属性冲突', property: '属性', values: '取值',
  hierarchy_columns: '层位字段', reference_columns: '关联字段', concept_columns: '概念字段',
  namespace_columns: '命名空间字段', source_columns: '使用字段', target_columns: '目标端字段',
  code: '编码字段', key_column: '身份编码字段',
  constant_properties: '来源位置与固定属性', context_columns: '路径上下文字段', identity: '业务身份', source_key_columns: '来源端身份字段', target_key_columns: '目标端身份字段',
  scope_columns: '可见范围字段', label_column: '名称字段',
  physical_column: '物理字段', catalog_comment: '字段注释', evidence_refs: '证据引用',
}
export const supportOriginName = (origin: string) => ({
  source_query: '事实数据查询', user_input: '用户确认', document: '文档依据',
  metadata: '授权字段元数据', agent_declared: 'Agent 依据材料声明',
}[origin] || '规则计算')

export type MetadataEndpoint = {
  endpoint?: string
  basis: string
  column: { source_id: string; schema?: string; table: string; column: string; evidence_refs?: string[] }
  meaning_origin?: string
  row_membership?: string
  non_null_observed?: boolean
}
export function metadataRelationValues(endpoint: MetadataEndpoint): Record<string, unknown> {
  const { column } = endpoint
  const basis = endpoint.basis === 'schema_applies_to_rows' ? '声明适用于实际行对象' :
    endpoint.basis === 'observed_non_null' ? '实际行中该字段非空' : '依据未识别'
  return { '列定义所在端': endpoint.endpoint === 'source' ? '关系起点' : '关系终点',
    '关系依据': basis, '数据来源': column.source_id, '模式': column.schema || 'public',
    '表': column.table, '字段': column.column,
    ...(endpoint.meaning_origin === 'agent_declared' ? { '关系含义': 'Agent 依据材料声明' } : {}),
    ...(endpoint.row_membership === 'observed_projection' ? { '行对象依据': '来自实际投影行' } : {}),
    '非空观测': endpoint.non_null_observed === true ? '已观察到非空值；未保留经营数值' : '未声明已观察到非空值',
    '证据引用': column.evidence_refs || [] }
}
const chinese = (value: string) => /[\u3400-\u9fff]/.test(value)
export function vocabularyLabel(category: keyof DisplaySchema, raw: unknown, schema: DisplaySchema = {}): string {
  const name = String(raw || '')
  if (!name) return category === 'relations' ? '关联' : '未提供'
  const entry = schema[category]?.find(value => value.name === name)
  // Descriptions are definitions, not invented translations. Long definitions
  // remain available as tooltips and in the detail panel.
  const builtin = (category === 'relations' ? relations : category === 'entity_types' ? kinds : properties)[name]
  const declared = entry?.display_label || builtin || (chinese(name) ? name : entry?.description)
  if (declared && chinese(declared)) {
    // Prefer the definition's own opening name/clause, without inventing a translation.
    const opening = declared.split(/[：:，,；;。\n]/, 1)[0].trim()
    const title = opening && opening.length <= 24 ? opening : declared
    return title.length > 24 ? title.slice(0, 23) + '…' : title
  }
  return builtin || (chinese(name) ? name : `${name}（未提供中文名）`)
}
export function vocabularyDescription(category: keyof DisplaySchema, raw: unknown, schema: DisplaySchema = {}): string {
  return schema[category]?.find(value => value.name === raw)?.description || ''
}
export function mergeDisplaySchema(...schemas: Array<DisplaySchema | undefined>): DisplaySchema {
  const result: DisplaySchema = {}
  for (const category of ['entity_types', 'relations', 'properties'] as const) {
    const values = new Map<string, DisplayDefinition>()
    for (const schema of schemas) for (const entry of schema?.[category] || []) values.set(entry.name, entry)
    result[category] = [...values.values()]
  }
  return result
}
export const nodeCaption = (node: GraphNode) => String([node.display_label, node.label, node.name].find(value => value && value !== node.id) || '未命名节点')
export function nodeIdentity(node: GraphNode): Record<string, unknown> {
  return node.identity && typeof node.identity === 'object' && !Array.isArray(node.identity) ? node.identity as Record<string, unknown> : {}
}
export function nodeSearchText(node: GraphNode): string {
  return [nodeCaption(node), node.kind || '', ...Object.entries(nodeIdentity(node)).flat(),
    ...(Array.isArray(node.aliases) ? node.aliases : [])].map(String).join(' ').toLocaleLowerCase()
}
export function nodeProperties(node: GraphNode): Record<string, unknown> {
  const fields = Object.fromEntries(Object.entries(node).filter(([key, value]) =>
    !['id', 'type', 'kind', 'layer', 'status', 'display_label', 'display_label_basis', 'label_origin', 'identity_origin', 'identity', 'scopes',
      'provenance', 'supports', 'entity_members', 'members', 'support_ids', 'fact_ids', 'properties'].includes(key) &&
    !(key === 'label' && value === node.id) && !(key === 'conflicts' && Array.isArray(value) && !value.length)))
  return { ...fields, ...(node.properties && typeof node.properties === 'object' ? node.properties : {}) }
}
