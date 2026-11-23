<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ApiError, ensureSession, projectPath, request } from './api'
import BusinessContextSelect from './BusinessContextSelect.vue'
import PropertyTable from './PropertyTable.vue'
import AvailableContext from './AvailableRetrievalContext.vue'
import type { AvailableRetrievalContext, BusinessContext, FactContext, EmbeddingConfig, RetrievalStatusResponse } from './types'

type Observation = { business_context?: FactContext; properties?: Record<string, unknown>; evidence_refs?: string[]; source_tables?: string[] }
type PathStep = { source: string; target: string; relation: string; direction?: string; observations?: Observation[] }

interface PathResult {
  status: 'found' | 'no_visible_seed' | 'target_not_reached' | 'incomplete' | 'no_compatible_path'
  equally_ranked_paths?: number
  uncovered_seed_ids: string[]
  paths: Array<{ node_ids: string[]; edge_ids: string[]; stop_reason: string; business_context?: FactContext }>
}

interface RetrievalQueryResponse {
  agent_context?: Record<string, unknown>
  sql_preview?: { status: 'generated' | 'insufficient_context' | 'unavailable'; sql?: string; reason?: string; code?: string }

  available_context?: AvailableRetrievalContext
  grouping?: { axis: string; paths: PathResult['paths'] }
  plan?: {
    intent: string
    parser_fallback?: string
    conditions: Array<{text: string; role: string | null; match?: string; candidates?: Array<{display_name?: string}>}>
    suggestions?: Array<{text: string; reason?: string; candidates: Array<{node_id: string; display_name?: string}>}>
    drill_role?: string | null
    requested_level?: string | null
    business_context?: BusinessContext | null
    unresolved: string[]
    excluded_terms: string[]
  }
  overview?: {
    status: string
    counts?: {nodes: number; edges: number; communities: number}
    communities?: Array<{id: string; size: number}>
    representative_count?: number
  }
  dimensional?: {
    status: string
    fact_ids: string[]
    children: string[]
    matched_conditions: string[]
    level_node_ids?: string[]
    stop_reason: string
    witnesses: Array<{fact_id: string; conditions: Array<{text: string; node_id: string}>; row_id: string; edge_ids: string[]}>
  }
  association_paths?: PathResult['paths']
  drilldown?: PathResult
  traverse?: PathResult
  query: string
  scopes: string[]
  slots: Array<{
    surface: string
    role_hint?: string | null
    source: string
    candidates: Array<{
      node_id: string
      kind: string
      rrf_score: number | null
      vec_rank: number | null
      vec_score: number | null
      lex_rank: number | null
      match_type: string | null
    }>
  }>
  nodes: Array<{
    id: string
    kind: string
    display_name?: string
    chinese_name?: string | null
    english_name?: string | null
    description?: string | null
    aliases?: string[]
    identity?: Record<string, unknown>
    degree?: number
    matched_slots?: string[]
    is_seed?: boolean
    is_bridge?: boolean
    [key: string]: unknown
  }>
  edges: Array<{
    id: string
    relation: string
    source: string
    target: string
    evidence_refs?: string[]
    source_tables?: string[]
    observations?: Observation[]
    [key: string]: unknown
  }>
  bridge_nodes: Array<{
    node_id: string
    kind: string
    display_name: string
    bridge_role: string
    matched_slots: string[]
    context_status?: string
    path_groups?: Array<{ business_context: FactContext; context_status: string; paths_by_slot: Record<string, PathStep[]> }>
  }>
  connected_components: string[][]
  truncated: boolean
  truncation_reasons: string[]
}

const props = defineProps<{
  projectId: string
  embedding?: EmbeddingConfig
  status?: RetrievalStatusResponse | null
  busy: boolean
  syncing: boolean
}>()

const emit = defineEmits<{
  'vectorize': []
}>()

const queryText = ref('')
const businessContext = ref<BusinessContext | null>(null)
const atomicOpen = ref(false)
const mode = ref<'search' | 'id' | 'drilldown' | 'traverse'>('search')
const method = ref<'hybrid' | 'keyword' | 'vector'>('hybrid')
const field = ref<'all' | 'name' | 'description'>('all')
const hops = ref<0 | 1 | 2>(2)
const idHops = ref<0 | 1 | 2>(0)
const seedInput = ref<'query' | 'ids'>('query')
const nodeId = ref('')
const seedIds = ref('')
const pathHint = ref('')
const relationTypes = ref('')
const includeEndpointVariants = ref(false)
const direction = ref<'outgoing' | 'incoming' | 'both'>('outgoing')
const targetId = ref('')
const maxDepth = ref(12)
const listValues = (value: string) => [...new Set(value.split(/[，,\n]/).map(item => item.trim()).filter(Boolean))]
const selectedRelations = computed(() => listValues(relationTypes.value))
watch(selectedRelations, names => { if (!names.length) includeEndpointVariants.value = false })
const pathMode = computed(() => mode.value === 'drilldown' || mode.value === 'traverse')
const bySeedIds = computed(() => pathMode.value && seedInput.value === 'ids')
watch(mode, value => { direction.value = value === 'traverse' ? 'both' : 'outgoing' })
const hasInput = computed(() => atomicOpen.value && mode.value === 'id' ? Boolean(nodeId.value.trim()) : atomicOpen.value && bySeedIds.value ? listValues(seedIds.value).length > 0 : Boolean(queryText.value.trim()))
const topPSeeds = ref(4)
const working = ref(false)
const panelError = ref('')
const result = ref<RetrievalQueryResponse | null>(null)
let queryEpoch = 0
let queryController: AbortController | undefined

function clearResult() {
  queryEpoch++
  queryController?.abort()
  queryController = undefined
  working.value = false
  panelError.value = ''
  result.value = null
}
onUnmounted(clearResult)
watch([businessContext, atomicOpen, mode, seedInput, () => props.status?.generation, () => props.status?.vectorized], clearResult)
watch(() => props.projectId, () => {
  clearResult()
  businessContext.value = null
  queryText.value = nodeId.value = seedIds.value = pathHint.value = targetId.value = relationTypes.value = ''
})
const pathResult = computed(() => result.value?.traverse || result.value?.drilldown ||
  (result.value?.grouping || result.value?.association_paths ? { status: 'found', paths: result.value.grouping?.paths || result.value.association_paths || [], uncovered_seed_ids: [], equally_ranked_paths: 0 } : null))
const fallbackLabels: Record<string, string> = {
  model_parser_not_configured: '当前项目没有可用的模型解析器',
  model_interpretation_unavailable: '模型未返回可用的解析结果',
  no_visible_context: '当前范围没有可见对象',
}
const activeStatus = computed(() => props.status)
const showMainGraph = computed(() => result.value && (
  !result.value.available_context || result.value.nodes.length > 0 || result.value.edges.length > 0
))
const stopLabels: Record<string, string> = {
  anchors_connected: '已连接指定节点', path_selected: '已找到有来源的关联路径', level_reached: '已到指定层级',
  leaf: '已到当前关系和范围内的叶节点', target_reached: '已到指定终点',
  depth_limit: '已到深度上限', output_limit: '已到返回数量上限',
  cycle: '遇到环路', search_limit: '已到搜索上限',
}
const nodeMap = computed(() => {
  const map = new Map<string, RetrievalQueryResponse['nodes'][number]>()
  for (const n of result.value?.nodes || []) map.set(n.id, n)
  return map
})
const edgeMap = computed(() => new Map((result.value?.edges || []).map(edge => [edge.id, edge])))
const pathEdges = computed(() => new Map([...(result.value?.available_context?.edges || []),
  ...(result.value?.edges || [])].map(edge => [edge.id, edge])))
const contextLabel = (context?: FactContext) => !context || context.status === 'unknown' ? '适用范围尚未确认'
  : context.status === 'global' ? '通用知识' : Object.entries(context.dimensions).map(([key, value]) => `${key}：${String(value)}`).join(' · ')
const nodeLabel = (id: string) => nodeMap.value.get(id)?.display_name ||
  result.value?.available_context?.nodes.find(n => n.id === id)?.display_name || id

const generatingSQL = ref(false)
async function runQuery(withSQL = false) {
  if (!hasInput.value || working.value || props.syncing || !props.status?.vectorized) return
  generatingSQL.value = withSQL
  const epoch = ++queryEpoch
  const controller = new AbortController()
  queryController = controller
  result.value = null
  working.value = true
  panelError.value = ''
  const project = props.projectId
  const body: Record<string, unknown> = {}
  if (businessContext.value !== null || atomicOpen.value) body.business_context = businessContext.value
  if (!atomicOpen.value) {
    body.query = queryText.value.trim()
  } else if (mode.value === 'id') {
    Object.assign(body, { node_id: nodeId.value.trim(), hops: idHops.value })
  } else if (pathMode.value) {
    Object.assign(body, { mode: mode.value, direction: direction.value, max_depth: maxDepth.value })
    if (bySeedIds.value) {
      body.seed_ids = listValues(seedIds.value)
      if (mode.value === 'drilldown' && pathHint.value.trim()) body.query = pathHint.value.trim()
    } else {
      body.query = queryText.value.trim()
      if (mode.value === 'drilldown') Object.assign(body, { field: field.value, method: method.value })
    }
    if (selectedRelations.value.length) {
      body.relation_types = selectedRelations.value
      if (includeEndpointVariants.value) body.include_endpoint_variants = true
    }
    if (targetId.value.trim()) body.target_id = targetId.value.trim()
  } else {
    Object.assign(body, { mode: 'search', query: queryText.value.trim(), field: field.value, method: method.value, top_p_seeds: topPSeeds.value, hops: hops.value })
  }
  try {
    await ensureSession(project)
    if (epoch !== queryEpoch) return
    const value = await request<RetrievalQueryResponse>(`${projectPath(project)}/retrieval/query${withSQL ? '?sql_preview=true' : ''}`, {
      method: 'POST',
      signal: controller.signal,
      timeoutMs: null,
      body: JSON.stringify(body),
    })
    if (epoch === queryEpoch) result.value = value
  } catch (err) {
    if (epoch === queryEpoch) panelError.value = err instanceof Error ? err.message : '子图召回失败，请重试。'
  } finally {
    if (epoch === queryEpoch) {
      working.value = false
      queryController = undefined
    }
  }
}
</script>

<template>
  <div class="retrieval-page">
    <section class="panel retrieval-search-panel">
      <div class="panel-head">
        <h2>图谱检索</h2>
        <div v-if="activeStatus" class="actions index-status">
          <span class="badge" :class="activeStatus.vectorized ? 'green' : 'amber'">{{ activeStatus.vectorized ? '已完成向量化' : activeStatus.status === 'stale' ? '索引待更新' : activeStatus.has_graph ? '未向量化' : '尚无图谱' }}</span>
          <small v-if="activeStatus.vectorized">{{ activeStatus.embedder_model }} · {{ activeStatus.vector_dim }} 维</small>
          <button v-if="activeStatus.has_graph" class="btn small" type="button" :disabled="syncing || working || busy" @click="emit('vectorize')">{{ syncing ? '正在向量化…' : activeStatus.vectorized ? '重新向量化' : '立即向量化' }}</button>
        </div>
      </div>
      <div class="panel-body">
        <form class="retrieval-form" @submit.prevent="runQuery()">
          <div v-if="!atomicOpen" class="field">
            <label for="retrieval-query">问题</label>
            <textarea id="retrieval-query" v-model="queryText" maxlength="4000" rows="3" required :disabled="working" placeholder="输入想查的问题，或粘贴一段文字"/>
          </div>
          <button type="button" class="text-button atomic-toggle" :aria-expanded="atomicOpen" @click="atomicOpen = !atomicOpen">{{ atomicOpen ? '返回问题检索' : '调试检索算子' }}</button>
          <template v-if="atomicOpen">
          <div class="retrieval-modes" role="group" aria-label="检索方式">
            <button v-for="item in ([['search','文本检索'], ['id','按节点 ID'], ['traverse','多层关联'], ['drilldown','路径下钻']] as const)" :key="item[0]" type="button" :aria-pressed="mode === item[0]" @click="mode = item[0]">{{ item[1] }}</button>
          </div>
          <div v-if="pathMode" class="field">
            <label for="drilldown-start">{{ mode === 'traverse' ? '路径节点' : '路径起点' }}</label>
            <select id="drilldown-start" v-model="seedInput"><option value="query">{{ mode === 'traverse' ? '从问题识别' : '用文本匹配起点' }}</option><option value="ids">指定节点 ID</option></select>
          </div>
          <div v-if="mode === 'id'" class="field">
            <label for="retrieval-node-id">节点 ID</label>
            <input id="retrieval-node-id" maxlength="512" v-model="nodeId" required :disabled="working" autocomplete="off"/>
          </div>
          <div v-else-if="bySeedIds" class="field">
            <label for="drilldown-seeds">{{ mode === 'traverse' ? '路径须经过的节点 ID' : '起点 ID' }}</label>
            <textarea id="drilldown-seeds" v-model="seedIds" required :disabled="working" rows="2" placeholder="每行一个，也可用逗号分隔"/>
          </div>
          <div v-else class="field">
            <label for="retrieval-query">问题或关键词</label>
            <input id="retrieval-query" maxlength="4000" v-model="queryText" required :disabled="working" placeholder="输入问题、实体名称或编码"/>
          </div>
          <div v-if="mode === 'search' || (mode === 'drilldown' && !bySeedIds)" class="retrieval-params">
            <div class="field"><label for="retrieval-field">匹配字段</label>
              <select id="retrieval-field" v-model="field" :disabled="working"><option value="all">全部</option><option value="name">名称</option><option value="description">描述</option></select>
            </div>
            <div class="field"><label for="retrieval-method">匹配方式</label>
              <select id="retrieval-method" v-model="method" :disabled="working"><option value="hybrid">混合检索</option><option value="keyword">关键词</option><option value="vector">向量相似度</option></select>
            </div>
          </div>
          <div v-if="mode === 'id'" class="retrieval-params">
            <div class="field"><label for="retrieval-id-hops">返回范围</label><select id="retrieval-id-hops" v-model.number="idHops" :disabled="working"><option :value="0">仅此节点</option><option :value="1">一跳邻域</option><option :value="2">两跳邻域</option></select></div>
          </div>
          <details v-else class="retrieval-options">
            <summary>{{ pathMode ? '路径条件' : '召回范围' }}</summary>
            <div class="retrieval-params">
              <div v-if="mode === 'search'" class="field"><label for="retrieval-seeds">每个关键词的起点数</label><input id="retrieval-seeds" v-model.number="topPSeeds" type="number" min="1" max="20" required :disabled="working"/></div>
              <div v-if="mode === 'search'" class="field"><label for="retrieval-hops">邻域</label><select id="retrieval-hops" v-model.number="hops" :disabled="working"><option :value="0">仅匹配节点</option><option :value="1">一跳</option><option :value="2">两跳</option></select></div>
              <template v-else>
                <div v-if="bySeedIds && mode === 'drilldown'" class="field"><label for="drilldown-hint">关注的内容</label><input id="drilldown-hint" v-model="pathHint" maxlength="4000" placeholder="可选，用于选择路径" :disabled="working"/></div>
                <div class="field"><label for="drilldown-direction">方向</label><select id="drilldown-direction" v-model="direction" :disabled="working"><option v-if="mode === 'traverse'" value="both">双向查找</option><option value="outgoing">沿出边</option><option value="incoming">沿入边</option></select></div>
                <div class="field"><label for="drilldown-depth">最大深度</label><input id="drilldown-depth" v-model.number="maxDepth" type="number" min="1" max="64" required :disabled="working"/></div>
                <div class="field">
                  <label for="drilldown-relations">关系类型</label>
                  <input id="drilldown-relations" v-model="relationTypes" placeholder="可选，多个类型用逗号分隔" :disabled="working"/>
                  <label v-if="selectedRelations.length"><input id="include-endpoint-variants" v-model="includeEndpointVariants" type="checkbox" :disabled="working"/> 同时查询因端点类型变化而分出的同一关系</label>
                </div>
                <div class="field"><label for="drilldown-target">终点 ID</label><input id="drilldown-target" maxlength="512" v-model="targetId" placeholder="可选" :disabled="working"/></div>
              </template>
            </div>
          </details>
          </template>
          <details class="retrieval-options"><summary>限定适用范围</summary>
          <BusinessContextSelect v-model="businessContext" :project-id="projectId" :refresh-key="status?.generation" :disabled="working || syncing"/>
          </details>
          <div class="actions">
            <button class="btn primary" type="submit" :disabled="working || syncing || !activeStatus?.vectorized || !hasInput">{{ (working && !generatingSQL) ? '正在检索…' : '检索' }}</button>
            <button v-if="!atomicOpen" class="btn" type="button" :disabled="working || syncing || !activeStatus?.vectorized || !hasInput" @click="runQuery(true)">{{ working && generatingSQL ? '正在召回并生成 SQL…' : '召回并试写 SQL' }}</button>
            <button v-if="working" class="btn small" type="button" @click="clearResult">停止等待</button>
            <span v-if="activeStatus?.has_graph && !activeStatus.vectorized" class="field-hint">请先为当前图谱建立索引。</span>
          </div>
        </form>
        <p v-if="panelError" class="note amber" role="alert">{{ panelError }}</p>
      </div>
    </section>

    <template v-if="result">
      <section class="panel retrieval-summary-panel">
        <div class="panel-head">
          <h2>检索结果</h2>
          <span v-if="atomicOpen" class="badge blue">连通分量 {{ result.connected_components.length }}</span>
        </div>
        <div class="panel-body">
          <section v-if="result.sql_preview" class="sql-preview" aria-label="SQL 草稿">
            <h3>SQL 草稿</h3>
            <small v-if="result.sql_preview.sql">由项目模型生成，未执行。</small>
            <pre v-if="result.sql_preview.sql"><code>{{ result.sql_preview.sql }}</code></pre>
            <p v-else role="status">{{ result.sql_preview.reason || new ApiError(result.sql_preview.code || 'sql_preview_model_failed').message }}</p>
          </section>
          <details v-if="result.agent_context" class="agent-context">
            <summary>查看 Agent 上下文</summary>
            <pre>{{ JSON.stringify(result.agent_context, null, 2) }}</pre>
          </details>
          <p v-if="result.truncated" class="note amber">结果达到读取或路径分析上限，当前展示部分内容。</p>
          <section v-if="result.plan" class="plan-summary" aria-label="分析条件">
            <div class="chips">
              <span v-if="result.plan.business_context && !Object.keys(result.plan.business_context).length" class="badge">通用知识</span>
              <span v-for="(value, key) in result.plan.business_context || {}" :key="key" class="badge blue">{{ key === 'project' ? '项目' : key }} · {{ value }}</span>
              <span v-for="condition in result.plan.conditions" :key="condition.text" class="badge">{{ condition.role ? condition.role + ' · ' : '' }}{{ condition.text }}<template v-if="condition.match === 'semantic'"> → {{ condition.candidates?.[0]?.display_name }}（语义匹配）</template></span>
            </div>
            <p v-if="result.plan.drill_role">下钻维度 · {{ result.plan.drill_role }}<template v-if="result.plan.requested_level">，目标层级 · {{ result.plan.requested_level }}</template></p>
            <p v-if="result.plan.excluded_terms?.length">排除 {{ result.plan.excluded_terms.join('、') }}</p>
            <p v-for="reason in result.plan.unresolved" :key="reason">{{ reason }}</p>
            <details v-if="result.plan.parser_fallback && result.plan.intent === 'clarify'">
              <summary>问句解析方式</summary>
              <p>已使用索引解析。{{ fallbackLabels[result.plan.parser_fallback] }}</p>
            </details>
            <template v-if="result.plan.suggestions?.length && result.available_context?.status !== 'scope_unresolved'">
              <p v-for="item in result.plan.suggestions?.filter(s => s.candidates.some(c => c.display_name))" :key="item.text">
                “{{ item.text }}”{{ item.reason === 'related' ? '的相关候选（尚未确认）' : '的待确认候选' }} · {{ item.candidates.map(c => c.display_name).filter(Boolean).join('、') }}
              </p>
            </template>
            <template v-if="result.overview">
              <div v-if="result.overview.counts">
                <h3>社区概览</h3>
                <p>{{ result.overview.counts.nodes }} 个节点 · {{ result.overview.counts.edges }} 条关系 · {{ result.overview.counts.communities }} 个社区</p>
                <ul><li v-for="community in result.overview.communities || []" :key="community.id">社区 {{ community.id.slice(-10) }} · {{ community.size }} 个节点</li></ul>
              </div>
              <p v-else>当前版本与范围暂无离线分析。可以在业务图谱中计算。</p>
            </template>
            <template v-if="result.dimensional">
              <p v-if="result.dimensional.status !== 'matched'" role="status">{{ result.dimensional.status === 'incomplete' ? '本次检索未能核实全部条件。' : '当前范围内未找到能同时支持这些条件的事实。' }}</p>
              <article v-for="witness in result.dimensional.witnesses" :key="JSON.stringify([witness.fact_id, witness.row_id, witness.edge_ids])" class="fact-witness">
                <strong>{{ nodeLabel(witness.fact_id) }}</strong>
                <span>{{ witness.conditions.map(c => nodeLabel(c.node_id)).join(' · ') }}</span>
                <details><summary>查看共同依据</summary><div>来源行 <code>{{ witness.row_id }}</code></div><div v-for="id in witness.edge_ids" :key="id">{{ edgeMap.get(id)?.relation || id }}</div></details>
              </article>
              <template v-if="result.dimensional.children.length"><h3>有依据的下级节点</h3><ul><li v-for="id in result.dimensional.children" :key="id">{{ nodeLabel(id) }} <span v-if="result.dimensional.level_node_ids?.includes(id)" class="badge green">目标层级</span></li></ul></template>
              <p v-if="result.dimensional.stop_reason === 'requested_level_unreached'">已有事实支持上述条件，尚未找到可证实的目标层级。</p>
              <p v-else-if="result.dimensional.stop_reason === 'search_limit'">已到本次搜索上限，可能还有其他符合条件的结果。</p>
            </template>
          </section>
          <div v-if="pathResult" class="retrieved-paths">
            <h3>{{ result.grouping ? '分组候选路径' : (result.traverse || result.association_paths) ? '关联路径' : '下钻路径' }}</h3>
            <p v-if="(pathResult.equally_ranked_paths || 0) > 1" class="field-hint">本次搜索发现 {{ pathResult.equally_ranked_paths }} 条并列路径，当前展示其中一条。</p>
            <p v-if="!pathResult.paths.length">{{ pathResult.status === 'no_visible_seed' ? '当前范围内没有匹配的起点。' : '在本次搜索范围内未找到满足全部要求的路径。' }}</p>
            <p v-if="pathResult.uncovered_seed_ids.length" class="field-hint">未进入此路径的起点：{{ pathResult.uncovered_seed_ids.map(nodeLabel).join('、') }}</p>
            <article v-for="(path, index) in pathResult.paths" :key="index">
              <ol>
                <li v-for="(id, position) in path.node_ids" :key="position">
                  <span v-if="position" class="path-relation">
                    {{ pathEdges.get(path.edge_ids[position - 1]!)?.target === id ? '→' : '←' }}
                    {{ pathEdges.get(path.edge_ids[position - 1]!)?.relation }}
                  </span>
                  {{ nodeLabel(id) }}
                </li>
              </ol>
              <small>{{ stopLabels[path.stop_reason] || path.stop_reason }} · {{ contextLabel(path.business_context) }}</small>
            </article>
          </div>
          <AvailableContext v-if="result.available_context" :context="result.available_context" />
          <div v-if="showMainGraph" class="report-summary">
            <div><small>子图节点</small><strong>{{ result.nodes.length }}</strong></div>
            <div><small>关系</small><strong>{{ result.edges.length }}</strong></div>
            <div v-if="atomicOpen"><small>种子节点</small><strong>{{ result.nodes.filter(n => n.is_seed).length }}</strong></div>
            <div v-if="atomicOpen"><small>桥接节点</small><strong>{{ result.bridge_nodes.length }}</strong></div>
            <div v-if="atomicOpen"><small>授权域</small><strong>{{ result.scopes.join(', ') }}</strong></div>
          </div>

          <div v-if="atomicOpen && result.slots.length" class="slot-grid">
            <div v-for="slot in result.slots" :key="slot.surface" class="slot-card">
              <h3>{{ slot.surface }}</h3>
              <p v-if="!slot.candidates.length" class="field-hint">无匹配候选</p>
              <ul v-else class="candidate-list">
                <li v-for="cand in slot.candidates.slice(0, 6)" :key="cand.node_id">
                  <span>
                    <strong>{{ nodeMap.get(cand.node_id)?.display_name || cand.node_id }}</strong>
                    <small class="badge">{{ cand.kind }}</small>
                  </span>
                  <small class="mono muted">
                    <template v-if="cand.match_type === 'exact_id'">精确 ID</template>
                    <template v-else-if="cand.rrf_score != null">RRF {{ cand.rrf_score.toFixed(4) }}</template>
                    <template v-if="cand.vec_score != null"> · Vec {{ cand.vec_score.toFixed(3) }}</template>
                    <template v-if="cand.lex_rank != null"> · Lex #{{ cand.lex_rank }}</template>
                  </small>
                </li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      <div v-if="showMainGraph" class="two-fields retrieval-detail-grid">
        <section class="panel">
          <div class="panel-head">
            <h2>召回实体节点 ({{ result.nodes.length }})</h2>
          </div>
          <div class="panel-body">
            <div v-if="result.bridge_nodes.length" class="bridge-box">
              <h3>共同关联对象与路径依据</h3>
              <article v-for="b in result.bridge_nodes" :key="b.node_id">
                <p><strong>{{ b.display_name || b.node_id }}</strong>（{{ b.kind }}）关联：{{ b.matched_slots.join('、') }} <span v-if="b.context_status === 'unverified'" class="badge amber">适用条件未确认</span></p>
                <details v-for="(group, index) in b.path_groups || []" :key="index">
                  <summary>{{ contextLabel(group.business_context) }} · 第 {{ index + 1 }} 组路径</summary>
                  <div v-for="(steps, slot) in group.paths_by_slot" :key="slot">
                    <strong>{{ slot }}</strong>
                    <ol><li v-for="(step, position) in steps" :key="position">
                      {{ nodeLabel(step.source) }} → {{ step.relation }} → {{ nodeLabel(step.target) }}
                      <div v-for="(observation, observationIndex) in step.observations || []" :key="observationIndex">
                        <small>{{ contextLabel(observation.business_context) }}</small>
                        <PropertyTable :values="{ ...observation.properties, '引用': observation.evidence_refs || [], '来源': observation.source_tables || [] }"/>
                      </div>
                    </li></ol>
                  </div>
                </details>
              </article>
            </div>
            <div v-if="!result.nodes.length" class="empty-state"><p>未命中满足授权作用域的节点。</p></div>
            <div v-else class="table-scroll">
              <table>
                <thead><tr><th>实体名称</th><th>类型</th><th>英文名</th><th>标识键</th></tr></thead>
                <tbody>
                  <tr v-for="node in result.nodes" :key="node.id">
                    <td>{{ node.display_name || node.chinese_name || node.id }}<small class="result-node-id mono">{{ node.id }}</small></td>
                    <td><span class="badge">{{ node.kind }}</span></td>
                    <td class="mono">{{ node.english_name || '—' }}</td>
                    <td class="mono">{{ JSON.stringify(node.identity || {}) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>关系 ({{ result.edges.length }})</h2>
          </div>
          <div class="panel-body">
            <div v-if="!result.edges.length" class="empty-state"><p>当前子图没有真实关联边。</p></div>
            <div v-else class="table-scroll">
              <table>
                <thead><tr><th>起点</th><th>关系</th><th>终点</th><th>适用范围与依据</th></tr></thead>
                <tbody>
                  <tr v-for="edge in result.edges" :key="edge.id">
                    <td>{{ nodeMap.get(edge.source)?.display_name || edge.source }}</td>
                    <td><span class="badge blue">{{ edge.relation }}</span></td>
                    <td>{{ nodeMap.get(edge.target)?.display_name || edge.target }}</td>
                    <td><details v-for="(observation, index) in edge.observations || []" :key="index">
                      <summary>{{ contextLabel(observation.business_context) }}</summary>
                      <PropertyTable :values="{ ...observation.properties, '引用': observation.evidence_refs || [], '来源': observation.source_tables || [] }"/>
                    </details></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.atomic-toggle { justify-self: start }
.fact-witness { display: grid; gap: 4px; margin-block: 10px; overflow-wrap: anywhere }
.plan-summary { margin-bottom: 12px }

.retrieval-modes { display: inline-flex; width: fit-content; flex-wrap: wrap; gap: 2px; background: #f0f3f8; border-radius: 8px; padding: 3px }
.retrieval-modes button { padding: 7px 12px; border-radius: 6px; color: #485974 }
.retrieval-modes button[aria-pressed="true"] { color: var(--blue); background: white; box-shadow: 0 1px 4px #25385a18 }
.retrieval-form .field,.retrieval-form .business-context-select { margin: 0 }
.index-status,.retrieval-search-panel .panel-head { flex-wrap: wrap; gap: 8px }
.index-status small { overflow-wrap: anywhere }
.retrieval-options summary { cursor: pointer; font-weight: 500 }
.retrieval-options .retrieval-params { margin-top: 10px }
.retrieved-paths article { padding: 10px 0; border-bottom: 1px solid var(--line) }
.retrieved-paths ol { display: flex; flex-wrap: wrap; gap: .5em; padding: 0; list-style: none }
.path-relation { font-size: 12px; color: #52637a; margin-right: .4em }
.result-node-id { display: block; color: #52637a; user-select: all; overflow-wrap: anywhere }
</style>

<style scoped>
.sql-preview pre, .agent-context pre { max-height: 24rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
