<script setup lang="ts">
import { computed } from 'vue'
import PropertyTable from './PropertyTable.vue'
import TechnicalDetails from './TechnicalDetails.vue'
import { metadataRelationValues, nodeCaption, nodeIdentity, nodeProperties, supportOriginName, vocabularyDescription, vocabularyLabel } from './graphLabels'
import type { DisplaySchema, MetadataEndpoint } from './graphLabels'
import type { GraphEdge, GraphNode, StandardNode } from './types'

type SupportItem = {
  id: string; fact_id: string; status: string; origin: string; source_binding: string
  rule_id: string; rule_revision?: number; publication_id?: string; document_ref?: string; scope_ref: string
  binding?: {
    source_kind?: string; evidence_refs?: string[]; metadata_endpoint?: MetadataEndpoint
    constant_properties?: Record<string, unknown>; row_context?: { values?: Record<string, unknown> }
    lookup_matches?: Array<Record<string, unknown>>
    inherited_from?: { inherit_id?: string; source_project_id?: string }
  }
}

type FactItem = { id: string; fact_kind: string; status: string; payload: Record<string, unknown> }

type ProvenancePayload = {
  facts: FactItem[]
  supports: SupportItem[]
  rules: Array<Record<string, unknown>>
  nodes?: GraphNode[]
  standard_nodes?: StandardNode[]
  display_schema?: DisplaySchema
  next_offset: number | null
  supports_truncated: boolean
  total_incident_edges?: number | null
}

const props = defineProps<{
  selected: GraphNode
  details: ProvenancePayload | null
  detailsLoading: boolean
  detailsError: string
  adjacent: GraphEdge[]
  drawnIncidentEdges: number
  knownIncidentEdges?: number | null
  pageLoading: boolean
  serverPaging: boolean
  fullTransport: boolean
  hasNeighborhood: boolean
  expansionOffset: number | null | undefined
  hasExpansionOffset: boolean
  vocabulary: DisplaySchema
  projectId?: string
  detailNodes: Record<string, GraphNode>
  allNodes: GraphNode[]
}>()

const emit = defineEmits<{
  (e: 'selectEdge', edge: GraphEdge): void
  (e: 'showTwoHops', node: GraphNode): void
  (e: 'expandNode'): void
  (e: 'loadMoreDetails'): void
  (e: 'retryDetails'): void
}>()

const standardNode = computed<StandardNode | null>(() =>
  props.details?.standard_nodes?.find(item => item.id === props.selected.id) ?? null,
)

const statusName = (value?: unknown) => ({ accepted: '已接受', candidate: '候选', unresolved: '未决', conflict: '冲突' }[String(value)] || '状态未提供')
const kindName = (value: unknown) => vocabularyLabel('entity_types', value, props.vocabulary)
const relationName = (value: unknown) => vocabularyLabel('relations', value, props.vocabulary)
const propertyName = (value: string) => vocabularyLabel('properties', value, props.vocabulary)
const relationDescription = (value: unknown) => vocabularyDescription('relations', value, props.vocabulary)

const labelOrigin = (node: GraphNode) => ({
  declared: '名称由已声明的展示字段生成',
  relations: '名称由关联对象和业务身份生成',
  identity: '名称由业务身份生成',
  metadata: '名称来自字段元数据；此节点代表列定义',
  metadata_declared: '名称由 Agent 依据元数据或文档声明；此节点代表列定义',
}[String(node.label_origin)] || '')

const accessLabel = (node: GraphNode) => Array.isArray(node.scopes) ? node.scopes.join('、') : ''

const endpointName = (id: unknown) => {
  const key = String(id || '')
  const node = props.detailNodes[key] || props.allNodes.find(item => item.id === key)
  return node ? nodeCaption(node) : '节点名称尚未加载'
}

const factTitle = (fact: FactItem) => fact.fact_kind === 'PropertyValue' ? propertyName(String(fact.payload.name || '')) : fact.fact_kind === 'Edge' ? relationName(fact.payload.relation) : '对象存在'
const visibleQualifiers = (value: unknown) => Object.fromEntries(Object.entries((value || {}) as Record<string, unknown>).filter(([key]) => !['semantic_basis', 'document_ref', 'source_group'].includes(key)))
const factValues = (fact: FactItem) => fact.fact_kind === 'PropertyValue' ? { [String(fact.payload.name)]: fact.payload.value } : fact.fact_kind === 'Edge' ? visibleQualifiers(fact.payload.qualifiers) : { kind: kindName(fact.payload.kind) }
const sourceSummary = (rule: Record<string, unknown>) => Object.fromEntries(Object.entries((rule.source || {}) as Record<string, unknown>).filter(([, value]) => value != null && value !== ''))
const lookupValues = (match: Record<string, unknown>) => ({
  '关联端点': match.endpoint === 'source' ? '关系起点' : '关系终点',
  '匹配来源': match.source,
  '本行字段': match.input_columns,
  '本行取值': match.input_values,
  '目标匹配字段': match.match_columns,
  '目标身份字段': match.key_columns,
  '匹配后的身份': match.resolved_key,
  '可见范围': match.scope_ref,
})
const mappingValues = (rule: Record<string, unknown>) => Object.fromEntries([
  'identity_columns', 'namespace_columns', 'attribute_columns', 'hierarchy_columns',
  'reference_columns', 'concept_columns', 'source_columns', 'scope_columns',
  'label_column', 'english_name_column', 'description_column',
  'constant_properties', 'context_columns', 'endpoint_lookups',
].filter(key => rule[key] && (typeof rule[key] === 'string' || Object.keys(rule[key] as object).length)).map(key => [key, rule[key]]))

const descriptionOriginLabel = (origin: StandardNode['business_description_origin']) => ({
  source_column: '数据表描述字段',
  entity_definition: '实体类型定义',
  none: '未提供',
}[origin])

</script>

<template>
  <div class="node-inspector">
    <span class="badge blue">{{ kindName(selected.kind || selected.layer) }}</span>
    <span class="badge" :class="selected.status === 'accepted' ? 'green' : 'amber'">{{ statusName(selected.status) }}</span>
    <h2 class="detail-name">{{ nodeCaption(selected) }}</h2>
    <p v-if="labelOrigin(selected)" class="muted small-text">{{ labelOrigin(selected) }}</p>
    <p v-if="selected.overview_only" class="note">此节点先显示精简摘要，完整属性正在按需读取；读取失败时可重试来源详情。</p>

    <section v-if="standardNode" class="standard-node-summary" aria-label="标准化节点名片">
      <div class="standard-card-grid">
        <div class="standard-row">
          <span class="standard-label">中文名</span>
          <strong class="standard-val">{{ standardNode.chinese_name || '未单独映射（使用显示名称）' }}</strong>
        </div>
        <div class="standard-row">
          <span class="standard-label">英文名</span>
          <span class="standard-val" :class="{ muted: standardNode.english_name_status === 'unknown' }">
            {{ standardNode.english_name_status === 'mapped' && standardNode.english_name ? standardNode.english_name : '未知（未单独映射）' }}
          </span>
        </div>
        <div v-if="standardNode.aliases.length" class="standard-row">
          <span class="standard-label">别名</span>
          <span class="standard-val">{{ standardNode.aliases.join('、') }}</span>
        </div>
      </div>

      <div class="divider"></div>
      <div class="standard-section">
        <div class="section-title-row">
          <h3>节点描述</h3>
          <span class="badge">{{ descriptionOriginLabel(standardNode.business_description_origin) }}</span>
        </div>
        <p class="standard-desc">{{ standardNode.business_description || '暂无业务描述' }}</p>

        <div v-if="standardNode.catalog_metadata_notes.length" class="catalog-notes">
          <p class="muted small-text">物理目录注释（表/列元数据声明，非单指标业务口径）：</p>
          <div v-for="note in standardNode.catalog_metadata_notes" :key="`${note.source_id}:${note.qualified_table}`" class="catalog-note-item">
            <strong>{{ note.source_id }} · {{ note.qualified_table }}</strong>
            <span v-if="note.table_comment"> · 表注释：{{ note.table_comment }}</span>
            <ul v-if="note.column_comments.length" class="compact-list">
              <li v-for="col in note.column_comments" :key="col.column">
                {{ col.column }} ({{ col.data_type }})：{{ col.comment }}
              </li>
            </ul>
          </div>
        </div>
      </div>
    </section>

    <template v-if="Object.keys(nodeIdentity(selected)).length">
      <div class="divider"></div>
      <h3>业务身份</h3>
      <PropertyTable :values="nodeIdentity(selected)" />
    </template>
    <p v-if="accessLabel(selected)" class="muted small-text">可见范围：{{ accessLabel(selected) }}（访问授权，不代表业务主体）</p>

    <div class="divider"></div>
    <h3>节点属性</h3>
    <template v-if="standardNode && standardNode.attributes.length">
      <div class="dynamic-attr-list" aria-label="动态构建属性列表">
        <div v-for="attr in standardNode.attributes" :key="attr.key" class="dynamic-attr-item">
          <div class="attr-head">
            <strong>{{ propertyName(attr.label) }}</strong>
            <span v-if="attr.data_type && attr.data_type !== 'unknown'" class="badge">{{ attr.data_type }}</span>
          </div>
          <PropertyTable :values="{ 值: attr.values }" />
          <div v-if="attr.origin_tables.length" class="muted small-text attr-origin">
            来源：{{ attr.origin_tables.map(o => o.column ? `${o.qualified_table}.${o.column}` : o.qualified_table).join('、') }}
          </div>
        </div>
      </div>
    </template>
    <p v-else-if="standardNode" class="muted small-text">暂无属性</p>
    <PropertyTable v-else :values="nodeProperties(selected)" :label="propertyName" />

    <section v-if="standardNode && standardNode.source_evidences.length" aria-label="来源物理表清单">
      <div class="divider"></div>
      <h3>物理表来源与证据（{{ standardNode.source_evidences.length }} 张表）</h3>
      <div v-for="ev in standardNode.source_evidences" :key="`${ev.source_id}:${ev.qualified_table}`" class="source-evidence-card">
        <div class="ev-head">
          <span class="badge blue">{{ ev.source_kind_label }}</span>
          <strong>{{ ev.source_id }} · {{ ev.qualified_table }}</strong>
        </div>
        <div class="ev-meta small-text">
          <div v-if="ev.key_columns.length">身份键列：{{ ev.key_columns.join('、') }}</div>
          <div v-if="ev.contributed_fields.length">
            贡献字段：
            <span v-for="(field, idx) in ev.contributed_fields" :key="`${field.role}:${field.column}`">
              {{ idx > 0 ? '、' : '' }}{{ field.field }}({{ field.column }}<template v-if="field.data_type && field.data_type !== 'unknown'">:{{ field.data_type }}</template>)
            </span>
          </div>
          <div>可见范围：{{ ev.scopes.join('、') }}<template v-if="ev.evidence_refs.length"> · 证据：{{ ev.evidence_refs.join('、') }}</template></div>
          <div v-if="ev.inherited_from" class="muted">
            继承来源：{{ ev.inherited_from.source_project_id || ev.inherited_from.inherit_id }}
          </div>
        </div>
      </div>
    </section>

    <div class="divider"></div>
    <h3>已加载关联 · {{ adjacent.length }}</h3>
    <p class="field-hint" aria-label="节点关联范围">
      当前画布显示 {{ drawnIncidentEdges }} 条相连关系<template v-if="knownIncidentEdges != null">，当前授权图中共有 {{ knownIncidentEdges }} 条</template><template v-else>；完整关系数量尚未读取</template>。<template v-if="knownIncidentEdges == null || drawnIncidentEdges < knownIncidentEdges">当前是局部视图，不能据此判断节点孤立。</template>
    </p>
    <button class="btn small" :disabled="pageLoading" @click="emit('showTwoHops', selected)">查看两跳关联</button>
    <button v-if="serverPaging && !fullTransport && !hasNeighborhood" class="btn small" :disabled="pageLoading || expansionOffset === null" @click="emit('expandNode')">
      {{ hasExpansionOffset ? '继续展开关联' : '展开相邻节点' }}
    </button>
    <button v-for="(edge, i) in adjacent.slice(0, 40)" :key="i" type="button" class="edge-detail relation-selection" @click="emit('selectEdge', edge)">
      <span class="badge" :title="relationDescription(edge.relation)">{{ relationName(edge.relation) }} · {{ statusName(edge.status) }}</span>
      <p>{{ endpointName(edge.source) }} → {{ endpointName(edge.target) }}</p>
    </button>
    <p v-if="adjacent.length > 40" class="muted small-text">仅展示前 40 条关系；来源详情按事实分页读取。</p>

    <section v-if="projectId" aria-label="来源支持">
      <div class="divider"></div>
      <h3>来源支持</h3>
      <p v-if="detailsLoading" role="status">正在读取来源…</p>
      <p v-if="detailsError" role="alert">{{ detailsError }}
        <button type="button" class="btn small" @click="emit('retryDetails')">重试读取详情</button>
      </p>
      <template v-if="details">
        <article v-for="fact in details.facts" :key="fact.id" class="fact-detail">
          <strong>{{ factTitle(fact) }}</strong> <span class="badge">{{ statusName(fact.status) }}</span>
          <p v-if="fact.fact_kind === 'Edge'" class="relation-endpoints">{{ endpointName(fact.payload.source) }} → {{ endpointName(fact.payload.target) }}</p>
          <p v-else class="muted small-text">{{ endpointName(fact.payload.entity_id) }}</p>
          <PropertyTable :values="factValues(fact)" :label="propertyName" />
          <div v-for="support in details.supports.filter(item => item.fact_id === fact.id)" :key="support.id" class="support-detail">
            <PropertyTable :values="{ '支持状态': statusName(support.status), '来源类型': supportOriginName(support.origin), '可见范围': support.scope_ref, '数据位置': support.source_binding, ...(support.rule_revision != null ? { '映射修订': support.rule_revision } : {}) }" />
            <p v-if="support.binding?.source_kind === 'metadata_column'" class="muted small-text">{{ support.origin === 'agent_declared' ? '名称和含义是 Agent 依据材料作出的声明，引用可追溯不代表已独立确认业务解释。' : '此支持来自授权目录中的列定义，不是经营数值的查询结果。' }}</p>
            <PropertyTable v-if="support.binding?.evidence_refs?.length" :values="{ '证据引用': support.binding.evidence_refs }" />
            <div v-if="support.binding?.metadata_endpoint" class="metadata-relation-basis">
              <h5>行对象与列定义的关系依据</h5>
              <PropertyTable :values="metadataRelationValues(support.binding.metadata_endpoint)" />
              <p class="muted small-text">{{ support.binding.metadata_endpoint.basis === 'schema_applies_to_rows' ? '适用性来自元数据或文档声明；行对象实际存在，但这份支持不证明经营数值非空。' : support.binding.metadata_endpoint.basis === 'observed_non_null' ? '仅连接实际非空行中的对象；空值不代表该定义不适用。' : '请查看原始映射，当前无法解释此关系依据。' }}</p>
            </div>
            <div v-if="support.binding?.constant_properties && Object.keys(support.binding.constant_properties).length">
              <h5>来源位置与固定属性</h5>
              <PropertyTable :values="support.binding.constant_properties" :label="propertyName" />
            </div>
            <div v-if="support.binding?.row_context?.values">
              <h5>同一行路径上下文</h5>
              <PropertyTable :values="support.binding.row_context.values" />
            </div>
            <div v-for="(match, index) in support.binding?.lookup_matches || []" :key="index">
              <h5>记录匹配依据</h5>
              <PropertyTable :values="lookupValues(match)" />
            </div>
            <TechnicalDetails :value="support" title="来源标识与引用（开发调试）" />
          </div>
          <TechnicalDetails :value="fact" title="事实标识与原始结构（开发调试）" />
        </article>
        <details v-if="details.rules.length" class="mapping-details">
          <summary>字段映射与语义依据</summary>
          <article v-for="(rule, index) in details.rules" :key="index" class="fact-detail">
            <h4>{{ rule.definition || '来源字段映射' }}<template v-if="rule.mapping_index != null"> · 映射 {{ Number(rule.mapping_index) + 1 }}</template><template v-if="rule.revision"> · 修订 {{ rule.revision }}</template></h4>
            <p v-if="rule.description" class="muted small-text">{{ rule.description }}</p>
            <PropertyTable :values="sourceSummary(rule)" :label="propertyName" />
            <PropertyTable :values="mappingValues(rule)" :label="propertyName" />
            <TechnicalDetails :value="rule" title="规则标识与证据引用" />
          </article>
        </details>
        <p v-if="details.supports_truncated" class="note amber">本页支持超过 500 条，仅展示前 500 条。</p>
        <button v-if="details.next_offset !== null" class="btn small" :disabled="detailsLoading" @click="emit('loadMoreDetails')">读取更多事实支持</button>
      </template>
    </section>

    <TechnicalDetails :value="selected" title="节点底层技术结构（开发调试）" />
  </div>
</template>

<style scoped>
.relation-selection { display: block; width: 100%; text-align: left; background: transparent; color: inherit; cursor: pointer; border: 0; }
.fact-detail { padding: 12px 0; border-bottom: 1px solid var(--border, #e2e8f0); }
.relation-endpoints { overflow-wrap: anywhere; line-height: 1.7; }
.support-detail { margin: 8px 0; padding: 6px; background: var(--surface-alt, #f8fafc); }
.mapping-details summary { cursor: pointer; }
.fact-detail ul { padding-left: 18px; font-size: 12px; }
.standard-card-grid { display: grid; gap: 8px; margin-top: 10px; padding: 10px 12px; background: #f8fafd; border: 1px solid var(--line, #e5eaf2); border-radius: 8px; }
.standard-row { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; font-size: 12px; }
.standard-label { color: var(--muted, #7c899e); flex-shrink: 0; }
.standard-val { text-align: right; overflow-wrap: anywhere; }
.section-title-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
.standard-desc { font-size: 12px; line-height: 1.7; color: #31425e; }
.catalog-notes { margin-top: 10px; padding: 8px 10px; background: #fcfdff; border-left: 3px solid #cbdcf9; font-size: 11px; }
.catalog-note-item { margin-top: 6px; }
.compact-list { margin: 4px 0 0; padding-left: 16px; }
.dynamic-attr-list { display: grid; gap: 8px; margin-bottom: 10px; }
.dynamic-attr-item { padding: 8px 10px; border: 1px solid var(--line, #e5eaf2); border-radius: 6px; background: #fcfdff; font-size: 12px; }
.attr-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.attr-origin { margin-top: 4px; }
.source-evidence-card { margin-top: 8px; padding: 10px 12px; border: 1px solid #dce7fb; border-radius: 8px; background: #f8fbff; }
.ev-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.ev-meta { margin-top: 6px; display: grid; gap: 4px; line-height: 1.6; overflow-wrap: anywhere; }
</style>
