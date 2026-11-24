<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import DeferredDetails from './DeferredDetails.vue'
import PropertyTable from './PropertyTable.vue'
import { isHierarchyLoop } from './graphRelationLines'
import TechnicalDetails from './TechnicalDetails.vue'
import { metadataRelationValues, nodeCaption, nodeIdentity, supportOriginName, vocabularyDescription, vocabularyLabel } from './graphLabels'
import type { DisplaySchema, MetadataEndpoint } from './graphLabels'
import type { GraphEdge, GraphNode, StandardNode, StandardNodeSourceEvidence } from './types'

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
  loadedIncidentEdges: number
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

const identity = computed(() => standardNode.value?.identity ?? nodeIdentity(props.selected))
const title = computed(() => {
  const standard = standardNode.value
  const names = nameVariants.value.length ? nameVariants.value : standard?.source_names ?? []
  return standard?.preferred_source_name || (names.length > 1 ? names.join(' / ') : standard?.source_name)
    || standard?.chinese_name || standard?.english_name || nodeCaption(props.selected)
})
const aliases = computed(() => (standardNode.value?.aliases ?? (Array.isArray(props.selected.aliases) ? props.selected.aliases : []))
  .filter(alias => alias && alias !== title.value))
const sources = computed(() => standardNode.value?.source_evidences.filter(source => source.table) ?? [])
function sourceColumns(source: StandardNodeSourceEvidence) {
  const roles = new Map<string, Set<string>>()
  function add(column: string, role: string) {
    if (!column) return
    if (!roles.has(column)) roles.set(column, new Set())
    roles.get(column)!.add(role)
  }
  for (const column of source.key_columns) add(column, '身份键')
  for (const field of source.contributed_fields) add(field.column, field.field)
  return [...roles].map(([column, fields]) => ({ column, roles: [...fields].join('、') }))
}

const businessAttributes = computed(() => {
  const reserved = new Set(sources.value.flatMap(source => [
    ...source.key_columns,
    ...source.contributed_fields.filter(field => ['chinese_name', 'english_name', 'alias', 'description', 'identity'].includes(field.role))
      .map(field => field.column),
  ].map(column => JSON.stringify([source.source_id, source.qualified_table, column]))))
  function usable(origin: { source_id: string; qualified_table: string; column?: string | null }) {
    return origin.column && origin.source_id && origin.source_id !== 'unknown' && origin.qualified_table
      && origin.qualified_table !== origin.source_id
      && !reserved.has(JSON.stringify([origin.source_id, origin.qualified_table, origin.column]))
  }
  return (standardNode.value?.attributes ?? []).flatMap(attr => {
    if (attr.observations?.length) {
      const observations = attr.observations.filter(obs =>
        obs.binding_role ? obs.binding_role === 'attribute_column' : usable(obs))
      if (!observations.length) return []
      const values = [...new Map(observations.map(obs => [JSON.stringify(obs.value), obs.value])).values()]
      return [{ ...attr, values, observations }]
    }
    // Older details may only identify columns at the field level. Mixed or
    // unlocated values stay in the original record until their source is known.
    return attr.origin_tables.length && attr.origin_tables.every(usable) ? [attr] : []
  })
})
const recordProperties = computed(() => props.selected.properties && typeof props.selected.properties === 'object'
  && !Array.isArray(props.selected.properties) ? props.selected.properties as Record<string, unknown> : {})
const nameVariants = computed(() => {
  if (standardNode.value?.label_conflict) return standardNode.value.label_variants || []
  return props.selected.label_conflict && Array.isArray(props.selected.label_variants) ? props.selected.label_variants : []
})

const statusName = (value?: unknown) => ({ accepted: '已接受', candidate: '候选', unresolved: '未决', conflict: '冲突' }[String(value)] || '状态未提供')
const kindName = (value: unknown) => vocabularyLabel('entity_types', value, props.vocabulary)
const relationName = (value: unknown) => vocabularyLabel('relations', value, props.vocabulary)
const propertyName = (value: string) => {
  const label = vocabularyLabel('properties', value, props.vocabulary)
  return label === value + '（未提供中文名）' ? value : label
}
const relationDescription = (value: unknown) => vocabularyDescription('relations', value, props.vocabulary)

const labelOrigin = (node: GraphNode) => ({
  declared: '来源未提供主名称',
  relations: '来源未提供主名称',
  identity: '来源未提供主名称',
  metadata: '名称来自字段元数据；此节点代表列定义',
  metadata_declared: '名称由 Agent 依据元数据或文档声明；此节点代表列定义',
}[String(node.label_origin)] || '')

const accessLabel = (node: GraphNode) => Array.isArray(node.scopes) ? node.scopes.join('、') : ''

const canvasNodes = computed(() => new Map(props.allNodes.map(node => [node.id, node])))
const endpointName = (id: unknown) => {
  const key = String(id || '')
  const node = props.detailNodes[key] || canvasNodes.value.get(key)
  return node ? nodeCaption(node) : '节点名称尚未加载'
}

function direction(edge: GraphEdge) {
  if (edge.source === edge.target) return '自环'
  const fromHere = edge.source === props.selected.id
  if (edge.hierarchy_direction === 'outgoing') return fromHere ? '下级' : '上级'
  if (edge.hierarchy_direction === 'incoming') return fromHere ? '上级' : '下级'
  return fromHere ? '出向' : '入向'
}

const hierarchyLoops = computed(() => props.adjacent.filter(isHierarchyLoop))
const neighborGroups = computed(() => {
  const groups = new Map<string, GraphEdge[]>()
  for (const edge of props.adjacent) {
    if (isHierarchyLoop(edge)) continue
    const other = edge.source === props.selected.id ? edge.target : edge.source
    const group = groups.get(other)
    if (group) group.push(edge)
    else groups.set(other, [edge])
  }
  return [...groups].map(([id, edges]) => ({ id, edges, directions: [...new Set(edges.map(direction))].join('、') }))
})
const neighborLimit = ref(40)
const expandedNeighbors = ref(new Map<string, number>())
function toggleNeighbor(id: string, event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) expandedNeighbors.value.set(id, 40)
  else expandedNeighbors.value.delete(id)
}
watch(() => props.selected.id, () => { neighborLimit.value = 40; expandedNeighbors.value.clear() })
const factTitle = (fact: FactItem) => fact.fact_kind === 'PropertyValue' ? propertyName(String(fact.payload.name || '')) : fact.fact_kind === 'Edge' ? relationName(fact.payload.relation) : fact.fact_kind === 'UnresolvedRelation' ? '待确认关系' : '对象存在'
const visibleQualifiers = (value: unknown) => Object.fromEntries(Object.entries((value || {}) as Record<string, unknown>).filter(([key]) => !['semantic_basis', 'document_ref', 'source_group'].includes(key)))
const contextValues = (value: unknown) => { const c = value as { status?: string; dimensions?: Record<string, unknown> } | undefined; return !c || c.status === 'unknown' ? { '适用范围': '尚未确认' } : c.status === 'global' ? { '适用范围': '通用知识' } : { '适用范围': c.dimensions } }
const factValues = (fact: FactItem) => fact.fact_kind === 'PropertyValue' ? { [String(fact.payload.name)]: fact.payload.value, ...contextValues(fact.payload.business_context) } : fact.fact_kind === 'Edge' ? { ...visibleQualifiers(fact.payload.qualifiers), ...contextValues(fact.payload.business_context) } : fact.fact_kind === 'UnresolvedRelation' ? { '状态': '端点尚未确认', '候选端点': fact.payload.candidate_endpoints, '观察': fact.payload.observations } : { kind: kindName(fact.payload.kind) }
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


</script>

<template>
  <div class="node-inspector">
    <header class="standard-node-summary" aria-label="节点名称与类型">
      <div class="entity-kind">
        {{ kindName(standardNode?.type_family || selected.original_type || selected.kind || selected.layer).replace(/（未提供中文名）$/, '') }}
        <span v-if="selected.status && selected.status !== 'accepted'" class="badge amber">{{ statusName(selected.status) }}</span>
      </div>
      <h2 class="detail-name">{{ title }}</h2>
      <p v-if="nameVariants.length" class="note amber">{{ standardNode?.preferred_source_name ? '各来源名称：' + nameVariants.join('、') : '各来源记录了不同名称' }}</p>
      <p v-if="labelOrigin(selected) && !standardNode?.source_name && !standardNode?.english_name" class="muted small-text">{{ labelOrigin(selected) }}</p>
      <p v-if="standardNode?.english_name && standardNode.english_name !== title" class="secondary-name">{{ standardNode.english_name }}</p>
      <p v-if="standardNode?.business_description_origin === 'source_column'" class="standard-desc">{{ standardNode.business_description }}</p>
    </header>
    <p v-if="selected.overview_only" class="note">正在读取完整节点信息。</p>

    <section v-if="Object.keys(identity).length" class="node-section">
      <h3>实体身份</h3>
      <PropertyTable :values="identity" :label="propertyName" />
    </section>

    <section v-if="aliases.length || businessAttributes.length" class="node-section" aria-label="业务属性">
      <h3>业务属性</h3>
      <PropertyTable v-if="aliases.length" :values="{ '别名': aliases }" />
      <div v-for="attr in businessAttributes" :key="attr.key" class="dynamic-attr-item">
        <PropertyTable :values="{ [propertyName(attr.label)]: attr.values }" />
        <DeferredDetails v-if="attr.observations?.length" :key="selected.id + ':' + attr.key" class="attr-origin" :title="'查看取值来源（' + attr.observations.length + '）'">
          <article v-for="(obs, index) in attr.observations" :key="index" class="support-detail">
            <PropertyTable :values="{ '值': obs.value, '来源': [obs.source_id, obs.qualified_table, obs.column].join('.'), ...(obs.raw_value !== undefined && obs.raw_value !== obs.value ? { '原值': obs.raw_value } : {}), ...(obs.original_property && obs.original_property !== attr.key ? { '原属性': obs.original_property } : {}), ...(obs.scope_ref ? { '可见范围': obs.scope_ref } : {}), ...(obs.evidence_refs?.length ? { '证据引用': obs.evidence_refs } : {}) }"/>
            <PropertyTable :values="contextValues(obs.business_context)" />
            <template v-if="obs.row_context?.values"><strong>同一行的上下文</strong><PropertyTable :values="obs.row_context.values"/></template>
          </article>
        </DeferredDetails>
        <p v-else class="attr-origin">{{ attr.origin_tables.map(o => [o.source_id, o.qualified_table, o.column].join('.')).join('、') }}</p>
      </div>
    </section>

    <section v-if="sources.length" class="node-section" aria-label="物理表与列">
      <h3>数据来源</h3>
      <div v-for="ev in sources" :key="JSON.stringify([ev.source_id,ev.qualified_table])" class="source-evidence-card">
        <div class="source-heading"><strong>{{ ev.qualified_table }}</strong> <span>{{ ev.source_id }}</span></div>
        <dl v-if="sourceColumns(ev).length" class="source-columns">
          <template v-for="field in sourceColumns(ev)" :key="field.column">
            <dt>{{ field.roles }}</dt><dd>{{ ev.qualified_table }}.{{ field.column }}</dd>
          </template>
        </dl>
        <p v-else class="muted small-text">此来源未记录列映射。</p>
        <details v-if="ev.evidence_refs.length || ev.inherited_from" class="attr-origin">
          <summary>查看来源记录</summary>
          <p v-if="ev.inherited_from">继承自 {{ ev.inherited_from.source_project_id || ev.inherited_from.inherit_id }}</p>
          <p>{{ ev.evidence_refs.join('、') }}</p>
        </details>
      </div>
    </section>

    <div class="divider"></div>
    <h3>已加载关联 · {{ neighborGroups.length }} 个对象 / {{ adjacent.length - hierarchyLoops.length }} 条关系</h3>
    <p class="field-hint" aria-label="节点关联范围">
      当前画布显示 {{ drawnIncidentEdges }} {{ loadedIncidentEdges === drawnIncidentEdges ? '条相连关系' : `条连线，已加载 ${loadedIncidentEdges} 条原始关系` }}<template v-if="knownIncidentEdges != null">，当前授权图中共有 {{ knownIncidentEdges }} 条</template><template v-else>；完整关系数量尚未读取</template>。<template v-if="knownIncidentEdges == null || loadedIncidentEdges < knownIncidentEdges">当前是局部视图，不能据此判断节点孤立。</template>
    </p>
    <button class="btn small" :disabled="pageLoading" @click="emit('showTwoHops', selected)">查看两跳关联</button>
    <button v-if="serverPaging && !fullTransport && !hasNeighborhood" class="btn small" :disabled="pageLoading || expansionOffset === null" @click="emit('expandNode')">
      {{ hasExpansionOffset ? '继续展开关联' : '展开相邻节点' }}
    </button>
    <component v-for="group in neighborGroups.slice(0, neighborLimit)" :key="`${selected.id}:${group.id}`" @toggle.self="toggleNeighbor(group.id, $event)"
      :is="group.edges.length > 1 ? 'details' : 'div'" class="neighbor-relations">
      <summary v-if="group.edges.length > 1">
        {{ group.directions }} · {{ endpointName(group.id) }} · {{ group.edges.length }} 条关系
      </summary>
      <button v-for="(edge, i) in group.edges.slice(0, group.edges.length === 1 ? 1 : expandedNeighbors.get(group.id) || 0)" :key="edge.id || i" type="button" class="edge-detail relation-selection" @click="emit('selectEdge', edge)">
        <span class="badge" :title="relationDescription(edge.relation)">{{ direction(edge) }} · {{ relationName(edge.relation) }}<template v-if="edge.status && edge.status !== 'accepted'"> · {{ statusName(edge.status) }}</template></span>
        <p>{{ endpointName(edge.source) }} → {{ endpointName(edge.target) }}</p>
      </button>
      <button v-if="expandedNeighbors.has(group.id) && group.edges.length > expandedNeighbors.get(group.id)!" class="btn small"
        @click="expandedNeighbors.set(group.id, expandedNeighbors.get(group.id)! + 40)">查看更多关系</button>
    </component>
    <details v-if="hierarchyLoops.length" class="neighbor-relations">
      <summary>层级自环 · {{ hierarchyLoops.length }} 条原始记录</summary>
      <button v-for="edge in hierarchyLoops" :key="edge.id" type="button" class="edge-detail relation-selection" @click="emit('selectEdge', edge)">{{ relationName(edge.relation) }} · {{ endpointName(edge.source) }} → {{ endpointName(edge.target) }}</button>
    </details>
    <button v-if="neighborGroups.length > neighborLimit" class="btn small" @click="neighborLimit += 40">查看更多关联对象</button>

    <section v-if="projectId" aria-label="来源支持">
      <div class="divider"></div>

      <p v-if="detailsLoading" role="status">正在读取来源…</p>
      <p v-if="detailsError" role="alert">{{ detailsError }}
        <button type="button" class="btn small" @click="emit('retryDetails')">重试读取详情</button>
      </p>
      <DeferredDetails v-if="details" :key="selected.id" title="事实与抽取依据" class="audit-details node-facts">
        <article v-for="fact in details.facts" :key="fact.id" class="fact-detail">
          <strong>{{ factTitle(fact) }}</strong> <span class="badge">{{ statusName(fact.status) }}</span>
          <p v-if="fact.fact_kind === 'Edge'" class="relation-endpoints">{{ endpointName(fact.payload.source) }} → {{ endpointName(fact.payload.target) }}</p>
          <p v-else class="muted small-text">{{ endpointName(fact.payload.entity_id) }}</p>
          <PropertyTable :values="factValues(fact)" :label="propertyName" />
          <div v-for="support in details.supports.filter(item => item.fact_id === fact.id)" :key="support.id" class="support-detail">
            <PropertyTable :values="{ '支持状态': statusName(support.status), '来源类型': supportOriginName(support.origin), '可见范围': support.scope_ref, '数据位置': support.source_binding, ...(support.rule_revision != null ? { '映射修订': support.rule_revision } : {}) }" />
            <p v-if="support.binding?.source_kind === 'metadata_column'" class="muted small-text">{{ support.origin === 'agent_declared' ? '名称和含义由 Agent 根据所引材料解释，仍需独立核实。' : '此处引用授权数据目录中的列定义。' }}</p>
            <PropertyTable v-if="support.binding?.evidence_refs?.length" :values="{ '证据引用': support.binding.evidence_refs }" />
            <div v-if="support.binding?.metadata_endpoint" class="metadata-relation-basis">
              <h5>行对象与列定义的关系依据</h5>
              <PropertyTable :values="metadataRelationValues(support.binding.metadata_endpoint)" />
              <p class="muted small-text">{{ support.binding.metadata_endpoint.basis === 'schema_applies_to_rows' ? '适用性来自元数据或文档。已找到对应记录，该字段是否非空尚未验证。' : support.binding.metadata_endpoint.basis === 'observed_non_null' ? '只连接该字段有值的记录。空值的适用性尚未验证。' : '请查看原始映射，当前无法解释此关系依据。' }}</p>
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
      </DeferredDetails>
    </section>
    <DeferredDetails v-if="Object.keys(recordProperties).length || standardNode?.catalog_metadata_notes.length || standardNode?.business_description_origin === 'entity_definition' || accessLabel(selected)" :key="'mapping:' + selected.id" class="audit-details record-properties" title="原始属性与映射说明">
      <p v-if="accessLabel(selected)">可见范围：{{ accessLabel(selected) }}</p>
      <p v-if="standardNode?.business_description_origin === 'entity_definition'">类型定义：{{ standardNode.business_description }}</p>
      <PropertyTable v-if="Object.keys(recordProperties).length" :values="recordProperties" :label="propertyName" />
      <template v-if="standardNode?.catalog_metadata_notes.length">
        <h4>源表或列的目录注释</h4>
        <article v-for="note in standardNode.catalog_metadata_notes" :key="JSON.stringify([note.source_id,note.qualified_table])" class="catalog-note">
          <strong>{{ note.source_id }} · {{ note.qualified_table }}</strong>
          <p v-if="note.table_comment">{{ note.table_comment }}</p>
          <p v-for="col in note.column_comments" :key="col.column">{{ col.column }} ({{ col.data_type }}) · {{ col.comment }}</p>
        </article>
      </template>
    </DeferredDetails>

    <TechnicalDetails :value="selected" title="节点底层技术结构（开发调试）" />
  </div>
</template>

<style scoped>
.neighbor-relations summary { cursor: pointer; padding: 10px 0; overflow-wrap: anywhere; }
.relation-selection { display: block; width: 100%; text-align: left; background: transparent; color: inherit; cursor: pointer; border: 0; }
.fact-detail { padding: 12px 0; border-bottom: 1px solid var(--border, #e2e8f0); }
.relation-endpoints { overflow-wrap: anywhere; line-height: 1.7; }
.support-detail { margin: 8px 0; padding: 6px; background: var(--surface-alt, #f8fafc); }
.mapping-details summary { cursor: pointer; }
.fact-detail ul { padding-left: 18px; font-size: 12px; }
.entity-kind, .source-heading span, .secondary-name, .attr-origin { color: var(--muted, #62738b); font-size: 12px; }
.standard-desc { font-size: 13px; line-height: 1.6; }
.node-section { border-top: 1px solid var(--line, #e5eaf2); padding-top: 12px; margin: 14px 0; }
.source-evidence-card { overflow-wrap: anywhere; margin: 10px 0; }
.source-heading { display: flex; align-items: baseline; flex-wrap: wrap; gap: 7px; font-size: 13px; }
.source-columns { display: grid; grid-template-columns: minmax(4em, 30%) 1fr; gap: 4px 8px; margin: 6px 0; font-size: 12px; }
.source-columns dt { color: var(--muted, #62738b); }
.source-columns dd { margin: 0; }
.catalog-note { margin: 8px 0; }
.audit-details { padding: 8px 0; overflow-wrap: anywhere; }
.audit-details :deep(summary), .attr-origin :deep(summary) { cursor: pointer; color: var(--muted, #62738b); font-size: 12px; }
</style>
