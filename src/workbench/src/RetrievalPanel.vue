<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ensureSession, projectPath, request } from './api'
import BusinessContextSelect from './BusinessContextSelect.vue'
import PropertyTable from './PropertyTable.vue'
import type { BusinessContext, FactContext, EmbeddingConfig, RetrievalStatusResponse } from './types'

type Observation = { business_context?: FactContext; properties?: Record<string, unknown>; evidence_refs?: string[]; source_tables?: string[] }
type PathStep = { source: string; target: string; relation: string; direction?: string; observations?: Observation[] }

interface RetrievalQueryResponse {
  query: string
  scopes: string[]
  slots: Array<{
    surface: string
    role_hint?: string | null
    source: string
    candidates: Array<{
      node_id: string
      kind: string
      rrf_score: number
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
const hops = ref<1 | 2>(2)
const topPSeeds = ref(4)
const working = ref(false)
const panelError = ref('')
const result = ref<RetrievalQueryResponse | null>(null)
let queryEpoch = 0

watch(businessContext, () => { queryEpoch++; result.value = null; working.value = false })
watch(() => props.projectId, () => { queryEpoch++; businessContext.value = null; result.value = null; working.value = false })

const activeStatus = computed(() => props.status)

const activeEngineLabel = computed(() => {
  const emb = props.embedding
  if (!emb || emb.provider !== 'openai_compatible') {
    return '内置本地哈希向量（可在「模型配置 → 向量嵌入服务」切换为 Qwen3-Embedding 等本地模型）'
  }
  return `${emb.model || '未指定模型'} @ ${emb.base_url || '未填地址'}`
})

const nodeMap = computed(() => {
  const map = new Map<string, RetrievalQueryResponse['nodes'][number]>()
  for (const n of result.value?.nodes || []) map.set(n.id, n)
  return map
})
const contextLabel = (context?: FactContext) => !context || context.status === 'unknown' ? '适用范围尚未确认'
  : context.status === 'global' ? '通用知识' : Object.entries(context.dimensions).map(([key, value]) => `${key}：${String(value)}`).join(' · ')
const nodeLabel = (id: string) => nodeMap.value.get(id)?.display_name || id

async function runQuery() {
  const q = queryText.value.trim()
  if (!q || working.value || props.syncing || !props.status?.vectorized) return
  const epoch = ++queryEpoch
  working.value = true
  panelError.value = ''
  try {
    await ensureSession(props.projectId)
    const value = await request<RetrievalQueryResponse>(`${projectPath(props.projectId)}/retrieval/query`, {
      method: 'POST',
      body: JSON.stringify({
        query: q,
        business_context: businessContext.value,
        hops: hops.value,
        top_p_seeds: topPSeeds.value,
      }),
    })
    if (epoch === queryEpoch) result.value = value
  } catch (err) {
    if (epoch === queryEpoch) panelError.value = err instanceof Error ? err.message : '子图召回失败，请重试。'
  } finally {
    if (epoch === queryEpoch) working.value = false
  }
}
</script>

<template>
  <div class="retrieval-page">
    <div class="intro">
      <div>
        <h2>图谱向量化与子图召回</h2>
        <p>当前向量服务：<strong>{{ activeEngineLabel }}</strong></p>
      </div>
      <div class="actions">
        <button
          class="btn"
          :class="{ primary: activeStatus?.status === 'not_vectorized' || activeStatus?.status === 'stale' }"
          :disabled="syncing || working || busy || activeStatus?.status === 'no_graph'"
          @click="emit('vectorize')"
        >
          {{
            syncing
              ? '正在向量化…'
              : activeStatus?.vectorized
                ? '重新向量化'
                : '开始向量化'
          }}
        </button>
      </div>
    </div>

    <!-- 向量化状态卡 -->
    <section v-if="activeStatus" class="panel retrieval-status-card">
      <div class="panel-body vector-status-row">
        <div class="vector-status-info">
          <span
            class="badge"
            :class="
              activeStatus.status === 'vectorized'
                ? 'green'
                : activeStatus.status === 'no_graph'
                  ? ''
                  : 'amber'
            "
          >
            {{
              activeStatus.status === 'vectorized'
                ? '已完成向量化'
                : activeStatus.status === 'stale'
                  ? '图谱或模型已变更 · 待重新向量化'
                  : activeStatus.status === 'not_vectorized'
                    ? '未向量化（抽取已完成，待用户确认执行）'
                    : '尚未生成业务图谱'
            }}
          </span>
          <span class="field-hint">
            <template v-if="activeStatus.status === 'vectorized'">
              模型：<strong>{{ activeStatus.embedder_model || 'local_hash' }}</strong>
              · 维度：<strong>{{ activeStatus.vector_dim ?? '—' }}</strong>
              · 语义卡片：{{ activeStatus.vector_cards ?? 0 }} 张
              <template v-if="activeStatus.synced_at"> · 完成时间：{{ new Date(activeStatus.synced_at).toLocaleString('zh-CN') }}</template>
            </template>
            <template v-else-if="activeStatus.has_graph">
              当前已抽取 {{ activeStatus.graph_nodes }} 个实体、{{ activeStatus.graph_edges }} 条关系。为节省构建时间，抽取后不会自动向量化，可按需点击右侧按钮执行。
            </template>
            <template v-else>
              请先完成图谱抽取后再执行向量化与语义子图召回。
            </template>
          </span>
        </div>
        <button
          v-if="activeStatus.has_graph"
          class="btn small"
          :class="{ primary: !activeStatus.vectorized }"
          :disabled="syncing || working || busy"
          @click="emit('vectorize')"
        >
          {{ syncing ? '正在向量化…' : activeStatus.vectorized ? '重新向量化' : '立即向量化' }}
        </button>
      </div>
    </section>

    <p v-if="panelError" class="note amber" role="alert">{{ panelError }}</p>

    <section class="panel retrieval-search-panel">
      <div class="panel-body">
        <form class="retrieval-form" @submit.prevent="runQuery">
          <div class="field retrieval-query-field">
            <label for="retrieval-query">检索问题或关键词（支持多实体联合查询，如「华东设备采购合同 结算付款单」）</label>
            <input
              id="retrieval-query"
              v-model="queryText"
              :disabled="working || syncing"
              placeholder="输入自然语言问题、实体名称或指标编码"
            />
          </div>
          <BusinessContextSelect v-model="businessContext" :project-id="projectId" :refresh-key="status?.generation" :disabled="working || syncing"/>
          <div class="retrieval-params">
            <div class="field">
              <label for="retrieval-hops">图扩展跳数</label>
              <select id="retrieval-hops" v-model.number="hops" :disabled="working || syncing">
                <option :value="1">1 跳直接邻接</option>
                <option :value="2">2 跳关联桥接</option>
              </select>
            </div>
            <div class="field">
              <label for="retrieval-seeds">每槽位种子数</label>
              <input id="retrieval-seeds" v-model.number="topPSeeds" type="number" min="1" max="20" :disabled="working || syncing" />
            </div>
            <button class="btn primary" type="submit" :disabled="working || syncing || !activeStatus?.vectorized || !queryText.trim()">
              {{ working ? '召回中…' : '执行子图召回' }}
            </button>
          </div>
        </form>
      </div>
    </section>

    <template v-if="result">
      <section class="panel retrieval-summary-panel">
        <div class="panel-head">
          <h2>召回结果总览</h2>
          <span class="badge blue">连通分量 {{ result.connected_components.length }}</span>
        </div>
        <div class="panel-body">
          <p v-if="result.truncated" class="note amber">结果达到读取或路径分析上限，当前展示部分内容。</p>
          <p class="field-hint">连通分量表示事实之间存在邻接。推理依据按适用条件分别列出，不把邻接并集当成一条完整推导。</p>
          <div class="report-summary">
            <div><small>子图节点</small><strong>{{ result.nodes.length }}</strong></div>
            <div><small>真实有向边</small><strong>{{ result.edges.length }}</strong></div>
            <div><small>种子节点</small><strong>{{ result.nodes.filter(n => n.is_seed).length }}</strong></div>
            <div><small>公共桥接节点</small><strong>{{ result.bridge_nodes.length }}</strong></div>
            <div><small>授权域</small><strong>{{ result.scopes.join(', ') }}</strong></div>
          </div>

          <div v-if="result.slots.length" class="slot-grid">
            <div v-for="slot in result.slots" :key="slot.surface" class="slot-card">
              <h3>槽位：{{ slot.surface }}</h3>
              <p v-if="!slot.candidates.length" class="field-hint">无匹配候选</p>
              <ul v-else class="candidate-list">
                <li v-for="cand in slot.candidates.slice(0, 6)" :key="cand.node_id">
                  <span>
                    <strong>{{ nodeMap.get(cand.node_id)?.display_name || cand.node_id }}</strong>
                    <small class="badge">{{ cand.kind }}</small>
                  </span>
                  <small class="mono muted">
                    RRF {{ cand.rrf_score.toFixed(4) }}
                    <template v-if="cand.vec_score !== null"> · Vec {{ cand.vec_score.toFixed(3) }}</template>
                    <template v-if="cand.lex_rank !== null"> · Lex #{{ cand.lex_rank }}</template>
                  </small>
                </li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      <div class="two-fields retrieval-detail-grid">
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
                    <td>{{ node.display_name || node.chinese_name || node.id }}</td>
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
            <h2>真实关系边 ({{ result.edges.length }})</h2>
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
