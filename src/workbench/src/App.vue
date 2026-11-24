<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import type { Ref } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import GraphAnalysis from './GraphAnalysis.vue'
import GraphHistory from './GraphHistory.vue'
import StructurePreview from './StructurePreview.vue'
import RunIssues from './RunIssues.vue'
import ConstructionReport from './ConstructionReport.vue'
import TechnicalDetails from './TechnicalDetails.vue'
import RunProgress from './RunProgress.vue'
import RunTrace from './RunTrace.vue'
import { effectiveInputTokenLimit, modelLimitError, modelLimitKeys, modelLimitDefaults, runSecondsDefault } from './modelLimits'
import { configTextChanged, mergeConfigDraft } from './configDraft'
import type { ModelLimitKey } from './modelLimits'
import { resourceFields, resourceInputs, resourceInputsError, resourceValues } from './resourceLimits'
import type { ResourceKey } from './resourceLimits'
import IdentityCandidates from './IdentityCandidates.vue'
import DiscoveryCandidates from './DiscoveryCandidates.vue'
import DatabaseSourceDialog from './DatabaseSourceDialog.vue'
import DataInputs from './DataInputs.vue'
import DocumentLibrary from './DocumentLibrary.vue'
import WorkflowEditor from './WorkflowEditor.vue'
import BusinessContextSelect from './BusinessContextSelect.vue'
import GraphAlignmentReview from './GraphAlignmentReview.vue'
import type { BusinessContext } from './types'
import RetrievalPanel from './RetrievalPanel.vue'
import VectorizationProgress from './VectorizationProgress.vue'
import RetrievalIndexControls from './RetrievalIndexControls.vue'
import { useVectorization } from './useVectorization'
import ConfirmDelete from './ConfirmDelete.vue'
import { goalMarkdown } from './goalMarkdown'
import { documentManifestBody } from './documentManifest'
import { configurationTabs, resultTabs, isResultTab } from './onboarding'
import type { ConfigurationTab, ResultTab, WorkbenchTab } from './onboarding'
import { ApiError, ensureSession, onSessionRenewed, clearSessions, clearSession, json, projectPath, request, responseRevision } from './api'
import { assertNoCredentials, graphOutcome, isActive, runLabels, stageLabel, workflowFailureLabel } from './state'
import { WorkbenchLocation } from './workbenchLocation'
import { readGraph } from './graphRead'
import type { DocumentScope, EmbeddingConfig, Graph, InheritedGraphRef, Metadata, Project, ProjectFile, RetrievalStatusResponse, Run, RunConfig, SourceConfig, WorkflowConfig, WorkflowMergeNodeState } from './types'


const projects = ref<Project[]>([])
const project = ref<Project | null>(null)
const config = ref<RunConfig>({})
const configRevision = ref<string | undefined>()
const configurationConflict = ref(false)
const includeCandidates = ref(false)
const tab = ref<WorkbenchTab>('inputs')
const configurationTab = ref<ConfigurationTab>('inputs'), resultTab = ref<ResultTab>('runs')
const inResults = computed(() => isResultTab(tab.value))
const tabs = computed(() => inResults.value ? resultTabs : configurationTabs)
watch(tab, value => { if (isResultTab(value)) resultTab.value = value; else configurationTab.value = value })
function phaseKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const results = event.key === 'End' || (event.key !== 'Home' && !inResults.value)
  tab.value = results ? resultTab.value : configurationTab.value
  ;(event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('[role=tab]')[results ? 1 : 0]?.focus()
}
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const toast = ref('')
const purpose = ref('')
const runSeconds = ref<number | null>(runSecondsDefault)
const maxDbQueries = ref<number | null>(null)
const queryCountUnlimited = ref(true)
const constructionTarget = ref<'instances' | 'structure'>('instances')
const effectiveQueryLimit = computed(() => queryCountUnlimited.value ? null : maxDbQueries.value)
const queryLimitError = computed(() => !queryCountUnlimited.value && (maxDbQueries.value === null || !Number.isSafeInteger(maxDbQueries.value) || maxDbQueries.value <= 0) ? '查询次数上限须为正整数，或勾选不限次数。' : '')
const resources = ref(resourceInputs({}))
const resourceError = computed(() => resourceInputsError(resources.value))
const resourceChanges = computed(() => {
  const current = resourceValues(resources.value), saved = resourceValues(resourceInputs(config.value))
  return Object.fromEntries(resourceFields.filter(field => current[field.key] !== saved[field.key])
    .map(field => [field.key, current[field.key]])) as Partial<RunConfig>
})
function setResourceLimit(key: ResourceKey, unit: number, event: Event) {
  const text = (event.target as HTMLInputElement).value
  resources.value[key].value = text === '' ? null : Number(text) * unit
}
const traceRecording = ref(true)
const model = ref<NonNullable<RunConfig['model']>>({})
const internalHttpEndpoint = computed(() => {
  try {
    const url = new URL(String(model.value.base_url || '').trim())
    return url.protocol === 'http:' && !['127.0.0.1', 'localhost', '::1', '[::1]'].includes(url.hostname)
  } catch { return false }
})
const defaultEmbeddingTimeout = 120
const embedding = ref<EmbeddingConfig>({})
const embeddingKey = ref('')
const embeddingCredStatus = ref<{ embedding: boolean; embedding_error?: string; embedding_environment?: boolean }>({ embedding: false })
const embeddingTesting = ref(false)
const embeddingTestResult = ref<{ status: string; provider: string; base_url: string; model: string; dimensions: number; latency_ms: number } | null>(null)
const internalEmbeddingHttp = computed(() => {
  try {
    const url = new URL(String(embedding.value.base_url || '').trim())
    return url.protocol === 'http:' && !['127.0.0.1', 'localhost', '::1', '[::1]'].includes(url.hostname)
  } catch { return false }
})
const workflowConfig = ref<WorkflowConfig | null>(null)
const inheritedGraphs = ref<InheritedGraphRef[]>([])
const retrievalStatus = ref<RetrievalStatusResponse | null>(null)
const { running: vectorizing, job: vectorJob, start: startVectorization, error: vectorError } = useVectorization(
  () => ['business', 'retrieval'].includes(tab.value) ? project.value?.project_id : undefined,
  () => { void refreshRetrievalStatus() },
)
const agentLimits = ref<Record<string, unknown>>({}), agentLimitsSaved = ref('{}'), agentLimitsLoading = ref(false)
const agentLimitsDirty = computed(() => !!config.value.agent_config && JSON.stringify(agentLimits.value) !== agentLimitsSaved.value)
const limitModel = computed(() => config.value.agent_config ? agentLimits.value : model.value)
const limitsError = computed(() => !traceRecording.value ? '运行需要保存轨迹，请先启用后保存。' : runSeconds.value === null || !Number.isFinite(runSeconds.value) || runSeconds.value <= 0 ? '整次运行时限须大于 0。' : queryLimitError.value || resourceError.value || modelLimitError(limitModel.value))
const effectiveInputTokens = computed(() => effectiveInputTokenLimit(limitModel.value))
function useDefaultLimits() {
  const defaults = { ...modelLimitDefaults, context_window_tokens: null }
  if (config.value.agent_config) agentLimits.value = { ...agentLimits.value, ...defaults }
  else model.value = { ...model.value, ...defaults }
  runSeconds.value = runSecondsDefault
}
function setModelLimit(key: ModelLimitKey, event: Event) {
  const text = (event.target as HTMLInputElement).value
  const value = text === '' ? null : Number(text)
  if (config.value.agent_config) agentLimits.value = { ...agentLimits.value, [key]: value }
  else model.value = { ...model.value, [key]: value }
}
let agentLimitsEpoch = 0
let loadedAgentLimitsKey = ''
watch([() => project.value?.project_id, () => config.value.agent_config], async () => {
  const id = project.value?.project_id, file = config.value.agent_config
  const key = JSON.stringify([id, file])
  if (id && file && key === loadedAgentLimitsKey) return
  const requestEpoch = ++agentLimitsEpoch
  loadedAgentLimitsKey = ''
  agentLimits.value = {}; agentLimitsSaved.value = '{}'
  if (!id || !file) { agentLimitsLoading.value = false; return }
  agentLimitsLoading.value = true
  try {
    const value = await request<Record<string, unknown>>(`${projectPath(id)}/model-limits`)
    if (requestEpoch === agentLimitsEpoch && project.value?.project_id === id && config.value.agent_config === file) { agentLimits.value = value; agentLimitsSaved.value = JSON.stringify(value); loadedAgentLimitsKey = key }
  } catch (exception) { if (requestEpoch === agentLimitsEpoch && project.value?.project_id === id) report(exception) }
  finally { if (requestEpoch === agentLimitsEpoch && project.value?.project_id === id) agentLimitsLoading.value = false }
})
async function saveModelLimits() {
  if (limitsError.value) throw new Error(limitsError.value)
  if (!config.value.agent_config) { await saveForms(); notify('模型配置已保存，下次启动立即生效'); return }
  if (advancedDirty.value && advancedConfig().agent_config !== config.value.agent_config) throw new Error('完整 JSON 更换了独立模型文件，请先保存文件选择，再调整其模型额度。')
  const value = Object.fromEntries(modelLimitKeys.filter(key => key in agentLimits.value).map(key => [key, agentLimits.value[key]]))
  // Save all edited form fields before refreshing the versioned Agent reference.
  // Otherwise a refresh can erase pending guidance or vocabulary changes.
  if (formDirty.value || modelKey.value || embeddingKey.value || advancedDirty.value) await saveForms()
  // The saved Agent path, limits and revision must come from the same write.
  // A second GET could fail after a successful save or read another tab's edit.
  const saved = await request<{ config: RunConfig; limits: Record<string, unknown> }>(`${path.value}/model-limits?include_config=true`, { method: 'PUT', body: json(value), headers: revisionHeaders() })
  configRevision.value = responseRevision(saved) || configRevision.value
  agentLimitsEpoch++
  loadedAgentLimitsKey = JSON.stringify([project.value?.project_id, saved.config.agent_config])
  agentLimits.value = saved.limits; agentLimitsSaved.value = JSON.stringify(saved.limits); agentLimitsLoading.value = false
  fillForms(saved.config)
  notify('模型配置已保存，下次启动立即生效')
}
const advanced = ref('')
const advancedBaseline = ref('')
const modelKey = ref('')
type CredentialStatus = { database_sources: string[]; database_errors?: Record<string, string>; model: boolean; model_error?: string; model_environment?: boolean }
const credentials = ref<CredentialStatus>({ database_sources: [], model: false })
const metadata = shallowRef<Metadata>({ tables: [], nodes: [], edges: [] })
const business = shallowRef<Graph>({ nodes: [], edges: [] })
const selectedGraphVersion = ref('')
const graphBusinessContext = ref<BusinessContext | null>(null)
async function selectGraphVersion(version: string) {
  selectedGraphVersion.value = version
  business.value = { nodes: [], edges: [] }; structurePreviewOpen.value = false
  await loadBusinessGraph()
}
const graphOptionsOpen = ref(false)
function toggleGraphOptions(event: Event) { graphOptionsOpen.value = (event.currentTarget as HTMLDetailsElement).open }
const structurePreviewOpen = ref(false)
const graphLoading = ref(false), graphError = ref('')
let graphRequestEpoch = 0
let graphController: AbortController | undefined
const runs = shallowRef<Run[]>([])
const runMode = ref<'continue' | 'fresh'>('continue')
const selectedRunId = ref('')
const selectedRun = shallowRef<Run | null>(null)
const runDetailError = ref('')
let detailController: AbortController | undefined
let pollController: AbortController | undefined
const historyPage = ref(0)
const historyPageSize = 50
const historyPages = computed(() => Math.ceil(runs.value.length / historyPageSize))
const historyRows = computed(() => runs.value.slice(historyPage.value * historyPageSize, (historyPage.value + 1) * historyPageSize))
watch(selectedRunId, id => {
  if (!id || !project.value) return
  workbenchLocation.selectRun(project.value.project_id, id)
  const index = runs.value.findIndex(run => run.run_id === id)
  if (index >= 0) historyPage.value = Math.floor(index / historyPageSize)
})
watch(historyPages, count => { historyPage.value = Math.max(0, Math.min(historyPage.value, count - 1)) })
const visibleWorkflowState = computed(() => selectedRun.value?.workflow_state)
function workflowRelationSummary(node: WorkflowMergeNodeState) {
  const counts = node.delta_merge_counts ?? node.merge_counts
  const lines = []
  if (counts?.input_edges != null && counts.output_edges != null) {
    const stage = node.delta_merge_counts ? '补充抽取后合并' : '合并'
    lines.push(`${stage}关系记录 ${counts.input_edges} → ${counts.output_edges} 条`)
  }
  if (counts?.output_directed_endpoint_pairs != null && counts.output_multi_edge_pairs != null &&
      counts.output_endpoint_pair_excess != null && counts.output_self_loop_edges != null) {
    lines.push(`有向端点对 ${counts.output_directed_endpoint_pairs} 组，其中 ${counts.output_multi_edge_pairs} 组有多条关系`,
      `每对端点只计一次，记录数相差 ${counts.output_endpoint_pair_excess} 条。自环 ${counts.output_self_loop_edges} 条`)
  } else if (node.status === 'completed') {
    lines.push('此运行未记录有向端点统计')
  }
  return lines
}
const traceNodeId = ref('')
function nodeTraceTarget(nodeId: string) {
  const state = visibleWorkflowState.value
  if (!state || state.recomposed_nodes?.includes(nodeId)) return null
  const unit = state.units?.[nodeId], merge = state.merge_nodes?.[nodeId]
  if (unit && merge || merge && merge.merge_mode !== 'merge_and_extract') return null
  const row = unit ?? merge
  if (!row?.run_id || row.run_id === selectedRun.value?.run_id || (unit?.unit_id ?? merge?.node_id ?? nodeId) !== nodeId) return null
  return { nodeId, runId: row.run_id, status: row.status }
}
const traceNode = computed(() => nodeTraceTarget(traceNodeId.value))
function toggleNodeTrace(nodeId: string) {
  traceNodeId.value = traceNodeId.value === nodeId ? '' : nodeId
}
const canEditWorkflowRun = computed(() => Boolean(config.value.workflow?.enabled && selectedRun.value &&
  selectedRun.value.current_workflow_run_id === selectedRun.value.run_id))
const workflowOutputLabel = computed(() => {
  const merge = visibleWorkflowState.value?.merge
  const status = merge?.status
  const coverage = (merge?.conflict_isolation as { coverage?: string } | undefined)?.coverage
  if (merge?.publication_status === 'published' && coverage === 'incomplete') return '已发布，覆盖有缺口'
  if (merge?.publication_status === 'retained_previous') return '未发布新图，仍显示上次结果'
  if (merge?.publication_status === 'blocked') return '本次未生成项目输出，可在历史版本查看已提交的节点图'
  if (status === 'direct_output') return '抽取节点直接输出'
  if (status === 'direct_multi_output') return '各抽取分支独立输出'
  return merge?.publication_status === 'published' ? '已发布' : status || '待运行'
})
const runPollError = ref<string | null>(null)
const newProjectOpen = ref(false), newName = ref(''), newId = ref('')
const databaseOpen = ref(false), databaseSource = ref<SourceConfig | null>(null)
const databaseDialog = ref<{ completeDraft: () => Promise<void> } | null>(null)
const workbenchLocation = new WorkbenchLocation()
const uploadInput = ref<HTMLInputElement | null>(null)
type DeletionTarget = { kind: 'run'; run: Run } | { kind: 'project'; id: string; name: string } | { kind: 'file'; file: ProjectFile } | { kind: 'source'; source: SourceConfig } | { kind: 'table'; source: SourceConfig; table: string }
const deletion = ref<DeletionTarget | null>(null), deletionError = ref('')
const preview = ref<{ name: string; content: string } | null>(null)
let generation = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
let toastTimer: ReturnType<typeof setTimeout> | undefined
const activeRun = computed(() => runs.value.find(isActive))
const locked = computed(() => busy.value || loading.value || !!activeRun.value)
const path = computed(() => projectPath(project.value?.project_id || ''))
const queryLimitDirty = computed(() => effectiveQueryLimit.value !== (config.value.max_db_queries ?? null) || queryCountUnlimited.value !== (config.value.max_db_queries == null))
const embeddingDirty = computed(() => JSON.stringify(embedding.value) !== JSON.stringify(config.value.embedding || {}))
const workflowDirty = computed(() => JSON.stringify(workflowConfig.value) !== JSON.stringify(config.value.workflow ?? null))
const inheritedDirty = computed(() => JSON.stringify(inheritedGraphs.value) !== JSON.stringify(config.value.inherited_graphs || []))
const formDirty = computed(() => traceRecording.value !== (config.value.record_trace !== false) || runSeconds.value !== (config.value.max_run_seconds ?? runSecondsDefault) || queryLimitDirty.value || Object.keys(resourceChanges.value).length > 0 || constructionTarget.value !== (config.value.construction_target ?? 'instances') || purpose.value !== goalMarkdown(config.value) || JSON.stringify(model.value) !== JSON.stringify(config.value.model || {}) || embeddingDirty.value || workflowDirty.value || inheritedDirty.value)
const dirty = computed(() => !!modelKey.value || !!embeddingKey.value || formDirty.value || agentLimitsDirty.value)
const advancedDirty = computed(() => configTextChanged(advanced.value, advancedBaseline.value))
function previewConfig(): RunConfig {
  const value = { ...config.value, ...resourceChanges.value }
  if (purpose.value !== goalMarkdown(config.value)) value.goal_markdown = purpose.value
  if (runSeconds.value !== (config.value.max_run_seconds ?? runSecondsDefault)) value.max_run_seconds = runSeconds.value!
  if (effectiveQueryLimit.value !== (config.value.max_db_queries ?? null)) value.max_db_queries = effectiveQueryLimit.value
  if (constructionTarget.value !== (config.value.construction_target ?? 'instances')) value.construction_target = constructionTarget.value
  if (traceRecording.value !== (config.value.record_trace !== false)) value.record_trace = traceRecording.value
  if (!value.agent_config && JSON.stringify(model.value) !== JSON.stringify(config.value.model || {})) value.model = { ...model.value }
  if (embeddingDirty.value || 'embedding' in config.value) value.embedding = JSON.parse(JSON.stringify(embedding.value)) as EmbeddingConfig
  if (workflowDirty.value || 'workflow' in config.value) value.workflow = workflowConfig.value ? JSON.parse(JSON.stringify(workflowConfig.value)) as WorkflowConfig : null
  if (inheritedDirty.value || 'inherited_graphs' in config.value) value.inherited_graphs = JSON.parse(JSON.stringify(inheritedGraphs.value)) as InheritedGraphRef[]
  return value
}
function resetAdvanced() { advanced.value = advancedBaseline.value = JSON.stringify(previewConfig(), null, 2) }
watch([purpose, runSeconds, effectiveQueryLimit, resources, constructionTarget, traceRecording, model, embedding, workflowConfig, inheritedGraphs], () => {
  // The JSON view follows the form until the user edits the JSON itself.
  if (!advancedDirty.value) resetAdvanced()
}, { deep: true })
const deletionTitle = computed(() => deletion.value?.kind === 'run' ? '删除运行记录' : deletion.value?.kind === 'project' ? '删除项目' : deletion.value?.kind === 'source' ? '删除数据库来源' : deletion.value?.kind === 'table' ? '移除数据表' : '删除文件')
const deletionDescription = computed(() => {
  const target = deletion.value
  if (!target) return ''
  if (target.kind === 'run') return `删除运行 ${target.run.run_id} 的报告和直属日志？并行节点产物、检查点和已发布图谱保留。删除后无法从这条记录恢复运行。`
  if (target.kind === 'project') return `删除项目“${target.name}”及其文件、配置、运行记录和图谱结果？此操作无法撤销。`
  if (target.kind === 'file') return `删除项目中的“${target.file.name}”及其相关配置和元数据？已有运行记录与图谱结果保留。`
  if (target.kind === 'source') return `移除来源“${target.source.id}”、本机保存的连接信息及相关配置和元数据？外部数据库和已有图谱结果保留。`
  return `从来源“${target.source.id}”移除“${target.table}”？外部数据表和已有图谱结果保留。`
})
const deletionDetail = computed(() => deletion.value?.kind === 'project' && (dirty.value || advancedDirty.value) ? '此项目尚未保存的修改也会被删除。' : deletion.value?.kind === 'source' || deletion.value?.kind === 'table' || deletion.value?.kind === 'file' && ['excel', 'csv'].includes(deletion.value.file.kind) ? '失去绑定范围的文档会暂停参与抽取，可重新绑定。' : '')
const documentFiles = computed(() => project.value?.files.filter(f => f.kind === 'markdown') || [])
const time = (value?: string) => value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' }).format(new Date(value)) : '—'
const tableKey = (table: Metadata['tables'][number]) => `${table.source_id}.${table.schema}.${table.name}`
const documentTableKeys = computed(() => {
  const keys = new Set<string>()
  for (const source of config.value.sources || []) {
    if (Array.isArray(source.allowed_tables) && source.allowed_tables.length) {
      for (const name of source.allowed_tables) keys.add(`${source.id}.${name}`)
    } else {
      for (const table of metadata.value.tables) if (table.source_id === source.id) keys.add(tableKey(table))
      if (source.kind === 'csv_file' && source.table_name) keys.add(`${source.id}.${source.schema_name || 'public'}.${source.table_name}`)
    }
  }
  return [...keys].sort()
})
function notify(message: string) { toast.value = message; clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.value = '' }, 4500) }
function report(exception: unknown) { if (exception instanceof ApiError && ['config_revision_conflict', 'model_credential_binding_conflict'].includes(exception.code)) configurationConflict.value = true; error.value = exception instanceof Error ? exception.message : '操作未完成，请重试。' }
function fillForms(value: RunConfig) {
  configRevision.value = responseRevision(value) || configRevision.value
  configurationConflict.value = false
  config.value = value
  purpose.value = goalMarkdown(value)
  runSeconds.value = value.max_run_seconds ?? runSecondsDefault
  queryCountUnlimited.value = value.max_db_queries == null
  maxDbQueries.value = value.max_db_queries ?? null
  resources.value = resourceInputs(value)
  constructionTarget.value = value.construction_target ?? 'instances'
  traceRecording.value = value.record_trace !== false
  model.value = JSON.parse(JSON.stringify(value.model || {}))
  embedding.value = JSON.parse(JSON.stringify(value.embedding || {}))
  workflowConfig.value = value.workflow ? JSON.parse(JSON.stringify(value.workflow)) : null
  inheritedGraphs.value = JSON.parse(JSON.stringify(value.inherited_graphs || []))
  advanced.value = advancedBaseline.value = JSON.stringify(value, null, 2)
}
function formConfig(): RunConfig {
  const problem = limitsError.value
  if (problem) throw new Error(problem)
  const value: RunConfig = { ...config.value, ...resourceChanges.value, record_trace: traceRecording.value, max_run_seconds: runSeconds.value!, max_db_queries: effectiveQueryLimit.value, construction_target: constructionTarget.value, goal_markdown: purpose.value }
  if (!value.agent_config) value.model = { ...model.value }
  if (embeddingDirty.value || 'embedding' in config.value) value.embedding = JSON.parse(JSON.stringify(embedding.value)) as EmbeddingConfig
  if (workflowDirty.value || 'workflow' in config.value) value.workflow = workflowConfig.value ? JSON.parse(JSON.stringify(workflowConfig.value)) as WorkflowConfig : null
  if (inheritedDirty.value || 'inherited_graphs' in config.value) value.inherited_graphs = JSON.parse(JSON.stringify(inheritedGraphs.value)) as InheritedGraphRef[]
  assertNoCredentials(value)
  return value
}
async function action(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await fn() } catch (exception) { report(exception) } finally { busy.value = false }
}
async function reloadProjects(select?: string) {
  const response = await request<{ projects: Project[] }>('/projects')
  projects.value = response.projects
  if (!select && project.value && response.projects.some(item => item.project_id === project.value?.project_id)) return
  const next = select || workbenchLocation.preferredProject(response.projects)
  if (next) await loadProject(next)
  else { project.value = null; databaseOpen.value = false; loading.value = false }
}
// Load each tab's inputs on demand. Keep project navigation and run reports usable
// while an input view is waiting for its metadata or saved connection status.
function useProjectInput<T>(tabs: WorkbenchTab[], target: Ref<T>, read: (id: string, signal: AbortSignal) => Promise<T>, revision = ref(0)) {
  const pending = ref(false)
  let loadedGeneration: number | undefined, loadedRevision: number | undefined
  watch([() => !loading.value && tabs.includes(tab.value) ? project.value?.project_id : undefined, configRevision, revision], async ([id, , version], _, onCleanup) => {
    pending.value = false
    if (!id || (loadedGeneration === generation && loadedRevision === version)) return
    const epoch = generation, controller = new AbortController()
    onCleanup(() => controller.abort())
    pending.value = loadedGeneration !== generation
    const previous = target.value
    try {
      const value = await read(id, controller.signal)
      if (!controller.signal.aborted && epoch === generation) {
        if (target.value === previous) target.value = value
        loadedGeneration = epoch
        loadedRevision = version
      }
    } catch (exception) {
      if (!controller.signal.aborted && epoch === generation) report(exception)
    } finally { if (!controller.signal.aborted && epoch === generation) pending.value = false }
  })
  return pending
}
const metadataPending = useProjectInput(['inputs', 'documents', 'goal', 'workflow', 'metadata'], metadata,
  (id, signal) => request<Metadata>(`${projectPath(id)}/metadata`, { signal }))
const credentialSessionRevision = ref(0)
const stopSessionUpdates = onSessionRenewed(id => {
  if (project.value?.project_id === id) {
    credentials.value = { database_sources: [], model: false }
    credentialSessionRevision.value++
  }
})
onUnmounted(stopSessionUpdates)
const credentialsPending = useProjectInput(['inputs', 'settings'], credentials, async (id, signal) => {
  await ensureSession(id)
  signal.throwIfAborted()
  return request<CredentialStatus>(`${projectPath(id)}/credentials`, { signal })
}, credentialSessionRevision)
async function loadProject(id: string) {
  if (project.value?.project_id === id && !loading.value) return
  if (project.value && project.value.project_id !== id && (dirty.value || advancedDirty.value) && !window.confirm('当前项目有未保存的配置修改。切换项目会放弃这些修改，继续吗？')) return
  runMode.value = 'continue'; selectedGraphVersion.value = ''; graphBusinessContext.value = null
  workbenchLocation.selectProject(id)
  const epoch = ++generation
  graphController?.abort(); graphOptionsOpen.value = false
  embeddingTesting.value = false
  clearTimeout(pollTimer); loading.value = true; error.value = ''; selectedRunId.value = ''; pollController?.abort(); historyPage.value = 0; runPollError.value = null; preview.value = null; databaseOpen.value = false; modelKey.value = ''; embeddingKey.value = ''; embeddingCredStatus.value = { embedding: false }; embeddingTestResult.value = null; retrievalStatus.value = null; credentials.value = { database_sources: [], model: false }
  const p = projectPath(id)
  try {
    const values = await Promise.all([request<Project>(p), request<RunConfig>(`${p}/config`)])
    if (epoch !== generation) return
    configRevision.value = undefined; project.value = values[0]; fillForms(values[1]); metadata.value = { tables: [], nodes: [], edges: [] }; business.value = { nodes: [], edges: [] }; runs.value = []
    if (tab.value === 'business') void loadBusinessGraph(p, epoch)
    try {
      const list = await request<{ runs: Run[] }>(`${p}/runs?view=summary`)
      if (epoch !== generation) return
      runs.value = list.runs.map(historyRow)
    } catch (exception) { if (epoch === generation) report(exception) }
    if (epoch !== generation) return
    const previousRun = workbenchLocation.lastRun(id)
    selectedRunId.value = (runs.value.find(run => run.run_id === previousRun) || runs.value[0])?.run_id || ''
    schedulePoll(epoch)
    const restored = workbenchLocation.restoreDatabase(id, config.value.sources || [])
    if (restored && !activeRun.value) {
      tab.value = 'inputs'; databaseSource.value = restored.source; databaseOpen.value = true
    }
  } catch (exception) { if (epoch === generation) { project.value = null; report(exception) } }
  finally { if (epoch === generation) loading.value = false }
}
function applyProjectConfig(nextProject: Project, nextConfig: RunConfig, preserveEdits = false) {
  // Preserve only edited fields; unchanged controls must follow server-side dependency cleanup.
  const edits = preserveEdits ? {
    traceRecording: traceRecording.value !== (config.value.record_trace !== false) ? traceRecording.value : undefined,
    runSeconds: runSeconds.value !== (config.value.max_run_seconds ?? runSecondsDefault) ? runSeconds.value : undefined,
    maxDbQueries: queryLimitDirty.value ? { value: effectiveQueryLimit.value, unlimited: queryCountUnlimited.value } : undefined,
    resources: Object.fromEntries(resourceFields.filter(field => field.key in resourceChanges.value)
      .map(field => [field.key, { ...resources.value[field.key] }])),
    constructionTarget: constructionTarget.value !== (config.value.construction_target ?? 'instances') ? constructionTarget.value : undefined,
    purpose: purpose.value !== goalMarkdown(config.value) ? purpose.value : undefined,
    model: JSON.stringify(model.value) !== JSON.stringify(config.value.model || {}) ? JSON.parse(JSON.stringify(model.value)) as typeof model.value : undefined,
    embedding: embeddingDirty.value ? JSON.parse(JSON.stringify(embedding.value)) as EmbeddingConfig : undefined,
    workflowConfig: workflowDirty.value ? JSON.parse(JSON.stringify(workflowConfig.value)) as WorkflowConfig | null : undefined,
    inheritedGraphs: inheritedDirty.value ? JSON.parse(JSON.stringify(inheritedGraphs.value)) as InheritedGraphRef[] : undefined,
  } : null
  const draft = preserveEdits && advancedDirty.value ? advanced.value : undefined
  const draftBaseline = advancedBaseline.value
  const draftRevision = configRevision.value
  const agentConfigRemoved = preserveEdits && !!config.value.agent_config && !nextConfig.agent_config
  project.value = nextProject; fillForms(nextConfig)
  if (draft !== undefined) { advanced.value = draft; advancedBaseline.value = draftBaseline; configRevision.value = draftRevision }
  if (agentConfigRemoved) modelKey.value = ''
  if (edits) {
    if (edits.traceRecording !== undefined) traceRecording.value = edits.traceRecording
    if (edits.runSeconds !== undefined) runSeconds.value = edits.runSeconds
    if (edits.maxDbQueries !== undefined) { maxDbQueries.value = edits.maxDbQueries.value; queryCountUnlimited.value = edits.maxDbQueries.unlimited }
    Object.assign(resources.value, edits.resources)
    if (edits.constructionTarget !== undefined) constructionTarget.value = edits.constructionTarget
    if (edits.purpose !== undefined) purpose.value = edits.purpose
    if (edits.model !== undefined && !agentConfigRemoved) model.value = edits.model
    if (edits.embedding !== undefined) embedding.value = edits.embedding
    if (edits.workflowConfig !== undefined) workflowConfig.value = edits.workflowConfig
    if (edits.inheritedGraphs !== undefined) inheritedGraphs.value = edits.inheritedGraphs
  }
  projects.value = projects.value.map(p => p.project_id === nextProject.project_id ? nextProject : p)
}
async function refreshProjectConfig(preserveEdits = false) {
  const values = await Promise.all([request<Project>(path.value), request<RunConfig>(`${path.value}/config`)])
  applyProjectConfig(values[0], values[1], preserveEdits)
}
async function refreshGraphs() {
  const p = path.value, epoch = generation
  // Each view commits independently. A failed metadata refresh must not
  // discard a successful read of the already published business graph.
  const values = await Promise.allSettled([request<Metadata>(`${p}/metadata`), loadBusinessGraph(p, epoch)])
  if (epoch !== generation) return
  if (values[0].status === 'fulfilled') metadata.value = values[0].value
  else throw values[0].reason
}
async function refreshAfterIdentityDecision() {
  await Promise.all([refreshGraphs(), refreshRetrievalStatus()])
}
async function loadBusinessGraph(p = path.value, epoch = generation) {
  if (tab.value !== 'business' || !project.value) return
  const requestEpoch = ++graphRequestEpoch
  graphController?.abort()
  const controller = new AbortController(); graphController = controller
  graphLoading.value = true; graphError.value = ''
  try {
    const graph = await readGraph<Graph>(`${p}/graph?layer=business&include_candidates=${includeCandidates.value}&limit=240${graphBusinessContext.value !== null ? `&business_context=${encodeURIComponent(JSON.stringify(graphBusinessContext.value))}` : ''}${selectedGraphVersion.value ? `&version=${encodeURIComponent(selectedGraphVersion.value)}` : ''}`, controller.signal)
    if (epoch === generation && requestEpoch === graphRequestEpoch) business.value = graph
  } catch (exception) {
    if (epoch === generation && requestEpoch === graphRequestEpoch) {
      // A previous view cannot stand in for a currently denied or invalid snapshot.
      if (exception instanceof ApiError && exception.status >= 400 && exception.status < 500) business.value = { nodes: [], edges: [] }
      graphError.value = exception instanceof Error ? exception.message : '图谱读取失败，请重试。'
    }
  } finally { if (epoch === generation && requestEpoch === graphRequestEpoch) graphLoading.value = false }
}
// History rows must stay small even when a poll or start action returns a full run.
function historyRow(run: Run): Run {
  const { project_id, run_id, status, created_at, cleanup_status, workflow } = run
  return { project_id, run_id, status, created_at, cleanup_status, workflow, stage: run.progress?.stage || run.stage }
}
function updateHistory(run: Run) {
  const index = runs.value.findIndex(item => item.run_id === run.run_id)
  if (index < 0) return
  const before = runs.value[index]
  const next = historyRow(run)
  if (before.status === next.status && before.stage === next.stage && before.cleanup_status === next.cleanup_status) return
  const rows = runs.value.slice()
  rows[index] = next
  runs.value = rows
}
async function readSelectedRun() {
  const id = selectedRunId.value
  if (!id || !project.value || tab.value !== 'runs' || detailController) return
  const controller = new AbortController()
  detailController = controller
  const epoch = generation
  try {
    const run = await request<Run>(`${path.value}/runs/${encodeURIComponent(id)}?view=workbench`, { signal: controller.signal })
    if (controller.signal.aborted || epoch !== generation || selectedRunId.value !== id || tab.value !== 'runs') return
    selectedRun.value = run
    runDetailError.value = ''
    runPollError.value = null
    updateHistory(run)
    return run
  } catch (exception) {
    if (controller.signal.aborted || epoch !== generation) return
    if (exception instanceof ApiError && [401, 403, 404, 409].includes(exception.status)) selectedRun.value = null
    runDetailError.value = exception instanceof Error ? exception.message : '读取运行详情失败。'
    runPollError.value = id
  } finally {
    if (detailController === controller) detailController = undefined
  }
}
function schedulePoll(epoch: number) {
  clearTimeout(pollTimer)
  pollController?.abort()
  if (epoch !== generation || !activeRun.value) return
  pollTimer = setTimeout(async () => {
    const runningId = activeRun.value?.run_id
    if (!runningId) return
    const controller = new AbortController()
    pollController = controller
    try {
      let run: Run | undefined
      if (tab.value === 'runs' && selectedRunId.value === runningId) {
        run = await readSelectedRun()
      } else {
        run = await request<Run>(`${path.value}/runs/${encodeURIComponent(runningId)}?view=summary`, { signal: controller.signal })
        if (controller.signal.aborted || epoch !== generation) return
        updateHistory(run)
      }
      if (run && !isActive(run) && epoch === generation && !controller.signal.aborted) {
        try { await refreshGraphs() } catch (exception) { if (epoch === generation) report(exception) }
        if (epoch === generation && (tab.value === 'business' || tab.value === 'retrieval')) await refreshRetrievalStatus()
        if (epoch === generation) notify(graphOutcome(run.report) || `运行${runLabels[run.status]}，请查看结果与报告。`)
      }
    } catch { if (epoch === generation && !controller.signal.aborted) runPollError.value = runningId }
    if (epoch === generation && !controller.signal.aborted) schedulePoll(epoch)
  }, 1000)
}
watch([() => tab.value === 'runs' && selectedRunId.value, () => project.value?.project_id, configRevision], () => {
  detailController?.abort()
  detailController = undefined
  selectedRun.value = null
  traceNodeId.value = ''
  runDetailError.value = ''
  void readSelectedRun()
  schedulePoll(generation)
})
async function refreshCredentials() {
  const id = project.value!.project_id, epoch = generation
  await ensureSession(id)
  if (epoch !== generation) return
  const status = await request<CredentialStatus>(`${projectPath(id)}/credentials`)
  if (epoch === generation) credentials.value = status
}
function revisionHeaders(): Record<string, string> { return configRevision.value ? { 'If-Match': configRevision.value } : {} }
function advancedConfig(): RunConfig {
  let value: unknown
  try { value = JSON.parse(advanced.value) } catch { throw new Error('JSON 格式不正确，请检查引号、逗号和括号。') }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('配置必须是 JSON 对象。')
  assertNoCredentials(value)
  if ((value as RunConfig).record_trace === false) throw new Error('运行需要保存轨迹，请将 record_trace 设为 true。')
  return value as RunConfig
}
type SavedModelBinding = { path: string; revision?: string; expectedModel: { base_url: string; model: string }; expectedEmbedding: { base_url: string; model: string } }
async function persistConfiguration(value: RunConfig): Promise<SavedModelBinding> {
  const projectPath = path.value
  const endpoint = `${projectPath}/config`
  const options = { method: 'PUT', headers: revisionHeaders(), body: json(value) }
  // Changing the selected tables can bind or suspend existing Markdown in the same write.
  const rebindsFiles = documentFiles.value.some(file => file.binding_origin === 'filename') &&
    JSON.stringify(config.value.sources) !== JSON.stringify(value.sources)
  let saved: RunConfig, revision: string | undefined
  if (rebindsFiles) {
    const result = await request<{ project: Project; config: RunConfig }>(`${endpoint}?include_project=true`, options)
    saved = result.config
    revision = responseRevision(result)
    configRevision.value = revision || configRevision.value
    applyProjectConfig(result.project, saved)
  } else {
    saved = await request<RunConfig>(endpoint, options)
    revision = responseRevision(saved)
    fillForms(saved)
  }
  // Credential writes must use this write's exact endpoint and revision.
  return {
    path: projectPath,
    revision,
    expectedModel: { base_url: saved.model?.base_url || '', model: saved.model?.model || '' },
    expectedEmbedding: { base_url: saved.embedding?.base_url || '', model: saved.embedding?.model || '' },
  }
}
async function saveForms() {
  const value = advancedDirty.value ? mergeConfigDraft(JSON.parse(advancedBaseline.value), previewConfig(), advancedConfig()) : formConfig()
  const saved = await persistConfiguration(value)
  await saveModelCredential(saved)
  await saveEmbeddingCredential(saved)
}
async function saveEmbeddingCredential(saved: SavedModelBinding) {
  if (!embeddingKey.value) return
  const key = embeddingKey.value
  try {
    await ensureSession(project.value!.project_id)
    embeddingCredStatus.value = await request<typeof embeddingCredStatus.value>(`${saved.path}/credentials/embedding`, {
      method: 'PUT',
      headers: saved.revision ? { 'If-Match': saved.revision } : {},
      body: json({ api_key: key, expected_embedding: saved.expectedEmbedding }),
    })
    embeddingKey.value = ''
  } catch (exception) {
    if (exception instanceof ApiError && ['config_revision_conflict', 'embedding_credential_binding_conflict'].includes(exception.code)) configurationConflict.value = true
    throw new Error(`项目配置已保存，但向量服务 API key 保存失败，输入已保留。${exception instanceof Error ? exception.message : '请重试。'}`)
  }
}
async function clearEmbeddingCredential() {
  await action(async () => {
    await ensureSession(project.value!.project_id)
    embeddingCredStatus.value = await request<typeof embeddingCredStatus.value>(`${path.value}/credentials/embedding`, { method: 'DELETE', headers: revisionHeaders() })
    embeddingKey.value = ''
    notify('已清除本机保存的向量服务密钥')
  })
}
async function testEmbeddingEndpoint() {
  if (embeddingTesting.value || !project.value) return
  const epoch = generation
  const pid = project.value.project_id
  embeddingTesting.value = true
  error.value = ''
  embeddingTestResult.value = null
  try {
    await ensureSession(pid)
    if (generation !== epoch) return
    const timeout = embedding.value.timeout_seconds ?? defaultEmbeddingTimeout
    const payload: Record<string, unknown> = {
      provider: embedding.value.provider || 'none',
      base_url: embedding.value.base_url || '',
      model: embedding.value.model || '',
      api_key_env: embedding.value.api_key_env || 'LIGHTGRAPH_EMBEDDING_API_KEY',
      allow_insecure_http: !!embedding.value.allow_insecure_http,
      timeout_seconds: timeout,
      dimensions: embedding.value.dimensions ?? null,
      query_instruction: embedding.value.query_instruction || '',
    }
    if (embeddingKey.value) payload.api_key = embeddingKey.value
    const result = await request<NonNullable<typeof embeddingTestResult.value>>(`${projectPath(pid)}/retrieval/embedding/test`, {
      timeoutMs: Math.min(2 ** 31 - 1, (timeout + 10) * 1000),
      method: 'POST',
      body: json(payload),
    })
    if (generation === epoch) embeddingTestResult.value = result
  } catch (exception) {
    if (generation === epoch) report(exception)
  } finally {
    if (generation === epoch) embeddingTesting.value = false
  }
}
function fillQwenEmbeddingPreset() {
  embedding.value = {
    ...embedding.value,
    provider: 'openai_compatible',
    base_url: 'http://127.0.0.1:8002/v1',
    model: 'Qwen/Qwen3-Embedding-0.6B',
    dimensions: null,
    allow_insecure_http: false,
    batch_size: embedding.value.batch_size || 32,
    timeout_seconds: embedding.value.timeout_seconds ?? defaultEmbeddingTimeout,
    query_instruction: 'Given a question, retrieve graph entities relevant to its objects, constraints and requested relationships.',
  }
}
async function refreshRetrievalStatus() {
  const pid = project.value?.project_id
  if (!pid) return
  const epoch = generation
  try {
    await ensureSession(pid)
    if (epoch !== generation) return
    const value = await request<RetrievalStatusResponse>(`${projectPath(pid)}/retrieval/status`)
    if (epoch === generation) retrievalStatus.value = value
  } catch (exception) {
    if (epoch === generation && tab.value === 'retrieval') report(exception)
  }
}
async function vectorizeGraph() {
  if (!project.value || vectorizing.value) return
  try {
    if (dirty.value || advancedDirty.value) await saveDraft()
    await startVectorization()
  } catch (exception) { report(exception) }
}
async function retryWorkflowUnit(unitId: string) {
  await action(async () => {
    if (dirty.value || advancedDirty.value) await saveDraft()
    await ensureSession(project.value!.project_id)
    const run = await request<Run>(`${path.value}/workflow/units/${encodeURIComponent(unitId)}/retry`, {
      method: 'POST',
      headers: revisionHeaders(),
      body: '{}',
    })
    runs.value = [historyRow(run), ...runs.value.filter(item => item.run_id !== run.run_id)]
    selectedRunId.value = run.run_id
    tab.value = 'runs'
    schedulePoll(generation)
    notify(`已单独重跑链路 ${unitId}`)
  })
}
async function resumeWorkflow(target: { from_node_id: string; action?: 'recompose_legacy_merge'; verify_checkpoints_in_background?: true } | { artifact_id: string }) {
  const original = selectedRun.value
  if (!original || !project.value) return
  await action(async () => {
    if (dirty.value || advancedDirty.value) await saveDraft()
    const actionName = 'artifact_id' in target ? 'publish_artifact' : target.action || 'rerun_node'
    if (actionName === 'rerun_node') await ensureSession(original.project_id)
    const run = await request<Run>(`${projectPath(original.project_id)}/runs/${encodeURIComponent(original.run_id)}/resume`, {
      method: 'POST', headers: revisionHeaders(), body: json({ ...target, action: actionName }),
    })
    runs.value = [historyRow(run), ...runs.value.filter(item => item.run_id !== run.run_id)]
    selectedRunId.value = run.run_id; tab.value = 'runs'
    schedulePoll(generation)
    if (!isActive(run)) await loadBusinessGraph()
    notify(actionName === 'rerun_node' ? '已创建新的节点恢复运行' : '已创建项目发布任务')
  })
}
async function remergeWorkflow() {
  await action(async () => {
    if (dirty.value || advancedDirty.value) await saveDraft()
    const summary = await request<Record<string, unknown>>(`${path.value}/workflow/merge`, {
      method: 'POST',
      headers: revisionHeaders(),
      body: '{}',
    })
    await readSelectedRun()
    await refreshGraphs()
    notify(`已重新发布 ${summary.nodes ?? 0} 个节点、${summary.edges ?? 0} 条边`)
  })
}
async function saveModelCredential(saved: SavedModelBinding) {
  if (modelKey.value) {
    const key = modelKey.value
    try {
      await ensureSession(project.value!.project_id)
      credentials.value = await request<CredentialStatus>(`${saved.path}/credentials/model`, { method: 'PUT', headers: saved.revision ? { 'If-Match': saved.revision } : {}, body: json({ api_key: key, expected_model: saved.expectedModel }) })
      modelKey.value = ''
    } catch (exception) {
      if (exception instanceof ApiError && ['config_revision_conflict', 'model_credential_binding_conflict'].includes(exception.code)) configurationConflict.value = true
      throw new Error(`项目配置已保存，但 API key 保存失败，输入已保留。${exception instanceof Error ? exception.message : '请重试。'}`)
    }
  } else {
    // Presence is secondary to the committed configuration. A failed GET must
    // not turn a successful save into an apparent failed write.
    try { await refreshCredentials() } catch { notify('配置已保存；密钥状态暂未刷新。') }
  }
}
async function saveDraft() {
  if (advancedDirty.value) {
    if (agentLimitsDirty.value) await saveModelLimits()
    else await saveForms()
  } else {
    if (agentLimitsDirty.value) await saveModelLimits()
    if (dirty.value) await saveForms()
  }
}
async function reloadConfiguration() {
  if ((dirty.value || advancedDirty.value) && !window.confirm('重新加载会放弃当前未保存的配置修改，继续吗？')) return
  await action(async () => { closeDatabaseSource(); await refreshProjectConfig(); modelKey.value = ''; notify('已读取服务器配置') })
}
async function clearModelCredential() {
  await action(async () => { credentials.value = await request<CredentialStatus>(`${path.value}/credentials/model`, { method: 'DELETE', headers: revisionHeaders() }); modelKey.value = ''; notify('已清除本机保存的模型密钥') })
}
async function previewFileMetadata(file: ProjectFile) {
  await request(`${path.value}/files/${encodeURIComponent(file.file_id)}/metadata`)
  metadata.value = await request<Metadata>(`${path.value}/metadata`)
}

function askDeletion(target: DeletionTarget) {
  if (target.kind === 'run' ? busy.value || isActive(target.run) : locked.value) return
  if (target.kind !== 'project' && target.kind !== 'run' && advancedDirty.value) {
    error.value = '完整项目配置有未保存修改，请先保存或放弃，再删除数据。'
    return
  }
  deletionError.value = ''; deletion.value = target
}
async function confirmDeletion() {
  const target = deletion.value, id = project.value?.project_id
  if (!target || !id || (target.kind === 'run' ? busy.value || isActive(target.run) : locked.value)) return
  busy.value = true; deletionError.value = ''; error.value = ''
  let deleted = false
  try {
    await ensureSession(id)
    const base = projectPath(id)
    if (target.kind === 'run') {
      const rid = target.run.run_id
      await request(`${base}/runs/${encodeURIComponent(rid)}`, { method: 'DELETE', headers: revisionHeaders() })
      if (project.value?.project_id !== id) return
      runs.value = runs.value.filter(run => run.run_id !== rid)
      if (selectedRunId.value === rid) selectedRunId.value = runs.value[0]?.run_id || ''
      deletion.value = null
      notify('运行记录已删除')
      return
    }
    const endpoint = target.kind === 'project' ? base : target.kind === 'file' ? `${base}/files/${encodeURIComponent(target.file.file_id)}` : `${base}/sources/${encodeURIComponent(target.source.id)}${target.kind === 'table' ? '/tables' : ''}`
    const result = await request<{ project?: Project; config?: RunConfig; detached_rule_paths?: string[] }>(endpoint, { method: 'DELETE', headers: revisionHeaders(), ...(target.kind === 'table' ? { body: json({ table: target.table }) } : {}) })
    configRevision.value = responseRevision(result) || configRevision.value
    deleted = true; deletion.value = null
    const message = target.kind === 'project' ? '项目已删除' : target.kind === 'table' ? '数据表已移除' : target.kind === 'source' ? '数据库来源已删除' : '文件已删除'
    notify(message + (result.detached_rule_paths?.length ? `，已停用 ${result.detached_rule_paths.length} 个关联规则文件` : ''))
    if (target.kind === 'project') {
      // Invalidate old requests and polling before selecting another project.
      generation++; graphController?.abort(); clearTimeout(pollTimer); clearSession(id); workbenchLocation.removeProject(id)
      projects.value = projects.value.filter(item => item.project_id !== id)
      project.value = null; fillForms({}); modelKey.value = ''; credentials.value = { database_sources: [], model: false }
      metadata.value = { tables: [], nodes: [], edges: [] }; business.value = { nodes: [], edges: [] }; runs.value = []; selectedRunId.value = ''
      databaseOpen.value = false; databaseSource.value = null; preview.value = null
      tab.value = 'inputs'; configurationTab.value = 'inputs'; resultTab.value = 'runs'
      await reloadProjects()
    } else {
      if (target.kind === 'file') preview.value = null
      workbenchLocation.closeDatabase(id)
      // Do not show deleted metadata if the follow-up refresh fails.
      metadata.value = { tables: [], nodes: [], edges: [] }
      // The DELETE response is authoritative even if subsequent metadata requests fail.
      if (result.project && result.config) applyProjectConfig(result.project, result.config, true)
      else await refreshProjectConfig(true)
      metadata.value = await request<Metadata>(`${base}/metadata`)
      await refreshCredentials()
    }
  } catch (exception) {
    if (deleted) error.value = `已删除，但列表刷新失败：${exception instanceof Error ? exception.message : '请刷新项目。'}`
    else deletionError.value = exception instanceof Error ? exception.message : '删除失败，请重试。'
  } finally { busy.value = false }
}
async function createProject() {
  await action(async () => {
    const created = await request<Project>('/projects', { method: 'POST', body: json({ name: newName.value.trim(), ...(newId.value.trim() ? { project_id: newId.value.trim() } : {}) }) })
    newProjectOpen.value = false; newName.value = ''; newId.value = ''; await reloadProjects(created.project_id); notify('项目已创建')
  })
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement
  const files = [...(input.files || [])]
  if (!files.length) return
  await action(async () => {
    if (dirty.value || advancedDirty.value) await saveDraft()
    let count = 0
    try {
      for (const file of files) {
        const body = new FormData(); body.append('file', file)
        const uploaded = await request<ProjectFile>(`${path.value}/files`, { method: 'POST', headers: revisionHeaders(), body }); configRevision.value = responseRevision(uploaded) || configRevision.value; count++
        if (uploaded.kind === 'excel' || uploaded.kind === 'csv') await previewFileMetadata(uploaded)
      }
      notify(`已上传 ${count} 个文件${files.some(file => /\.(xlsx|csv)$/i.test(file.name)) ? '，数据文件元数据已解析' : ''}`)
    } finally { await refreshProjectConfig(); input.value = '' }
  })
}
async function showContent(file: ProjectFile) {
  await action(async () => { const result = await request<{ content: string }>(`${path.value}/files/${encodeURIComponent(file.file_id)}/content`); preview.value = { name: file.name, content: result.content } })
}
async function saveDocumentBinding(file: ProjectFile, purpose: string, scope: DocumentScope) {
  if (locked.value) return
  await action(async () => {
    if (dirty.value || advancedDirty.value) await saveDraft()
    const saved = await request<ProjectFile>(`${path.value}/files/${encodeURIComponent(file.file_id)}/binding`, {
      method: 'PATCH', headers: revisionHeaders(), body: json({ purpose, scope }),
    })
    configRevision.value = responseRevision(saved) || configRevision.value
    await refreshProjectConfig()
    notify('文档用途和使用范围已保存')
  })
}
async function importDocumentManifest(manifest: File, documents: File[], done?: () => void) {
  if (locked.value) return
  await action(async () => {
    const endpoint = `${path.value}/documents/manifest`
    const form = new FormData()
    form.append('manifest', manifest)
    const selection = await request<{ paths: string[]; missing_paths: string[] }>(`${endpoint}/paths`, { method: 'POST', body: form })
    const body = documentManifestBody(manifest, selection, documents)
    if (dirty.value || advancedDirty.value) await saveDraft()
    const result = await request<{ files: ProjectFile[]; imported: number }>(endpoint, {
      method: 'POST', headers: revisionHeaders(), body,
    })
    configRevision.value = responseRevision(result) || configRevision.value
    done?.()
    await refreshProjectConfig()
    notify(`已按清单配置 ${result.imported} 份知识文档`)
  })
}
async function refreshMetadata(saveEdits = true) {
  await action(async () => {
    if (saveEdits && (dirty.value || advancedDirty.value)) await saveDraft()
    const epoch = generation
    await ensureSession(project.value!.project_id)
    const refreshed = await request<Metadata>(`${path.value}/metadata/refresh`, { method: 'POST', body: '{}' })
    if (epoch !== generation) return
    metadata.value = refreshed
    notify('元数据已更新')
  })
}
async function startRun() {
  await action(async () => {
    await ensureSession(project.value!.project_id)
    await saveDraft()
    const run = await request<Run>(`${path.value}/runs`, { method: 'POST', headers: revisionHeaders(), body: JSON.stringify({ run_mode: runMode.value }) })
    if (run.run_mode === 'fresh' && run.status !== 'failed') {
      if (!selectedGraphVersion.value) { business.value = { nodes: [], edges: [] }; structurePreviewOpen.value = false; void loadBusinessGraph() }
    }
    runPollError.value = null
    runs.value = [historyRow(run), ...runs.value.filter(item => item.run_id !== run.run_id)]; selectedRunId.value = run.run_id; tab.value = 'runs'; schedulePoll(generation)
  })
}
async function cancelRun(run: Run) {
  await action(async () => {
    const result = await request<Run>(`${path.value}/runs/${encodeURIComponent(run.run_id)}/cancel`, { method: 'POST', body: '{}' })
    updateHistory(result)
    if (selectedRunId.value === result.run_id) {
      detailController?.abort()
      detailController = undefined
      void readSelectedRun()
    } else selectedRunId.value = result.run_id
    schedulePoll(generation)
    if (!isActive(result)) await refreshGraphs()
  })
}
function inspectRun(run: Run) {
  if (selectedRunId.value !== run.run_id) selectedRunId.value = run.run_id
  else if (runDetailError.value) void readSelectedRun()
}
function showRunProgress(run: Run) { selectedRunId.value = run.run_id; tab.value = 'runs' }
async function saveAdvanced() {
  await action(async () => {
    // Unedited JSON is only the last saved snapshot. It must not overwrite
    // newer ordinary form controls just because this save button was chosen.
    await saveDraft(); notify('完整配置已保存')
  })
}
function discardFormEdits() {
  try {
    const jsonDraft = JSON.stringify(mergeConfigDraft(JSON.parse(advancedBaseline.value), JSON.parse(JSON.stringify(config.value)), advancedConfig(), 'draft'), null, 2)
    fillForms(JSON.parse(JSON.stringify(config.value)) as RunConfig)
    agentLimits.value = JSON.parse(agentLimitsSaved.value)
    advanced.value = jsonDraft
  } catch (exception) { report(exception) }
}
async function openDatabaseSource(source: SourceConfig | null = null) {
  await action(async () => {
    if (dirty.value || advancedDirty.value) await saveDraft()
    error.value = ''; databaseSource.value = source; databaseOpen.value = true
    workbenchLocation.openDatabase(project.value!.project_id, source?.id ?? null)
  })
}
function closeDatabaseSource() {
  databaseOpen.value = false
  if (project.value) workbenchLocation.closeDatabase(project.value.project_id)
}
async function databaseConnected(value: RunConfig, result: Metadata, revision?: string) {
  configRevision.value = revision || configRevision.value
  closeDatabaseSource()
  await action(async () => { fillForms(value); metadata.value = result; await refreshProjectConfig(); await refreshCredentials(); notify('数据库已接入，选中表的元数据已生成') })
}

async function saveDatabaseSources(sources: SourceConfig[], retentionVerified: boolean) {
  await action(async () => {
    const value = { ...formConfig(), sources, deployment_retention_verified: retentionVerified }
    await persistConfiguration(value)
    await databaseDialog.value?.completeDraft()
    closeDatabaseSource(); await refreshCredentials(); notify('数据库来源已保存，点击读取元数据可连接服务器。')
  })
}
function capabilityUploaded(file: ProjectFile, revision?: string) {
  configRevision.value = revision || configRevision.value
  if (project.value) project.value.files = [...project.value.files.filter(item => item.file_id !== file.file_id), file]
}
watch(tab, (value, previous) => {
  if (value === 'business') void loadBusinessGraph()
  if (previous === 'business') {
    graphRequestEpoch++; graphController?.abort(); graphLoading.value = false; graphOptionsOpen.value = false
    business.value = { nodes: [], edges: [] }
  }
})
watch([() => !loading.value && tab.value === 'settings' && 'embedding' in config.value ? project.value?.project_id : undefined, configRevision], async ([id], _, onCleanup) => {
  if (!id) return
  const controller = new AbortController(), epoch = generation
  onCleanup(() => controller.abort())
  const previous = embeddingCredStatus.value
  try {
    await ensureSession(id)
    if (controller.signal.aborted) return
    const status = await request<typeof embeddingCredStatus.value>(`${projectPath(id)}/credentials/embedding`, { signal: controller.signal })
    if (!controller.signal.aborted && generation === epoch && embeddingCredStatus.value === previous) embeddingCredStatus.value = status
  } catch { /* optional */ }
})
watch([tab, () => project.value?.project_id], () => {
  if (['business', 'retrieval'].includes(tab.value)) void refreshRetrievalStatus()
})
watch(graphBusinessContext, () => { business.value = { nodes: [], edges: [] }; void loadBusinessGraph() })
watch(includeCandidates, () => { void action(refreshGraphs) })
onMounted(async () => { try { await reloadProjects() } catch (exception) { report(exception); loading.value = false } })
onUnmounted(() => { generation++; detailController?.abort(); pollController?.abort(); graphController?.abort(); clearTimeout(pollTimer); clearTimeout(toastTimer); modelKey.value = ''; embeddingKey.value = ''; clearSessions() })
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark"><svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="m6 6 12 5-8 8L6 6Z" stroke="currentColor" stroke-width="1.4"/><circle cx="6" cy="6" r="3" fill="currentColor"/><circle cx="18" cy="11" r="3" fill="currentColor"/><circle cx="10" cy="19" r="3" fill="currentColor"/></svg></span><span>数据图谱<small>DATA GRAPH</small></span></div>
      <div class="sidebar-label">工作空间 <button class="icon-btn" aria-label="刷新项目列表" :disabled="busy || loading" @click="action(() => reloadProjects())">↻</button></div>
      <div class="project-list"><button v-for="item in projects" :key="item.project_id" class="project" :class="{ active: project?.project_id === item.project_id }" :disabled="busy || loading" @click="loadProject(item.project_id)"><span class="project-icon">{{ item.name.slice(0, 1) }}</span><span class="project-copy">{{ item.name }}<small>{{ item.project_id }}</small></span></button><p v-if="!projects.length && !loading" class="empty-projects">还没有项目</p></div>
      <button class="btn create-project" :disabled="busy" @click="newProjectOpen = true">＋ 新建项目</button>

    </aside>
    <main class="workspace">
      <header class="topbar"><span>工作台 <span class="breadcrumb-separator">/</span> <strong>{{ project?.name || '项目管理' }}</strong></span><button v-if="project" class="text-button danger-text" :disabled="locked" @click="askDeletion({ kind: 'project', id: project.project_id, name: project.name })">删除项目</button></header>
      <div v-if="error" class="error-strip" role="alert"><span>{{ error }}</span><button v-if="configurationConflict" class="btn small" @click="reloadConfiguration">重新加载配置</button><button aria-label="关闭错误提示" @click="error = ''">×</button></div>
      <div v-if="loading" class="page-empty"><div class="spinner"></div><p>正在读取项目</p></div>
      <div v-else-if="!project" class="page-empty"><span class="empty-graphic">◇</span><h1>从一个项目开始</h1><p>上传数据、指定抽取目的，让 Agent 发现关键列和关系。<br/>数据文件和 Markdown 会保存在项目自己的文件夹中。</p><button class="btn primary" @click="newProjectOpen = true">新建项目</button><button v-if="error" class="btn" @click="action(() => reloadProjects(projects[0]?.project_id))">重新连接</button></div>
      <template v-else>
        <section class="project-head">
          <div class="phase-bar">
            <div class="phase-tabs" role="tablist" aria-label="工作阶段" @keydown="phaseKeydown">
              <button id="configuration-phase" role="tab" aria-controls="phase-content" :aria-selected="!inResults" :tabindex="!inResults ? 0 : -1" :class="{ active: !inResults }" @click="tab = configurationTab">项目配置</button>
              <button id="results-phase" role="tab" aria-controls="phase-content" :aria-selected="inResults" :tabindex="inResults ? 0 : -1" :class="{ active: inResults }" @click="tab = resultTab">运行与结果<span v-if="activeRun" class="status-dot" aria-label="正在运行"></span></button>
            </div>
            <div class="actions"><label for="run-mode" class="sr-only">运行方式</label><select id="run-mode" v-model="runMode" :disabled="locked" :title="runMode === 'fresh' ? '使用空白工作区，保留历史图谱、草稿和轨迹；当前画布切换为本轮结果' : '复用当前工作区中的草稿、证据和会话记录'"><option value="continue">继续上次工作</option><option value="fresh">从零开始（保留历史）</option></select><span v-if="dirty || advancedDirty" class="badge amber">有未保存修改</span><button class="btn primary" :disabled="locked || agentLimitsLoading || !!limitsError || (!config.sources?.length && !config.documents?.length && !inheritedGraphs.length && !advancedDirty) || (!purpose.trim() && !advancedDirty)" :title="!purpose.trim() ? '请先填写抽取目的' : '保存配置后启动抽取'" @click="startRun">{{ activeRun ? '正在抽取' : dirty || advancedDirty ? '保存并开始抽取' : '开始抽取' }}</button></div>
          </div>
          <nav class="tabs" :aria-label="inResults ? '运行与结果' : '项目配置'"><button v-for="item in tabs" :key="item.id" class="tab" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.label }}<span v-if="item.id === 'runs' && runs.length" class="count">{{ runs.length }}</span></button></nav>
        </section>
        <div v-if="metadataPending || credentialsPending" id="phase-content" class="page-empty" role="tabpanel" :aria-labelledby="inResults ? 'results-phase' : 'configuration-phase'"><div class="spinner"></div><p>{{ metadataPending ? '正在读取元数据' : '正在读取连接状态' }}</p></div>
        <div v-else id="phase-content" class="content" :class="{ 'inputs-content': tab === 'inputs', 'documents-content': tab === 'documents', 'workflow-content': tab === 'workflow' }" role="tabpanel" :aria-labelledby="inResults ? 'results-phase' : 'configuration-phase'">
          <div v-if="activeRun" class="run-banner"><span class="spinner tiny"></span><div><strong>{{ runLabels[activeRun.status] }}</strong><span>{{ stageLabel(activeRun.progress?.stage || activeRun.stage) }} · {{ activeRun.run_id }}</span></div><div class="actions"><button class="btn small" @click="showRunProgress(activeRun)">查看进度与日志</button><button class="btn small" :disabled="busy" @click="cancelRun(activeRun)">取消运行</button></div></div>
          <template v-if="tab === 'goal'">
            <div class="intro"><div><h2>这次希望发现什么？</h2></div></div>
            <div class="layout-two"><section class="panel"><div class="panel-head"><h2>抽取目标</h2><span class="badge">Markdown</span></div><div class="panel-body"><div class="field"><label for="purpose">目标与要求</label><textarea id="purpose" v-model="purpose" :disabled="locked" class="goal-input" maxlength="64000" placeholder="# 本次目标&#10;&#10;说明希望发现的对象、属性和关系。&#10;&#10;## 补充说明&#10;&#10;可填写业务定义、关注范围、排除内容和验收要求。"/><p class="field-hint">目标与知识材料统一使用 Markdown。目标直接指导本次构建，知识文档按分析需要读取；文字内容不会改变数据接入权限。</p></div><p v-if="config.goal?.entity_kinds?.length || config.goal?.relation_kinds?.length" class="note">当前配置还有限定类型：{{ [...(config.goal.entity_kinds || []), ...(config.goal.relation_kinds || [])].join('、') }}。这些程序约束保留在完整配置中，编辑 Markdown 不会自动解除。</p></div><div class="panel-footer"><span>保存后保留 Markdown 原文；下次运行使用已保存修订</span><button class="btn primary" :disabled="locked" @click="action(async () => { await saveForms(); notify('抽取目标已保存') })">保存目标</button></div></section><aside class="stack"><section class="panel"><div class="panel-head"><h2>知识材料</h2></div><div class="panel-body"><p>已有 {{ documentFiles.length }} 份 Markdown 文档。可补充数据字典、业务说明或关系定义，不必预先编写抽取规则。</p><button class="btn" @click="tab = 'documents'">管理知识文档</button></div></section><section class="panel"><div class="panel-head"><h2>项目概况</h2></div><div class="panel-body"><div class="scope-row"><span>数据来源</span><strong>{{ config.sources?.length || 0 }}</strong></div><div class="scope-row"><span>已读取的表</span><strong>{{ metadata.tables.length }}</strong></div><div class="scope-row"><span>业务图节点</span><strong>{{ business.total_nodes ?? business.nodes.length }}</strong></div></div></section></aside></div>
          </template>
          <template v-if="tab === 'workflow'">
            <WorkflowEditor
              :project-id="project.project_id"
              :projects="projects"
              :sources="config.sources || []"
              :tables="metadata.tables"
              :documents="documentFiles.filter(file => (config.documents || []).some(doc => doc === file.relative_path || doc === file.file_id || doc === file.name || doc.endsWith('/' + file.relative_path)))"
              :scopes="Array.isArray(config.scopes) ? config.scopes : ['public']"
              :workflow="workflowConfig"
              :inherited-graphs="inheritedGraphs"
              :locked="locked"
              :dirty="dirty || advancedDirty"
              :run-disabled="agentLimitsLoading || !!limitsError || (!config.sources?.length && !config.documents?.length && !inheritedGraphs.length && !advancedDirty) || (!purpose.trim() && !advancedDirty)"
              @update:workflow="val => workflowConfig = val"
              @update:inherited-graphs="val => inheritedGraphs = val"
              @save="action(async () => { await saveDraft(); notify('并行工作流配置已保存') })"
              @start-run="startRun"
              @refresh-tables="refreshMetadata(false)"
            />
          </template>
          <template v-if="tab === 'inputs'">
            <input ref="uploadInput" type="file" hidden multiple accept=".xlsx,.csv" @change="upload"/>
            <DataInputs :key="project.project_id" :project-id="project.project_id" :sources="config.sources || []" :files="project.files" :metadata="metadata" :credential-sources="credentials.database_sources" :credential-errors="credentials.database_errors" :locked="locked" :retention-verified="config.deployment_retention_verified === true" @connect="openDatabaseSource" @upload="uploadInput?.click()" @refresh="refreshMetadata" @delete-file="file => askDeletion({ kind: 'file', file })" @delete-source="source => askDeletion({ kind: 'source', source })" @delete-table="(source, table) => askDeletion({ kind: 'table', source, table })"/>
          </template>
          <DocumentLibrary v-if="tab === 'documents'" :key="project.project_id"
            :files="documentFiles" :active-documents="config.documents || []" :table-keys="documentTableKeys"
            :locked="locked" :busy="busy" @upload="upload" @import-manifest="importDocumentManifest" @save-binding="saveDocumentBinding"
            @preview="showContent" @delete-file="file => askDeletion({ kind: 'file', file })"/>
          <VectorizationProgress v-if="vectorizing && (tab === 'retrieval' || (tab === 'business' && !selectedGraphVersion))" :progress="vectorJob.progress"/>
          <template v-if="tab === 'metadata' || tab === 'business'"><div class="intro"><div><div class="graph-heading"><h2>{{ tab === 'metadata' ? '元数据关联图' : '业务知识图谱' }}</h2><span v-if="tab === 'business' && typeof business.merged_observation_nodes === 'number'" class="badge" title="当前范围内，合并后保留了多个抽取单元同源证据的节点数">已归并 {{ business.merged_observation_nodes }} 个节点</span></div></div><div class="actions"><template v-if="tab === 'business' && !selectedGraphVersion && (business.nodes.length > 0 || retrievalStatus?.has_graph)"><RetrievalIndexControls :status="retrievalStatus" :working="vectorizing" :busy="busy" @build="vectorizeGraph"/></template><button v-if="tab === 'business' && !selectedGraphVersion" class="btn" :aria-expanded="structurePreviewOpen" @click="structurePreviewOpen = !structurePreviewOpen">{{ structurePreviewOpen ? '收起结构方案' : '查看结构方案' }}</button><button class="btn" :disabled="busy" @click="action(refreshGraphs)">刷新图谱</button></div></div><p v-if="tab === 'metadata' && metadata.requires_refresh" class="note graph-notice">{{ metadata.tables.length ? '目前仅显示已读取来源的元数据；其他来源仍需连接并读取。' : '尚未读取元数据。请在「项目配置 → 数据接入」连接来源并读取元数据。' }}</p><p v-if="tab === 'metadata' && metadata.stale_engine_metadata" class="note amber graph-notice">元数据声明已更新。重新运行抽取后，会刷新与新声明对应的关系。</p>
            <details v-if="tab === 'business'" class="graph-view-options" @toggle="toggleGraphOptions">
              <summary><strong>版本与范围</strong><span>{{ selectedGraphVersion ? '历史快照' : '当前发布' }} · {{ graphBusinessContext === null ? '全部内容' : Object.entries(graphBusinessContext).map(([key, val]) => key + ' ' + val).join(' · ') || '仅通用知识' }}{{ includeCandidates ? ' · 含候选' : '' }}</span></summary>
              <div v-if="graphOptionsOpen" class="graph-options-body">
                <GraphHistory :project-id="project.project_id" :model-value="selectedGraphVersion" :config-revision="configRevision" :refresh-key="business" @update:model-value="selectGraphVersion"/>
                <BusinessContextSelect v-model="graphBusinessContext" :project-id="project.project_id" :version="selectedGraphVersion" :refresh-key="`${configRevision}:${business.version_id || business.generation || ''}`" :disabled="busy"/>
                <label class="checkbox-line graph-candidates-toggle"><input v-model="includeCandidates" type="checkbox" :disabled="busy"/>显示候选与未决关系</label>
              </div>
            </details>
            <p v-if="tab === 'business' && graphError" class="note amber" role="alert">图谱读取失败：{{ graphError }} <button class="text-button" @click="loadBusinessGraph()">重试读取图谱</button></p><StructurePreview v-if="tab === 'business' && !selectedGraphVersion && structurePreviewOpen" :key="project.project_id" :project-id="project.project_id" :config-revision="configRevision"/><GraphCanvas v-if="tab !== 'business' || !graphError || business.nodes.length > 0" :graph="tab === 'metadata' ? metadata : business" :layer="tab" :version="tab === 'business' ? selectedGraphVersion : undefined" :loading="busy || (tab === 'business' && graphLoading)" :project-id="project.project_id" :include-candidates="includeCandidates" :business-context="tab === 'business' ? graphBusinessContext : null" :config-revision="configRevision"/>
            <GraphAnalysis v-if="tab === 'business' && typeof business.version_id === 'string' && business.version_id && !graphError && !graphLoading" :project-id="project.project_id" :version="business.version_id" :business-context="graphBusinessContext" :graph="business"/>
            <GraphAlignmentReview v-if="tab === 'business' && includeCandidates" :graph="business" :project-id="project.project_id" :version="selectedGraphVersion" :business-context="graphBusinessContext"/><IdentityCandidates v-if="tab === 'business' && !selectedGraphVersion && !graphLoading && !graphError" :business-context="graphBusinessContext" :project-id="project.project_id" :config-revision="configRevision" :graph="business" :disabled="locked || dirty || advancedDirty" @changed="action(refreshAfterIdentityDecision)"/><DiscoveryCandidates v-if="tab === 'business' && !selectedGraphVersion" :project-id="project.project_id" :config-revision="configRevision" :refresh-key="business" :disabled="locked || dirty || advancedDirty"/><section v-if="tab === 'metadata' && metadata.tables.length" class="panel metadata-table"><div class="panel-head"><h2>表与列</h2></div><details v-for="table in metadata.tables" :key="tableKey(table)"><summary>{{ tableKey(table) }} <span class="muted">{{ table.columns.length }} 列</span></summary><div class="table-scroll"><table><thead><tr><th>字段</th><th>类型</th><th>注释</th></tr></thead><tbody><tr v-for="column in table.columns" :key="column.name"><td>{{ column.name }}</td><td>{{ column.data_type || '—' }}</td><td>{{ column.comment || '—' }}</td></tr></tbody></table></div></details></section></template>
          <p v-if="vectorError && (tab === 'retrieval' || (tab === 'business' && retrievalStatus?.has_graph))" class="note amber">{{ vectorError }}</p>
          <template v-if="tab === 'retrieval'">
            <RetrievalPanel :key="project.project_id" :syncing="vectorizing" :project-id="project.project_id" :embedding="config.embedding" :status="retrievalStatus" :busy="busy || loading || dirty || advancedDirty || locked" @vectorize="vectorizeGraph"/>
          </template>
          <template v-if="tab === 'runs'"><div class="intro"><div><h2>运行记录</h2></div></div><section class="panel"><div v-if="!runs.length" class="empty-state"><h3>尚未开始抽取</h3><p>先上传数据、配置目标和模型，再开始第一轮运行。</p></div><div v-else class="table-scroll run-history-table"><table><thead><tr><th>运行编号</th><th>状态</th><th>当前阶段</th><th>创建时间</th><th>资源释放</th><th></th></tr></thead><tbody><tr v-for="run in historyRows" :key="run.run_id" :class="{ chosen: run.run_id === selectedRunId }"><td class="mono">{{ run.run_id }}</td><td><span class="badge" :class="run.status === 'completed' ? 'green' : run.status === 'failed' ? 'red' : 'blue'">{{ runLabels[run.status] }}</span></td><td>{{ stageLabel(run.progress?.stage || run.stage) }}</td><td>{{ time(run.created_at) }}</td><td>{{ run.cleanup_status === 'completed' ? '已完成' : run.cleanup_status === 'pending' ? '待完成' : '未确认' }}</td><td><button class="btn small" :aria-pressed="run.run_id === selectedRunId" :disabled="busy" @click="inspectRun(run)">{{ run.run_id === selectedRunId ? '正在查看' : isActive(run) ? '查看进度' : '查看报告' }}</button> <button v-if="!isActive(run)" class="text-button danger" :disabled="busy" @click="askDeletion({kind: 'run', run})">删除</button></td></tr></tbody></table></div>
              <nav v-if="historyPages > 1" class="actions" aria-label="运行记录分页">
                <button class="btn small" :disabled="!historyPage" @click="historyPage--">上一页</button>
                <span>{{ historyPage + 1 }} / {{ historyPages }}</span>
                <button class="btn small" :disabled="historyPage + 1 >= historyPages" @click="historyPage++">下一页</button>
              </nav>
            </section>
            <div v-if="runDetailError" class="note amber" role="alert">运行详情刷新失败：{{ runDetailError }} <button class="btn small" @click="readSelectedRun">重试</button></div>
            <p v-else-if="selectedRunId && !selectedRun" class="note" role="status">正在读取运行 {{ selectedRunId }}…</p>
            <section v-if="visibleWorkflowState" class="panel run-report">
              <div class="panel-head">
                <h2>工作流节点执行与合并结果</h2>
                <div class="actions">
                  <button class="btn small" :disabled="busy" @click="readSelectedRun">刷新状态</button>
                  <button v-if="canEditWorkflowRun && !config.workflow?.merge_nodes?.length" class="btn small primary" :disabled="locked" @click="remergeWorkflow">重新发布输出</button>
                </div>
              </div>
              <div class="panel-body">
                <div class="table-scroll">
                  <table>
                    <thead><tr><th>节点</th><th>类型</th><th>状态</th><th>节点数</th><th>关系数</th><th>本轮 LLM 调用</th><th>子运行编号</th><th>说明</th><th>操作</th></tr></thead>
                    <tbody>
                      <tr v-for="(u, unitId) in visibleWorkflowState.units" :key="unitId">
                        <td><strong>{{ u.label || unitId }}</strong> <span class="mono muted">{{ unitId }}</span></td>
                        <td><span class="badge blue">抽取节点</span></td>
                        <td><span class="badge" :class="u.status === 'completed' ? 'green' : u.status === 'failed' ? 'red' : 'blue'">{{ u.status }}</span></td>
                        <td>{{ u.nodes ?? 0 }}</td>
                        <td>{{ u.edges ?? 0 }}</td>
                        <td>{{ u.model_calls ?? '—' }}</td>
                        <td class="mono">{{ u.run_id || '—' }}</td>
                        <td>{{ u.error_code ? new ApiError(u.error_code).message : '正常' }}
                          <div v-if="u.failure_detail" class="field-hint">{{ workflowFailureLabel(u.failure_detail) }}</div>
                        </td>
                        <td><button v-if="nodeTraceTarget(unitId)" class="btn small" :aria-expanded="traceNodeId === unitId" @click="toggleNodeTrace(unitId)">{{ traceNodeId === unitId ? '收起节点轨迹' : '查看节点轨迹' }}</button>
                          <button v-if="!visibleWorkflowState?.artifacts" class="btn small" :disabled="locked || !canEditWorkflowRun || u.status === 'skipped'" @click="retryWorkflowUnit(u.unit_id)">单独重跑</button><button v-if="visibleWorkflowState?.artifacts" class="btn small" :disabled="locked || !config.workflow?.enabled" @click="resumeWorkflow({ from_node_id: u.unit_id })">从此节点重新运行</button></td>
                      </tr>
                      <tr v-for="(m, mergeId) in visibleWorkflowState.merge_nodes" :key="mergeId">
                        <td><strong>{{ m.label || mergeId }}</strong> <span class="mono muted">{{ mergeId }}</span></td>
                        <td><span class="badge amber">{{ m.merge_mode === 'merge_and_extract' ? '合并抽取节点' : '合并节点' }}</span></td>
                        <td><span class="badge" :class="m.status === 'completed' ? 'green' : m.status === 'failed' ? 'red' : 'blue'">{{ m.status }}</span></td>
                        <td>{{ m.nodes ?? 0 }}</td>
                        <td>{{ m.edges ?? 0 }}</td>
                        <td>{{ m.model_calls ?? '—' }}</td>
                        <td class="mono">{{ m.run_id || '—' }}</td>
                        <td>{{ m.error_code ? new ApiError(m.error_code).message : '正常' }}
                          <div v-if="m.merge_counts?.input_nodes != null && m.merge_counts.output_nodes != null" class="field-hint">上游合并 {{ m.merge_counts.input_nodes }} → {{ m.merge_counts.output_nodes }} 个节点<template v-if="m.merge_counts.source_replay_status === 'ran'">，同源去重 {{ m.merge_counts.source_replay_collapsed_nodes }} 个</template><template v-if="m.merge_counts.hierarchy_replay_collapsed_nodes">，同源层级归并 {{ m.merge_counts.hierarchy_replay_collapsed_nodes }} 个</template><template v-if="m.merge_counts.observed_dictionary_collapsed_nodes">，按字典成员合并 {{ m.merge_counts.observed_dictionary_collapsed_nodes }} 个</template></div>
                          <div v-if="m.delta_merge_counts?.source_replay_status === 'ran'" class="field-hint">补充抽取后同源去重 {{ m.delta_merge_counts.source_replay_collapsed_nodes }} 个<template v-if="m.delta_merge_counts.hierarchy_replay_collapsed_nodes">，同源层级归并 {{ m.delta_merge_counts.hierarchy_replay_collapsed_nodes }} 个</template><template v-if="m.delta_merge_counts.observed_dictionary_collapsed_nodes">，按字典成员合并 {{ m.delta_merge_counts.observed_dictionary_collapsed_nodes }} 个</template></div>
                          <div v-for="line in workflowRelationSummary(m)" :key="line" class="field-hint">{{ line }}</div>
                          <div v-if="m.failure_detail" class="field-hint">{{ workflowFailureLabel(m.failure_detail) }}</div>
                        </td>
                        <td><button v-if="nodeTraceTarget(mergeId)" class="btn small" :aria-expanded="traceNodeId === mergeId" @click="toggleNodeTrace(mergeId)">{{ traceNodeId === mergeId ? '收起节点轨迹' : '查看节点轨迹' }}</button>
                          <button class="btn small" :disabled="locked || !config.workflow?.enabled" @click="resumeWorkflow({ from_node_id: m.node_id, verify_checkpoints_in_background: true })">从此节点重新运行</button>
                          <button v-if="!visibleWorkflowState?.artifacts && m.merge_mode === 'merge_and_extract' && ['partial', 'completed', 'completed_with_gaps'].includes(m.status)" class="btn small" :disabled="locked || dirty || advancedDirty" @click="resumeWorkflow({ from_node_id: m.node_id, action: 'recompose_legacy_merge' })">从原合并产物恢复发布</button>
                          <button v-if="visibleWorkflowState?.terminal_ids?.length === 1 && visibleWorkflowState.terminal_ids[0] === m.node_id && visibleWorkflowState.artifacts?.[m.node_id]?.graph_kind === 'node_full'" class="btn small" :disabled="locked" @click="resumeWorkflow({ artifact_id: visibleWorkflowState.artifacts[m.node_id]!.artifact_id })">仅发布此结果</button></td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <p v-if="visibleWorkflowState?.merge" class="note">
                  项目输出：<strong>{{ workflowOutputLabel }}</strong>
                  <template v-if="visibleWorkflowState?.merge?.nodes !== undefined">
                    · 终态节点 {{ visibleWorkflowState?.merge?.nodes }} 个
                    · 终态边 {{ visibleWorkflowState?.merge?.edges }} 条
                    · 待对齐定义 {{ visibleWorkflowState?.merge?.pending_definitions || 0 }} 组
                    · 未决关系 {{ visibleWorkflowState?.merge?.pending_relations || 0 }} 条
                    · 待审对齐候选 {{ (visibleWorkflowState?.merge?.candidate_identity_merges as number || 0) + (visibleWorkflowState?.merge?.candidate_column_alignments as number || 0) }} 条
                  </template>
                </p>
                <RunTrace v-if="selectedRun && traceNode" :key="`${selectedRun.project_id}:${selectedRun.run_id}:${traceNode.nodeId}:${traceNode.runId}`"
                  :run="selectedRun" :node-trace="traceNode" @close="traceNodeId = ''"/>
              </div>
            </section>
            <RunProgress v-if="selectedRun" :key="`${selectedRun.project_id}:${selectedRun.run_id}`" :run="selectedRun" :refresh-error="runPollError === selectedRun.run_id"/><section v-if="selectedRun" :key="selectedRun.run_id" class="panel run-report"><div class="panel-head"><h2>运行详情</h2><span class="badge">{{ selectedRun.run_id }}</span></div><div class="panel-body"><div class="report-summary"><div><small>执行状态</small><strong>{{ runLabels[selectedRun.status] }}</strong></div><div><small>当前阶段</small><strong>{{ stageLabel(selectedRun.progress?.stage || selectedRun.stage) }}</strong></div><div><small>运行方式</small><strong>{{ selectedRun.run_mode === 'fresh' ? '从零开始' : '继续上次工作' }}</strong></div><div><small>配置修订</small><strong>{{ selectedRun.config_revision ?? '历史记录未提供' }}</strong></div><div><small>结束时间</small><strong>{{ time(selectedRun.finished_at) }}</strong></div></div><p v-if="graphOutcome(selectedRun.report)" class="note">{{ graphOutcome(selectedRun.report) }}</p><p v-if="selectedRun.error_code" class="note amber">{{ new ApiError(selectedRun.error_code).message }}<span v-if="selectedRun.failure_detail"> · {{ workflowFailureLabel(selectedRun.failure_detail) }}</span></p><p v-if="selectedRun.output_path" class="field-hint">本轮成果目录：{{ selectedRun.output_path }}</p><ConstructionReport :report="selectedRun.report"/><RunIssues :report="selectedRun.report"/><TechnicalDetails v-if="selectedRun.report" :value="selectedRun.report" title="报告原始结构"/><p v-else class="note">{{ isActive(selectedRun) ? '运行尚未结束，报告会在后端生成后显示。' : '此运行没有可读取的报告，请结合状态和错误码判断。' }}</p></div></section><RunTrace v-if="selectedRun" :key="`${selectedRun.project_id}:${selectedRun.run_id}`" :run="selectedRun" :config-revision="configRevision"/></template>
          <template v-if="tab === 'settings'"><div class="intro"><div><h2>模型配置</h2></div></div><div class="layout-two"><section class="panel"><div class="panel-head"><h2>模型连接</h2></div><div class="panel-body"><p v-if="config.agent_config" class="note amber">当前配置引用独立模型文件 {{ config.agent_config }}。连接信息在该文件中配置，下方可调整额度和超时；保存时会生成新配置，保留原文件。</p><template v-else><div class="field"><label for="model-mode">模型模式</label><select id="model-mode" v-model="model.mode" :disabled="locked"><option value="disabled">关闭模型，暂不启动自主构建</option><option value="metadata_only">仅元数据，不向模型发送样本</option><option value="in_environment">环境内模型，可使用受限样本</option><option value="approved_external">已批准的外部模型</option></select></div><div class="field"><label for="model-url">服务 Base URL</label><input id="model-url" v-model="model.base_url" :disabled="locked" placeholder="https://model.example.com/v1" autocomplete="off"/><p class="field-hint">使用兼容 Chat Completions 的地址；不要在 URL 中包含密钥。</p></div><div class="two-fields"><div class="field"><label for="model-name">模型名称</label><input id="model-name" v-model="model.model" :disabled="locked" placeholder="模型服务提供的名称"/></div><div class="field"><label for="model-key">API key</label><input id="model-key" v-model="modelKey" :disabled="locked" type="password" autocomplete="new-password" :placeholder="credentials.model ? '已在本机保存，留空保留' : credentials.model_environment ? '服务器已配置，可留空' : '填写模型服务的 API key'"/></div></div><p class="credential-status field-hint"><span class="badge" :class="credentials.model || credentials.model_environment ? 'green' : ''">{{ credentials.model ? '已在本机保存密钥' : credentials.model_environment ? '已配置服务器环境变量' : '尚未配置密钥' }}</span><button v-if="credentials.model || credentials.model_error" class="text-button" :disabled="locked" @click="clearModelCredential">清除密钥</button></p><p v-if="credentials.model_error" class="note amber" role="alert">{{ new ApiError(credentials.model_error).message }}</p><details class="model-advanced"><summary>使用服务器环境变量（可选）</summary><div class="field"><label for="model-key-env">API key 环境变量名</label><input id="model-key-env" v-model="model.api_key_env" :disabled="locked" placeholder="LIGHTGRAPH_API_KEY" autocomplete="off"/></div></details><label v-if="internalHttpEndpoint" class="checkbox-line"><input id="model-allow-http" v-model="model.allow_insecure_http" type="checkbox" :disabled="locked"/>允许使用此内部 HTTP 服务</label></template>
<div class="field"><label for="construction-target">构建结果</label><select id="construction-target" v-model="constructionTarget" :disabled="locked"><option value="instances">生成实例图谱</option><option value="structure">仅生成结构与映射</option></select><p class="field-hint">结构与映射可以单独保存和发布；这不代表具体节点、关系或全部数据已经验证。</p></div>
<div class="field"><label for="max-db-queries">数据查询次数上限</label><label class="checkbox-line"><input id="query-count-unlimited" v-model="queryCountUnlimited" type="checkbox" :disabled="locked"/>不限次数</label><input id="max-db-queries" type="number" min="1" step="1" :value="maxDbQueries ?? ''" :disabled="locked || queryCountUnlimited" placeholder="填写正整数" @input="maxDbQueries = ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value)"/><p class="field-hint">所有数据来源共用，包括元数据与事务控制查询。不限次数仍受单次查询超时、整次运行时限和数据量限制。</p></div>
<fieldset class="resource-settings"><legend>数据读取与构图额度</legend>
<p class="field-hint">累计返回量是本轮所有来源查询合计读取的数据量，包含探查与实例验证；处理后释放内存也不会扣回，不是磁盘存储或模型 token。</p>
<div v-for="field in resourceFields" :key="field.key" class="field"><label :for="field.id">{{ field.label }}</label><label v-if="field.unlimited" class="checkbox-line"><input :id="`${field.id}-unlimited`" v-model="resources[field.key].unlimited" type="checkbox" :disabled="locked"/>不限</label><input :id="field.id" type="number" :min="field.unit === 1 ? 1 : 1 / field.unit" :step="field.unit === 1 ? 1 : 'any'" :placeholder="resources[field.key].unlimited ? '不限' : undefined" :value="resources[field.key].unlimited || resources[field.key].value === null ? '' : resources[field.key].value! / field.unit" :disabled="locked || (field.unlimited && resources[field.key].unlimited)" @input="setResourceLimit(field.key, field.unit, $event)"/></div>
<p class="field-hint">1 MiB = 1,048,576 字节；填写 1024 即 1 GiB。工作内存限制构图时保留的结果与索引，和累计返回量分别生效。不限累计返回量仍保留只读权限、单次查询限制、工作内存与运行时限。</p>
</fieldset>
<p v-if="agentLimitsLoading" role="status">正在读取模型额度…</p>
<div class="two-fields"><div class="field"><label for="model-max-calls">模型调用总额度</label><input id="model-max-calls" type="number" min="0" step="1" :value="limitModel.max_calls === undefined ? modelLimitDefaults.max_calls : limitModel.max_calls ?? ''" :disabled="locked || agentLimitsLoading" placeholder="留空不额外限制" @input="setModelLimit('max_calls', $event)"/><p class="field-hint">整次运行的模型请求次数，包含重试和压缩调用。0 表示不允许调用模型。</p></div><div class="field"><label for="run-seconds">整次运行时限（秒）</label><input id="run-seconds" type="number" min="0.1" step="any" :value="runSeconds ?? ''" :disabled="locked || agentLimitsLoading" @input="runSeconds = ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value)"/><p class="field-hint">同一时限覆盖发现、验证与构建。</p></div></div>
<div class="two-fields"><div class="field"><label for="model-input-tokens">最大输入 tokens</label><input id="model-input-tokens" type="number" min="1" step="1" :value="limitModel.max_prompt_tokens === undefined ? modelLimitDefaults.max_prompt_tokens : limitModel.max_prompt_tokens ?? ''" :disabled="locked || agentLimitsLoading" placeholder="留空不另设输入限制" @input="setModelLimit('max_prompt_tokens', $event)"/><p class="field-hint">请求派发前按估算值检查输入，包括系统提示、历史消息和工具定义。额度估算会结合接口实际用量校准，不代表精确分词结果。</p></div><div class="field"><label for="model-output-tokens">最大输出 tokens</label><input id="model-output-tokens" type="number" min="1" step="1" :value="limitModel.max_output_tokens === undefined ? modelLimitDefaults.max_output_tokens : limitModel.max_output_tokens ?? ''" :disabled="locked || agentLimitsLoading" placeholder="默认 65536" @input="setModelLimit('max_output_tokens', $event)"/><p class="field-hint">单次请求的输出上限，不是整轮抽取的 token 预算。</p></div></div>
<div class="field"><label for="model-timeout-seconds">接口超时时间（秒）</label><input id="model-timeout-seconds" type="number" min="0.1" step="any" :value="limitModel.timeout_seconds === undefined ? modelLimitDefaults.timeout_seconds : limitModel.timeout_seconds ?? ''" :disabled="locked || agentLimitsLoading" placeholder="默认 800" @input="setModelLimit('timeout_seconds', $event)"/></div>
<div class="actions"><button class="btn" :disabled="locked || agentLimitsLoading" @click="useDefaultLimits">使用默认额度</button><span class="field-hint">填入默认值后点击保存。已有配置不会自动覆盖。</span></div>
<p class="field-hint">配置保存成功后，下一次启动立即使用新值，无需重启服务。运行中的任务使用启动时的配置。</p>
<details class="optional-model-settings"><summary>更多上下文选项</summary><div class="field"><label for="model-context-tokens">模型总上下文窗口（tokens，可选）</label><input id="model-context-tokens" type="number" min="1" step="1" :value="limitModel.context_window_tokens ?? ''" :disabled="locked || agentLimitsLoading" placeholder="留空不额外限制" @input="setModelLimit('context_window_tokens', $event)"/><p class="field-hint">同时设置时，输入上限取“最大输入”与“总窗口减最大输出”中的较小值。</p></div>
<p class="field-hint">工作上下文默认在约 32768 tokens 时自动压缩，完整会话仍保留。此阈值与最大输入额度分别生效。</p></details>
<p class="field-hint" data-testid="effective-input-tokens">当前有效输入限制：{{ effectiveInputTokens === null ? '未额外设置' : `${effectiveInputTokens} tokens` }}。</p>
<p class="field-hint">模型输入、返回及工具结果保留在本地，供排查与恢复使用；运行后需手动清理。</p>
<button v-if="!traceRecording" class="btn" :disabled="locked" @click="traceRecording = true">启用运行轨迹</button>
<p v-if="limitsError" class="note amber" role="alert">{{ limitsError }}</p>
<fieldset class="resource-settings embedding-settings">
  <legend>可替换语义向量服务（Embedding）</legend>
  <p class="field-hint">用于子图语义召回。支持本地启动 <code>lightgraph embed-server --model Qwen/Qwen3-Embedding-0.6B</code>，或随时替换为 Ollama / vLLM / 外部兼容 OpenAI <code>/v1/embeddings</code> 服务。</p>
  <div class="field">
    <label for="embedding-provider">向量服务类型</label>
    <select id="embedding-provider" v-model="embedding.provider" :disabled="locked">
      <option value="none">仅用关键词和图关系</option>
      <option value="openai_compatible">可替换本地/远端向量服务（OpenAI /v1/embeddings 协议，如 Qwen3-Embedding-0.6B）</option>
    </select>
  </div>
  <template v-if="embedding.provider === 'openai_compatible'">
    <div class="two-fields">
      <div class="field">
        <label for="embedding-base-url">向量服务 Base URL</label>
        <input id="embedding-base-url" v-model="embedding.base_url" :disabled="locked" placeholder="http://127.0.0.1:8002/v1" autocomplete="off"/>
      </div>
      <div class="field">
        <label for="embedding-model">向量模型名称</label>
        <input id="embedding-model" v-model="embedding.model" :disabled="locked" placeholder="Qwen/Qwen3-Embedding-0.6B"/>
      </div>
    </div>
    <div class="two-fields">
      <div class="field">
        <label for="embedding-key">向量服务 API Key（本地服务可留空）</label>
        <input id="embedding-key" v-model="embeddingKey" :disabled="locked" type="password" autocomplete="new-password" :placeholder="embeddingCredStatus.embedding ? '已在本机保存，留空保留' : embeddingCredStatus.embedding_environment ? '服务器环境变量已配置，可留空' : '本地无鉴权服务可留空'"/>
      </div>
      <div class="field">
        <label for="embedding-dimensions">输出向量维度（留空自动识别）</label>
        <input id="embedding-dimensions" type="number" min="16" step="1" :value="embedding.dimensions ?? ''" :disabled="locked" placeholder="例如 1024（自动识别）" @input="embedding.dimensions = ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value)"/>
      </div>
    </div>
    <p class="credential-status field-hint">
      <span class="badge" :class="embeddingCredStatus.embedding || embeddingCredStatus.embedding_environment ? 'green' : ''">{{ embeddingCredStatus.embedding ? '已保存本机向量密钥' : embeddingCredStatus.embedding_environment ? '已配置环境变量' : '无密钥（本地服务免密）' }}</span>
      <button v-if="embeddingCredStatus.embedding || embeddingCredStatus.embedding_error" class="text-button" :disabled="locked" @click="clearEmbeddingCredential">清除向量密钥</button>
    </p>
    <div class="field">
      <label for="embedding-instruction">检索查询指令（仅作用于 Query，如 Qwen3-Embedding Instruct 前缀，可选）</label>
      <input id="embedding-instruction" v-model="embedding.query_instruction" :disabled="locked" placeholder="例如：Given a web search query, retrieve relevant passages that answer the query"/>
    </div>
    <div class="two-fields">
      <div class="field">
        <label for="embedding-batch">批处理大小</label>
        <input id="embedding-batch" type="number" min="1" max="512" step="1" :value="embedding.batch_size ?? 32" :disabled="locked" @input="embedding.batch_size = ($event.target as HTMLInputElement).value === '' ? 32 : Number(($event.target as HTMLInputElement).value)"/>
      </div>
      <div class="field">
        <label for="embedding-timeout">单批请求超时（秒）</label>
        <input id="embedding-timeout" type="number" min="1" step="any" :value="embedding.timeout_seconds ?? defaultEmbeddingTimeout" :disabled="locked" @input="embedding.timeout_seconds = ($event.target as HTMLInputElement).value === '' ? defaultEmbeddingTimeout : Number(($event.target as HTMLInputElement).value)"/>
      </div>
    </div>
    <label v-if="internalEmbeddingHttp" class="checkbox-line">
      <input id="embedding-allow-http" v-model="embedding.allow_insecure_http" type="checkbox" :disabled="locked"/>
      <span>允许连接此非本机 HTTP 向量服务</span>
    </label>
  </template>
  <div class="actions embedding-actions">
    <button class="btn small" type="button" :disabled="locked" @click="fillQwenEmbeddingPreset">填入本地 Qwen3-0.6B 默认参数</button>
    <button v-if="embedding.provider === 'openai_compatible'" class="btn small" type="button" :disabled="embeddingTesting || busy" @click="testEmbeddingEndpoint">{{ embeddingTesting ? '测试中…' : '测试向量服务连通性' }}</button>
  </div>
  <p v-if="embeddingTestResult" class="note">
    向量服务连通成功 · 模型：<strong>{{ embeddingTestResult.model }}</strong> · 向量维度：<strong>{{ embeddingTestResult.dimensions }}</strong> · 耗时：{{ embeddingTestResult.latency_ms }} ms
  </p>
</fieldset>
</div><div class="panel-footer"><span>API key 加密保存在服务端本机，刷新后无需重填</span><button class="btn primary" :disabled="locked || agentLimitsLoading || !!limitsError" @click="action(saveModelLimits)">保存配置</button></div></section><aside class="panel"><div class="panel-head"><h2>运行前检查</h2></div><div class="panel-body"><div class="setup-check"><strong>{{ config.sources?.length || 0 }} 个数据来源</strong><button class="text-button" @click="tab = 'inputs'">配置数据接入</button></div><div class="setup-check"><strong>{{ documentFiles.length }} 个知识文档</strong><button class="text-button" @click="tab = 'documents'">配置知识文档</button></div><div class="setup-check"><strong>{{ purpose.trim() ? '已填写抽取目标' : '尚未填写抽取目标' }}</strong><button class="text-button" @click="tab = 'goal'">配置抽取目标</button></div><div class="setup-check"><strong>{{ workflowConfig?.enabled ? `并行工作流（${workflowConfig.units.length} 条链路）` : '单链路运行模式' }}</strong><button class="text-button" @click="tab = 'workflow'">编排并行工作流</button></div></div></aside></div><details class="panel advanced-panel"><summary class="panel-head">高级配置（可选）</summary><div class="panel-head"><h2>完整项目配置</h2><span class="badge">JSON 编辑 · 服务端保存 YAML</span></div><div class="panel-body"><p class="field-hint editor-hint">此处与上方表单同步；两处修改不同字段时一并保存，同一字段有冲突时会提示。</p><label class="sr-only" for="advanced-config">完整项目 JSON 配置</label><textarea id="advanced-config" v-model="advanced" class="code-editor" :disabled="locked" spellcheck="false"/></div><div class="panel-footer"><span>完整配置不接受明文密码或 API key，请使用上方连接表单</span><div class="actions"><button v-if="advancedDirty && (formDirty || agentLimitsDirty)" class="btn" :disabled="locked" @click="discardFormEdits">放弃表单修改</button><button v-if="advancedDirty" class="btn" :disabled="locked" @click="resetAdvanced">放弃修改</button><button class="btn" :disabled="locked" @click="action(async () => { advanced = JSON.stringify(advancedConfig(), null, 2) })">格式化 JSON</button><button class="btn primary" :disabled="locked" @click="saveAdvanced">保存完整配置</button></div></div></details></template>
        </div>
      </template>
    </main>
    <DatabaseSourceDialog v-if="databaseOpen" ref="databaseDialog" :project-id="project?.project_id || ''" :source="databaseSource" :sources="config.sources || []" :files="project?.files || []" :retention-verified="config.deployment_retention_verified === true" :config-revision="configRevision" :busy="busy" :error="error" @close="closeDatabaseSource" @save="saveDatabaseSources" @connected="databaseConnected" @capability-uploaded="capabilityUploaded"/>
    <div v-if="newProjectOpen" class="modal-backdrop" @click.self="!busy && (newProjectOpen = false)"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="new-project-title"><form @submit.prevent="createProject"><div class="panel-head"><h2 id="new-project-title">新建项目</h2><button type="button" class="close" aria-label="关闭" :disabled="busy" @click="newProjectOpen = false">×</button></div><div class="panel-body"><p v-if="error" class="note amber" role="alert">{{ error }}</p><div class="field"><label for="new-name">项目名称</label><input id="new-name" v-model="newName" required maxlength="120" autofocus placeholder="例如：数据关系图谱"/></div><div class="field"><label for="new-id">项目编号（可选）</label><input id="new-id" v-model="newId" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="留空由服务端生成"/><p class="field-hint">用于 workspace 下的隔离文件夹，创建后保持不变。</p></div></div><div class="panel-footer"><span>文件夹不存在时会自动创建</span><button class="btn primary" :disabled="busy || !newName.trim()">创建项目</button></div></form></section></div>
    <div v-if="preview" class="modal-backdrop" @click.self="preview = null"><section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="preview-title"><div class="panel-head"><h2 id="preview-title">{{ preview.name }}</h2><button class="close" aria-label="关闭" @click="preview = null">×</button></div><div class="panel-body"><pre class="code file-preview">{{ preview.content }}</pre></div></section></div>
    <ConfirmDelete v-if="deletion" :title="deletionTitle" :description="deletionDescription" :detail="deletionDetail" :busy="busy" :locked="deletion.kind === 'run' ? busy : locked" :error="deletionError" @cancel="deletion = null" @confirm="confirmDeletion"/>
    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </div>
</template>

<style scoped>
.graph-view-options { margin-bottom: 12px; border: 1px solid var(--line); border-radius: 10px; background: #fff; }
.graph-view-options > summary { padding: 10px 16px; cursor: pointer; font-size: 13px; }
.graph-view-options > summary strong { font-weight: 600; }
.graph-view-options > summary span { margin-left: 12px; color: #52617a; overflow-wrap: anywhere; }
.graph-options-body { border-top: 1px solid var(--line); padding: 14px 16px; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(270px, 100%), 1fr)); align-items: start; gap: 10px 20px; }
.graph-candidates-toggle { align-self: end; }
.graph-options-body :deep(.business-context-select) { margin: 0; }
.graph-options-body :deep(.business-context-select label) { display: grid; gap: 5px; font-size: 12px; }
.graph-options-body :deep(.business-context-select select) { width: 100%; min-width: 0; }
.resource-settings { min-width: 0; margin: 0 0 24px; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }
.resource-settings legend { padding: 0 6px; font-weight: 600; }
.resource-settings > .field-hint { margin-bottom: 16px; }
.resource-settings > .field-hint:last-child { margin-bottom: 0; }
</style>

<style scoped>
.run-history-table { max-height: 15rem; overflow: auto; }
.run-history-table .chosen { background: #ecf3fc; }
.run-history-table th { position: sticky; top: 0; background: #f7f9fc; }
.run-history-table td:last-child { white-space: nowrap; }
</style>
