<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import StructurePreview from './StructurePreview.vue'
import RunIssues from './RunIssues.vue'
import ConstructionReport from './ConstructionReport.vue'
import TechnicalDetails from './TechnicalDetails.vue'
import RunProgress from './RunProgress.vue'
import RunTrace from './RunTrace.vue'
import { effectiveInputTokenLimit, modelLimitError, modelLimitKeys } from './modelLimits'
import { configTextChanged, mergeConfigDraft } from './configDraft'
import type { ModelLimitKey } from './modelLimits'
import IdentityCandidates from './IdentityCandidates.vue'
import DiscoveryCandidates from './DiscoveryCandidates.vue'
import DatabaseSourceDialog from './DatabaseSourceDialog.vue'
import DataInputs from './DataInputs.vue'
import ConfirmDelete from './ConfirmDelete.vue'
import { goalMarkdown } from './goalMarkdown'
import { configurationTabs, resultTabs, isResultTab } from './onboarding'
import type { ConfigurationTab, ResultTab, WorkbenchTab } from './onboarding'
import { ApiError, ensureSession, clearSessions, clearSession, json, projectPath, request, responseRevision } from './api'
import { assertNoCredentials, graphOutcome, isActive, runLabels, stageLabel } from './state'
import { WorkbenchLocation } from './workbenchLocation'
import type { DocumentScope, Graph, Metadata, Project, ProjectFile, Run, RunConfig, SourceConfig } from './types'


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
const runSeconds = ref<number | null>(1800)
const maxDbQueries = ref<number | null>(null)
const queryCountUnlimited = ref(true)
const constructionTarget = ref<'instances' | 'structure'>('instances')
const effectiveQueryLimit = computed(() => queryCountUnlimited.value ? null : maxDbQueries.value)
const queryLimitError = computed(() => !queryCountUnlimited.value && (maxDbQueries.value === null || !Number.isSafeInteger(maxDbQueries.value) || maxDbQueries.value <= 0) ? '查询次数上限须为正整数，或勾选不限次数。' : '')
const traceRecording = ref(true)
const model = ref<NonNullable<RunConfig['model']>>({})
const internalHttpEndpoint = computed(() => {
  try {
    const url = new URL(String(model.value.base_url || '').trim())
    return url.protocol === 'http:' && !['127.0.0.1', 'localhost', '::1', '[::1]'].includes(url.hostname)
  } catch { return false }
})
const agentLimits = ref<Record<string, unknown>>({}), agentLimitsSaved = ref('{}'), agentLimitsLoading = ref(false)
const agentLimitsDirty = computed(() => !!config.value.agent_config && JSON.stringify(agentLimits.value) !== agentLimitsSaved.value)
const limitModel = computed(() => config.value.agent_config ? agentLimits.value : model.value)
const limitsError = computed(() => !traceRecording.value ? '运行需要保存轨迹，请先启用后保存。' : runSeconds.value === null || !Number.isFinite(runSeconds.value) || runSeconds.value <= 0 ? '整次运行时限须大于 0。' : queryLimitError.value || modelLimitError(limitModel.value))
const effectiveInputTokens = computed(() => effectiveInputTokenLimit(limitModel.value))
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
  if (formDirty.value || modelKey.value || advancedDirty.value) await saveForms()
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
type CredentialStatus = { database_sources: string[]; model: boolean; model_error?: string; model_environment?: boolean }
const credentials = ref<CredentialStatus>({ database_sources: [], model: false })
const metadata = ref<Metadata>({ tables: [], nodes: [], edges: [] })
const business = ref<Graph>({ nodes: [], edges: [] })
const structurePreviewOpen = ref(false)
const graphLoading = ref(false), graphError = ref('')
let graphRequestEpoch = 0
const runs = ref<Run[]>([])
const selectedRun = ref<Run | null>(null)
const runPollError = ref<string | null>(null)
const newProjectOpen = ref(false), newName = ref(''), newId = ref('')
const databaseOpen = ref(false), databaseSource = ref<SourceConfig | null>(null)
const databaseDialog = ref<{ completeDraft: () => Promise<void> } | null>(null)
const workbenchLocation = new WorkbenchLocation()
const uploadInput = ref<HTMLInputElement | null>(null)
const documentInput = ref<HTMLInputElement | null>(null)
const bindingFile = ref<ProjectFile | null>(null)
const bindingPurpose = ref('unresolved')
const bindingMode = ref('global')
const bindingTables = ref<string[]>([])
const bindingScope = ref<DocumentScope>({ source_ids: [], tables: [], columns: [] })
type DeletionTarget = { kind: 'project'; id: string; name: string } | { kind: 'file'; file: ProjectFile } | { kind: 'source'; source: SourceConfig } | { kind: 'table'; source: SourceConfig; table: string }
const deletion = ref<DeletionTarget | null>(null), deletionError = ref('')
const preview = ref<{ name: string; content: string } | null>(null)
const purposeLabels: Record<string, string> = { data_description: '数据说明', analysis_knowledge: '分析知识', mixed: '混合文档', unresolved: '待分类' }
let generation = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
let toastTimer: ReturnType<typeof setTimeout> | undefined
const activeRun = computed(() => runs.value.find(isActive))
const locked = computed(() => busy.value || loading.value || !!activeRun.value)
const path = computed(() => projectPath(project.value?.project_id || ''))
const queryLimitDirty = computed(() => effectiveQueryLimit.value !== (config.value.max_db_queries ?? null) || queryCountUnlimited.value !== (config.value.max_db_queries == null))
const formDirty = computed(() => traceRecording.value !== (config.value.record_trace !== false) || runSeconds.value !== (config.value.max_run_seconds ?? 1800) || queryLimitDirty.value || constructionTarget.value !== (config.value.construction_target ?? 'instances') || purpose.value !== goalMarkdown(config.value) || JSON.stringify(model.value) !== JSON.stringify(config.value.model || {}))
const dirty = computed(() => !!modelKey.value || formDirty.value || agentLimitsDirty.value)
const advancedDirty = computed(() => configTextChanged(advanced.value, advancedBaseline.value))
function previewConfig(): RunConfig {
  const value = { ...config.value }
  if (purpose.value !== goalMarkdown(config.value)) value.goal_markdown = purpose.value
  if (runSeconds.value !== (config.value.max_run_seconds ?? 1800)) value.max_run_seconds = runSeconds.value!
  if (effectiveQueryLimit.value !== (config.value.max_db_queries ?? null)) value.max_db_queries = effectiveQueryLimit.value
  if (constructionTarget.value !== (config.value.construction_target ?? 'instances')) value.construction_target = constructionTarget.value
  if (traceRecording.value !== (config.value.record_trace !== false)) value.record_trace = traceRecording.value
  if (!value.agent_config && JSON.stringify(model.value) !== JSON.stringify(config.value.model || {})) value.model = { ...model.value }
  return value
}
function resetAdvanced() { advanced.value = advancedBaseline.value = JSON.stringify(previewConfig(), null, 2) }
watch([purpose, runSeconds, effectiveQueryLimit, constructionTarget, traceRecording, model], () => {
  // The JSON view follows the form until the user edits the JSON itself.
  if (!advancedDirty.value) resetAdvanced()
}, { deep: true })
const deletionTitle = computed(() => deletion.value?.kind === 'project' ? '删除项目' : deletion.value?.kind === 'source' ? '删除数据库来源' : deletion.value?.kind === 'table' ? '移除数据表' : '删除文件')
const deletionDescription = computed(() => {
  const target = deletion.value
  if (!target) return ''
  if (target.kind === 'project') return `删除项目“${target.name}”及其文件、配置、运行记录和图谱结果？此操作无法撤销。`
  if (target.kind === 'file') return `删除项目中的“${target.file.name}”及其相关配置和元数据？已有运行记录与图谱结果保留。`
  if (target.kind === 'source') return `移除来源“${target.source.id}”、本机保存的连接信息及相关配置和元数据？外部数据库和已有图谱结果保留。`
  return `从来源“${target.source.id}”移除“${target.table}”？外部数据表和已有图谱结果保留。`
})
const deletionDetail = computed(() => deletion.value?.kind === 'project' && (dirty.value || advancedDirty.value) ? '此项目尚未保存的修改也会被删除。' : deletion.value?.kind === 'source' || deletion.value?.kind === 'table' || deletion.value?.kind === 'file' && ['excel', 'csv'].includes(deletion.value.file.kind) ? '失去绑定范围的文档会暂停参与抽取，可重新绑定。' : '')
const documentFiles = computed(() => project.value?.files.filter(f => f.kind === 'markdown') || [])
const time = (value?: string) => value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' }).format(new Date(value)) : '—'
const size = (value: number) => value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`
const tableKey = (table: Metadata['tables'][number]) => `${table.source_id}.${table.schema}.${table.name}`
function notify(message: string) { toast.value = message; clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.value = '' }, 4500) }
function report(exception: unknown) { if (exception instanceof ApiError && ['config_revision_conflict', 'model_credential_binding_conflict'].includes(exception.code)) configurationConflict.value = true; error.value = exception instanceof Error ? exception.message : '操作未完成，请重试。' }
function fillForms(value: RunConfig) {
  configRevision.value = responseRevision(value) || configRevision.value
  configurationConflict.value = false
  config.value = value
  purpose.value = goalMarkdown(value)
  runSeconds.value = value.max_run_seconds ?? 1800
  queryCountUnlimited.value = value.max_db_queries == null
  maxDbQueries.value = value.max_db_queries ?? null
  constructionTarget.value = value.construction_target ?? 'instances'
  traceRecording.value = value.record_trace !== false
  model.value = JSON.parse(JSON.stringify(value.model || {})); advanced.value = advancedBaseline.value = JSON.stringify(value, null, 2)
}
function formConfig(): RunConfig {
  const problem = limitsError.value
  if (problem) throw new Error(problem)
  const value: RunConfig = { ...config.value, record_trace: traceRecording.value, max_run_seconds: runSeconds.value!, max_db_queries: effectiveQueryLimit.value, construction_target: constructionTarget.value, goal_markdown: purpose.value }
  if (!value.agent_config) value.model = { ...model.value }
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
async function loadProject(id: string) {
  if (project.value?.project_id === id && !loading.value) return
  if (project.value && project.value.project_id !== id && (dirty.value || advancedDirty.value) && !window.confirm('当前项目有未保存的配置修改。切换项目会放弃这些修改，继续吗？')) return
  workbenchLocation.selectProject(id)
  const epoch = ++generation
  clearTimeout(pollTimer); loading.value = true; error.value = ''; selectedRun.value = null; runPollError.value = null; preview.value = null; bindingFile.value = null; databaseOpen.value = false; modelKey.value = ''; credentials.value = { database_sources: [], model: false }
  const p = projectPath(id)
  try {
    const values = await Promise.all([request<Project>(p), request<RunConfig>(`${p}/config`)])
    if (epoch !== generation) return
    configRevision.value = undefined; project.value = values[0]; fillForms(values[1]); await ensureSession(id); metadata.value = { tables: [], nodes: [], edges: [] }; business.value = { nodes: [], edges: [] }; runs.value = []
    // An unavailable database must not prevent opening and repairing its project configuration.
    // Graph loading can take longer than opening configuration. A graph read
    // must not lock model fields or report a successful config load as failed.
    void loadBusinessGraph(p, epoch)
    const details = await Promise.allSettled([request<Metadata>(`${p}/metadata`), request<{ runs: Run[] }>(`${p}/runs`), request<CredentialStatus>(`${p}/credentials`)])
    if (epoch !== generation) return
    if (details[0].status === 'fulfilled') { metadata.value = details[0].value }
    if (details[1].status === 'fulfilled') runs.value = details[1].value.runs
    if (details[2].status === 'fulfilled') credentials.value = details[2].value
    const failed = details.find(result => result.status === 'rejected')
    if (failed?.status === 'rejected') report(failed.reason)
    selectedRun.value = runs.value[0] || null
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
    runSeconds: runSeconds.value !== (config.value.max_run_seconds ?? 1800) ? runSeconds.value : undefined,
    maxDbQueries: queryLimitDirty.value ? { value: effectiveQueryLimit.value, unlimited: queryCountUnlimited.value } : undefined,
    constructionTarget: constructionTarget.value !== (config.value.construction_target ?? 'instances') ? constructionTarget.value : undefined,
    purpose: purpose.value !== goalMarkdown(config.value) ? purpose.value : undefined,
    model: JSON.stringify(model.value) !== JSON.stringify(config.value.model || {}) ? JSON.parse(JSON.stringify(model.value)) as typeof model.value : undefined,
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
    if (edits.constructionTarget !== undefined) constructionTarget.value = edits.constructionTarget
    if (edits.purpose !== undefined) purpose.value = edits.purpose
    if (edits.model !== undefined && !agentConfigRemoved) model.value = edits.model
  }
  projects.value = projects.value.map(p => p.project_id === nextProject.project_id ? nextProject : p)
}
async function refreshProjectConfig(preserveEdits = false) {
  const values = await Promise.all([request<Project>(path.value), request<RunConfig>(`${path.value}/config`)])
  applyProjectConfig(values[0], values[1], preserveEdits)
}
async function refreshGraphs() {
  const p = path.value, epoch = generation, requestEpoch = ++graphRequestEpoch
  graphLoading.value = true; graphError.value = ''
  try {
    const values = await Promise.all([request<Metadata>(`${p}/metadata`), request<Graph>(`${p}/graph?layer=business&include_candidates=${includeCandidates.value}&limit=1000`)])
    if (epoch === generation && requestEpoch === graphRequestEpoch) { metadata.value = values[0]; business.value = values[1] }
  } finally { if (epoch === generation && requestEpoch === graphRequestEpoch) graphLoading.value = false }
}
async function loadBusinessGraph(p = path.value, epoch = generation) {
  const requestEpoch = ++graphRequestEpoch
  graphLoading.value = true; graphError.value = ''
  try {
    const graph = await request<Graph>(`${p}/graph?layer=business&include_candidates=${includeCandidates.value}&limit=1000`)
    if (epoch === generation && requestEpoch === graphRequestEpoch) business.value = graph
  } catch (exception) {
    if (epoch === generation && requestEpoch === graphRequestEpoch) graphError.value = exception instanceof Error ? exception.message : '图谱读取失败，请重试。'
  } finally { if (epoch === generation && requestEpoch === graphRequestEpoch) graphLoading.value = false }
}
function schedulePoll(epoch: number) {
  clearTimeout(pollTimer)
  if (epoch !== generation || !activeRun.value) return
  pollTimer = setTimeout(async () => {
    const runningId = activeRun.value?.run_id
    if (!runningId) return
    try {
      const run = await request<Run>(`${path.value}/runs/${encodeURIComponent(runningId)}`)
      if (epoch !== generation) return
      runPollError.value = null
      runs.value = runs.value.map(item => item.run_id === run.run_id ? run : item)
      if (selectedRun.value?.run_id === run.run_id) selectedRun.value = run
      if (!isActive(run)) {
        try { await refreshGraphs() } catch (exception) { if (epoch === generation) report(exception) }
        if (epoch === generation) notify(graphOutcome(run.report) || `运行${runLabels[run.status]}，请查看结果与报告。`)
      }
    } catch { if (epoch === generation) runPollError.value = runningId }
    schedulePoll(epoch)
  }, 1000)
}
async function refreshCredentials() {
  credentials.value = await request<CredentialStatus>(`${path.value}/credentials`)
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
type SavedModelBinding = { path: string; revision?: string; expectedModel: { base_url: string; model: string } }
async function persistConfiguration(value: RunConfig): Promise<SavedModelBinding> {
  const projectPath = path.value
  const saved = await request<RunConfig>(`${projectPath}/config`, { method: 'PUT', headers: revisionHeaders(), body: json(value) })
  // Capture the write response before applying reactive state. A later refresh
  // must not retarget the key that belongs to this configuration write.
  const binding = { path: projectPath, revision: responseRevision(saved), expectedModel: { base_url: saved.model?.base_url || '', model: saved.model?.model || '' } }
  fillForms(saved)
  return binding
}
async function saveForms() {
  const value = advancedDirty.value ? mergeConfigDraft(JSON.parse(advancedBaseline.value), previewConfig(), advancedConfig()) : formConfig()
  const saved = await persistConfiguration(value)
  await saveModelCredential(saved)
}
async function saveModelCredential(saved: SavedModelBinding) {
  if (modelKey.value) {
    const key = modelKey.value
    try {
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
  if (locked.value) return
  if (target.kind !== 'project' && advancedDirty.value) {
    error.value = '完整项目配置有未保存修改，请先保存或放弃，再删除数据。'
    return
  }
  deletionError.value = ''; deletion.value = target
}
async function confirmDeletion() {
  const target = deletion.value, id = project.value?.project_id
  if (!target || !id || locked.value) return
  busy.value = true; deletionError.value = ''; error.value = ''
  let deleted = false
  try {
    await ensureSession(id)
    const base = projectPath(id)
    const endpoint = target.kind === 'project' ? base : target.kind === 'file' ? `${base}/files/${encodeURIComponent(target.file.file_id)}` : `${base}/sources/${encodeURIComponent(target.source.id)}${target.kind === 'table' ? '/tables' : ''}`
    const result = await request<{ project?: Project; config?: RunConfig; detached_rule_paths?: string[] }>(endpoint, { method: 'DELETE', headers: revisionHeaders(), ...(target.kind === 'table' ? { body: json({ table: target.table }) } : {}) })
    configRevision.value = responseRevision(result) || configRevision.value
    deleted = true; deletion.value = null
    const message = target.kind === 'project' ? '项目已删除' : target.kind === 'table' ? '数据表已移除' : target.kind === 'source' ? '数据库来源已删除' : '文件已删除'
    notify(message + (result.detached_rule_paths?.length ? `，已停用 ${result.detached_rule_paths.length} 个关联规则文件` : ''))
    if (target.kind === 'project') {
      // Invalidate old requests and polling before selecting another project.
      generation++; clearTimeout(pollTimer); clearSession(id); workbenchLocation.removeProject(id)
      projects.value = projects.value.filter(item => item.project_id !== id)
      project.value = null; fillForms({}); modelKey.value = ''; credentials.value = { database_sources: [], model: false }
      metadata.value = { tables: [], nodes: [], edges: [] }; business.value = { nodes: [], edges: [] }; runs.value = []; selectedRun.value = null
      databaseOpen.value = false; databaseSource.value = null; bindingFile.value = null; preview.value = null
      tab.value = 'inputs'; configurationTab.value = 'inputs'; resultTab.value = 'runs'
      await reloadProjects()
    } else {
      if (target.kind === 'file') { if (bindingFile.value?.file_id === target.file.file_id) bindingFile.value = null; preview.value = null }
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
function openBinding(file: ProjectFile) {
  bindingFile.value = file; bindingPurpose.value = file.purpose || 'unresolved'; bindingScope.value = JSON.parse(JSON.stringify(file.scope || { source_ids: [], tables: [], columns: [] }))
  bindingTables.value = [...bindingScope.value.tables]; bindingMode.value = Object.values(bindingScope.value).some(values => values.length) ? 'tables' : 'global'
}
async function saveBinding() {
  await action(async () => {
    if (!bindingFile.value) return
    if (dirty.value || advancedDirty.value) await saveDraft()
    if (bindingMode.value === 'tables' && !bindingTables.value.length) throw new Error('请至少选择一张表，或改为全局使用。')
    const scope = bindingMode.value === 'global' ? { source_ids: [], tables: [], columns: [] } : { ...bindingScope.value, source_ids: [], tables: [...bindingTables.value] }
    await request<ProjectFile>(`${path.value}/files/${encodeURIComponent(bindingFile.value.file_id)}/binding`, { method: 'PATCH', headers: revisionHeaders(), body: json({ purpose: bindingPurpose.value, scope }) })
    bindingFile.value = null; await refreshProjectConfig(); notify('文档用途和使用范围已保存')
  })
}
async function refreshMetadata() {
  await action(async () => { if (dirty.value || advancedDirty.value) await saveDraft(); metadata.value = await request<Metadata>(`${path.value}/metadata/refresh`, { method: 'POST', body: '{}' }); notify('元数据已更新') })
}
async function startRun() {
  await action(async () => {
    await saveDraft()
    const run = await request<Run>(`${path.value}/runs`, { method: 'POST', headers: revisionHeaders(), body: '{}' })
    runPollError.value = null
    runs.value = [run, ...runs.value.filter(item => item.run_id !== run.run_id)]; selectedRun.value = run; tab.value = 'runs'; schedulePoll(generation)
  })
}
async function cancelRun(run: Run) {
  await action(async () => { const result = await request<Run>(`${path.value}/runs/${encodeURIComponent(run.run_id)}/cancel`, { method: 'POST', body: '{}' }); runs.value = runs.value.map(item => item.run_id === result.run_id ? result : item); selectedRun.value = result; schedulePoll(generation); if (!isActive(result)) await refreshGraphs() })
}
async function inspectRun(run: Run) {
  const epoch = generation
  await action(async () => {
    const current = await request<Run>(`${path.value}/runs/${encodeURIComponent(run.run_id)}`)
    if (epoch !== generation) return
    selectedRun.value = current
    if (runPollError.value === run.run_id) runPollError.value = null
  })
}
function showRunProgress(run: Run) { selectedRun.value = run; tab.value = 'runs' }
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
    fillForms(await request<RunConfig>(`${path.value}/config`, { method: 'PUT', headers: revisionHeaders(), body: json(value) }))
    await databaseDialog.value?.completeDraft()
    closeDatabaseSource(); await refreshCredentials(); notify('数据库来源已保存，点击读取元数据可连接服务器。')
  })
}
function capabilityUploaded(file: ProjectFile, revision?: string) {
  configRevision.value = revision || configRevision.value
  if (project.value) project.value.files = [...project.value.files.filter(item => item.file_id !== file.file_id), file]
}
watch(includeCandidates, () => { void action(refreshGraphs) })
onMounted(async () => { try { await reloadProjects() } catch (exception) { report(exception); loading.value = false } })
onUnmounted(() => { generation++; clearTimeout(pollTimer); clearTimeout(toastTimer); modelKey.value = ''; clearSessions() })
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
            <div class="actions"><span v-if="dirty || advancedDirty" class="badge amber">有未保存修改</span><button class="btn primary" :disabled="locked || agentLimitsLoading || !!limitsError || (!config.sources?.length && !documentFiles.length && !advancedDirty) || (!purpose.trim() && !advancedDirty)" :title="!purpose.trim() ? '请先填写抽取目的' : '保存配置后启动抽取'" @click="startRun">{{ activeRun ? '正在抽取' : dirty || advancedDirty ? '保存并开始抽取' : '开始抽取' }}</button></div>
          </div>
          <nav class="tabs" :aria-label="inResults ? '运行与结果' : '项目配置'"><button v-for="item in tabs" :key="item.id" class="tab" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.label }}<span v-if="item.id === 'runs' && runs.length" class="count">{{ runs.length }}</span></button></nav>
        </section>
        <div id="phase-content" class="content" :class="{ 'inputs-content': tab === 'inputs' }" role="tabpanel" :aria-labelledby="inResults ? 'results-phase' : 'configuration-phase'">
          <div v-if="activeRun" class="run-banner"><span class="spinner tiny"></span><div><strong>{{ runLabels[activeRun.status] }}</strong><span>{{ stageLabel(activeRun.progress?.stage || activeRun.stage) }} · {{ activeRun.run_id }}</span></div><div class="actions"><button class="btn small" @click="showRunProgress(activeRun)">查看进度与日志</button><button class="btn small" :disabled="busy" @click="cancelRun(activeRun)">取消运行</button></div></div>
          <template v-if="tab === 'goal'">
            <div class="intro"><div><h2>这次希望发现什么？</h2></div></div>
            <div class="layout-two"><section class="panel"><div class="panel-head"><h2>抽取目标</h2><span class="badge">Markdown</span></div><div class="panel-body"><div class="field"><label for="purpose">目标与要求</label><textarea id="purpose" v-model="purpose" :disabled="locked" class="goal-input" maxlength="64000" placeholder="# 本次目标&#10;&#10;说明希望发现的对象、属性和关系。&#10;&#10;## 补充说明&#10;&#10;可填写业务定义、关注范围、排除内容和验收要求。"/><p class="field-hint">目标与知识材料统一使用 Markdown。目标直接指导本次构建，知识文档按分析需要读取；文字内容不会改变数据接入权限。</p></div><p v-if="config.goal?.entity_kinds?.length || config.goal?.relation_kinds?.length" class="note">当前配置还有限定类型：{{ [...(config.goal.entity_kinds || []), ...(config.goal.relation_kinds || [])].join('、') }}。这些程序约束保留在完整配置中，编辑 Markdown 不会自动解除。</p></div><div class="panel-footer"><span>保存后保留 Markdown 原文；下次运行使用已保存修订</span><button class="btn primary" :disabled="locked" @click="action(async () => { await saveForms(); notify('抽取目标已保存') })">保存目标</button></div></section><aside class="stack"><section class="panel"><div class="panel-head"><h2>知识材料</h2></div><div class="panel-body"><p>已有 {{ documentFiles.length }} 份 Markdown 文档。可补充数据字典、业务说明或关系定义，不必预先编写抽取规则。</p><button class="btn" @click="tab = 'documents'">管理知识文档</button></div></section><section class="panel"><div class="panel-head"><h2>项目概况</h2></div><div class="panel-body"><div class="scope-row"><span>数据来源</span><strong>{{ config.sources?.length || 0 }}</strong></div><div class="scope-row"><span>已读取的表</span><strong>{{ metadata.tables.length }}</strong></div><div class="scope-row"><span>业务图节点</span><strong>{{ business.total_nodes ?? business.nodes.length }}</strong></div></div></section></aside></div>
          </template>
          <template v-if="tab === 'inputs'">
            <input ref="uploadInput" type="file" hidden multiple accept=".xlsx,.csv" @change="upload"/>
            <DataInputs :key="project.project_id" :project-id="project.project_id" :sources="config.sources || []" :files="project.files" :metadata="metadata" :locked="locked" :retention-verified="config.deployment_retention_verified === true" @connect="openDatabaseSource" @upload="uploadInput?.click()" @refresh="refreshMetadata" @delete-file="file => askDeletion({ kind: 'file', file })" @delete-source="source => askDeletion({ kind: 'source', source })" @delete-table="(source, table) => askDeletion({ kind: 'table', source, table })"/>
          </template>
          <template v-if="tab === 'documents'">
            <div class="intro"><div><h2>知识文档</h2></div><button class="btn primary" :disabled="locked" @click="documentInput?.click()">＋ 上传 Markdown</button><input ref="documentInput" type="file" hidden multiple accept=".md,.markdown" @change="upload"/></div>
            <div class="document-layout"><section class="panel"><div class="panel-head"><h2>Markdown 知识材料</h2><span class="badge">{{ documentFiles.length }} 个文档</span></div><div v-if="!documentFiles.length" class="empty-state"><h3>添加知识材料</h3><p>支持 Markdown 数据说明、业务定义和分析经验；运行时按需要读取，不会一次性塞入全部原文。</p></div><div v-for="file in documentFiles" :key="file.file_id" class="doc-row"><span class="file-icon">MD</span><div class="file-info"><h3>{{ file.name }}</h3><div class="chips"><span v-if="!Array.isArray(config.documents) || !config.documents.includes(file.relative_path)" class="badge amber">未参与抽取</span><span class="badge blue">{{ purposeLabels[file.purpose] || '待分类' }}</span><span class="badge">{{ file.scope?.tables?.length ? `绑定 ${file.scope.tables.length} 张表` : file.scope?.source_ids?.length || file.scope?.columns?.length ? '限定范围' : '全局使用' }}</span></div><p v-if="file.scope?.tables?.length" class="break">{{ file.scope.tables.join('、') }}</p><p>{{ size(file.size) }}</p><div class="actions"><button class="btn small" :disabled="busy" @click="showContent(file)">查看原文</button><button class="btn small" :disabled="locked" @click="openBinding(file)">设置用途与范围</button><button class="btn small danger-text" :disabled="locked" :aria-label="`删除文档 ${file.name}`" @click="askDeletion({ kind: 'file', file })">删除</button></div></div></div></section></div>
          </template>
          <template v-if="tab === 'metadata' || tab === 'business'"><div class="intro"><div><h2>{{ tab === 'metadata' ? '元数据关联图' : '业务知识图谱' }}</h2></div><button v-if="tab === 'business'" class="btn" :aria-expanded="structurePreviewOpen" @click="structurePreviewOpen = !structurePreviewOpen">{{ structurePreviewOpen ? '收起结构方案' : '查看结构方案' }}</button><button class="btn" :disabled="busy" @click="action(refreshGraphs)">刷新图谱</button></div><p v-if="tab === 'metadata' && metadata.requires_refresh" class="note graph-notice">{{ metadata.tables.length ? '目前仅显示已读取来源的元数据；其他来源仍需连接并读取。' : '尚未读取元数据。请在「项目配置 → 数据接入」连接来源并读取元数据。' }}</p><p v-if="tab === 'metadata' && metadata.stale_engine_metadata" class="note amber graph-notice">元数据声明已更新。重新运行抽取后，会刷新与新声明对应的关系。</p><label v-if="tab === 'business'" class="checkbox-line"><input v-model="includeCandidates" type="checkbox" :disabled="busy"/>显示候选与未决关系</label><p v-if="tab === 'business' && graphError" class="note amber" role="alert">图谱读取失败：{{ graphError }} <button class="text-button" @click="loadBusinessGraph()">重试读取图谱</button></p><StructurePreview v-if="tab === 'business' && structurePreviewOpen" :key="project.project_id" :project-id="project.project_id" :config-revision="configRevision"/><GraphCanvas :graph="tab === 'metadata' ? metadata : business" :layer="tab" :loading="busy || (tab === 'business' && graphLoading)" :project-id="project.project_id" :include-candidates="includeCandidates" :config-revision="configRevision"/><IdentityCandidates v-if="tab === 'business'" :project-id="project.project_id" :config-revision="configRevision" :graph="business" :disabled="locked || dirty || advancedDirty" @changed="action(refreshGraphs)"/><DiscoveryCandidates v-if="tab === 'business'" :project-id="project.project_id" :config-revision="configRevision" :refresh-key="business" :disabled="locked || dirty || advancedDirty"/><section v-if="tab === 'metadata' && metadata.tables.length" class="panel metadata-table"><div class="panel-head"><h2>表与列</h2></div><details v-for="table in metadata.tables" :key="tableKey(table)"><summary>{{ tableKey(table) }} <span class="muted">{{ table.columns.length }} 列</span></summary><div class="table-scroll"><table><thead><tr><th>字段</th><th>类型</th><th>注释</th></tr></thead><tbody><tr v-for="column in table.columns" :key="column.name"><td>{{ column.name }}</td><td>{{ column.data_type || '—' }}</td><td>{{ column.comment || '—' }}</td></tr></tbody></table></div></details></section></template>
          <template v-if="tab === 'runs'"><div class="intro"><div><h2>运行记录</h2></div></div><section class="panel"><div v-if="!runs.length" class="empty-state"><h3>尚未开始抽取</h3><p>先上传数据、配置目标和模型，再开始第一轮运行。</p></div><div v-else class="table-scroll"><table><thead><tr><th>运行编号</th><th>状态</th><th>当前阶段</th><th>创建时间</th><th>资源释放</th><th></th></tr></thead><tbody><tr v-for="run in runs" :key="run.run_id"><td class="mono">{{ run.run_id }}</td><td><span class="badge" :class="run.status === 'completed' ? 'green' : run.status === 'failed' ? 'red' : 'blue'">{{ runLabels[run.status] }}</span></td><td>{{ stageLabel(run.progress?.stage || run.stage) }}</td><td>{{ time(run.created_at) }}</td><td>{{ run.cleanup_status === 'completed' ? '已完成' : run.cleanup_status === 'pending' ? '待完成' : '未确认' }}</td><td><button class="btn small" :disabled="busy" @click="inspectRun(run)">{{ isActive(run) ? '查看进度' : '查看报告' }}</button></td></tr></tbody></table></div></section><RunProgress v-if="selectedRun" :key="`${selectedRun.project_id}:${selectedRun.run_id}`" :run="selectedRun" :refresh-error="runPollError === selectedRun.run_id"/><section v-if="selectedRun" class="panel run-report"><div class="panel-head"><h2>运行详情</h2><span class="badge">{{ selectedRun.run_id }}</span></div><div class="panel-body"><div class="report-summary"><div><small>执行状态</small><strong>{{ runLabels[selectedRun.status] }}</strong></div><div><small>当前阶段</small><strong>{{ stageLabel(selectedRun.progress?.stage || selectedRun.stage) }}</strong></div><div><small>配置修订</small><strong>{{ selectedRun.config_revision ?? '历史记录未提供' }}</strong></div><div><small>结束时间</small><strong>{{ time(selectedRun.finished_at) }}</strong></div></div><p v-if="graphOutcome(selectedRun.report)" class="note">{{ graphOutcome(selectedRun.report) }}</p><p v-if="selectedRun.error_code" class="note amber">{{ new ApiError(selectedRun.error_code).message }}</p><p class="field-hint">资源释放指关闭连接和释放运行内存；运行轨迹保留，需在下方主动清理。</p><ConstructionReport :report="selectedRun.report"/><RunIssues :report="selectedRun.report"/><TechnicalDetails v-if="selectedRun.report" :value="selectedRun.report" title="报告原始结构"/><p v-else class="note">{{ isActive(selectedRun) ? '运行尚未结束，报告会在后端生成后显示。' : '此运行没有可读取的报告，请结合状态和错误码判断。' }}</p></div></section><RunTrace v-if="selectedRun" :key="`${selectedRun.project_id}:${selectedRun.run_id}`" :run="selectedRun" :config-revision="configRevision"/></template>
          <template v-if="tab === 'settings'"><div class="intro"><div><h2>模型配置</h2></div></div><div class="layout-two"><section class="panel"><div class="panel-head"><h2>模型连接</h2><span class="badge blue">运行设置</span></div><div class="panel-body"><p v-if="config.agent_config" class="note amber">当前配置引用独立模型文件 {{ config.agent_config }}。连接信息在该文件中配置，下方可调整额度和超时；保存时会生成新配置，保留原文件。</p><template v-else><div class="field"><label for="model-mode">模型模式</label><select id="model-mode" v-model="model.mode" :disabled="locked"><option value="disabled">关闭模型，暂不启动自主构建</option><option value="metadata_only">仅元数据，不向模型发送样本</option><option value="in_environment">环境内模型，可使用受限样本</option><option value="approved_external">已批准的外部模型</option></select></div><div class="field"><label for="model-url">服务 Base URL</label><input id="model-url" v-model="model.base_url" :disabled="locked" placeholder="https://model.example.com/v1" autocomplete="off"/><p class="field-hint">使用兼容 Chat Completions 的地址；不要在 URL 中包含密钥。</p></div><div class="two-fields"><div class="field"><label for="model-name">模型名称</label><input id="model-name" v-model="model.model" :disabled="locked" placeholder="模型服务提供的名称"/></div><div class="field"><label for="model-key">API key</label><input id="model-key" v-model="modelKey" :disabled="locked" type="password" autocomplete="new-password" :placeholder="credentials.model ? '已在本机保存，留空保留' : credentials.model_environment ? '服务器已配置，可留空' : '填写模型服务的 API key'"/></div></div><p class="credential-status field-hint"><span class="badge" :class="credentials.model || credentials.model_environment ? 'green' : ''">{{ credentials.model ? '已在本机保存密钥' : credentials.model_environment ? '已配置服务器环境变量' : '尚未配置密钥' }}</span><button v-if="credentials.model || credentials.model_error" class="text-button" :disabled="locked" @click="clearModelCredential">清除密钥</button></p><p v-if="credentials.model_error" class="note amber" role="alert">{{ new ApiError(credentials.model_error).message }}</p><details class="model-advanced"><summary>使用服务器环境变量（可选）</summary><div class="field"><label for="model-key-env">API key 环境变量名</label><input id="model-key-env" v-model="model.api_key_env" :disabled="locked" placeholder="LIGHTGRAPH_API_KEY" autocomplete="off"/></div></details><p class="field-hint">连接和接口参数由后台自动适配。</p><label v-if="internalHttpEndpoint" class="checkbox-line"><input id="model-allow-http" v-model="model.allow_insecure_http" type="checkbox" :disabled="locked"/>允许使用此内部 HTTP 服务</label></template>
<div class="field"><label for="construction-target">构建结果</label><select id="construction-target" v-model="constructionTarget" :disabled="locked"><option value="instances">生成实例图谱</option><option value="structure">仅生成结构与映射</option></select><p class="field-hint">结构与映射可以单独保存和发布；这不代表具体节点、关系或全部数据已经验证。</p></div>
<div class="field"><label for="max-db-queries">数据查询次数上限</label><label class="checkbox-line"><input id="query-count-unlimited" v-model="queryCountUnlimited" type="checkbox" :disabled="locked"/>不限次数</label><input id="max-db-queries" type="number" min="1" step="1" :value="maxDbQueries ?? ''" :disabled="locked || queryCountUnlimited" placeholder="填写正整数" @input="maxDbQueries = ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value)"/><p class="field-hint">所有数据来源共用，包括元数据与事务控制查询。不限次数仍受单次查询超时、整次运行时限和数据量限制。</p></div>
<p v-if="agentLimitsLoading" role="status">正在读取模型额度…</p>
<div class="two-fields"><div class="field"><label for="model-max-calls">模型调用总额度</label><input id="model-max-calls" type="number" min="0" step="1" :value="limitModel.max_calls === undefined ? 40 : limitModel.max_calls ?? ''" :disabled="locked || agentLimitsLoading" placeholder="留空不额外限制" @input="setModelLimit('max_calls', $event)"/><p class="field-hint">整次运行的模型请求次数，包含重试和压缩调用。0 表示不允许调用模型。</p></div><div class="field"><label for="run-seconds">整次运行时限（秒）</label><input id="run-seconds" type="number" min="0.1" step="any" :value="runSeconds ?? ''" :disabled="locked || agentLimitsLoading" @input="runSeconds = ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value)"/><p class="field-hint">由同一时限覆盖发现、验证与构建，</p></div></div>
<div class="two-fields"><div class="field"><label for="model-input-tokens">最大输入 tokens</label><input id="model-input-tokens" type="number" min="1" step="1" :value="limitModel.max_prompt_tokens ?? ''" :disabled="locked || agentLimitsLoading" placeholder="留空不另设输入限制" @input="setModelLimit('max_prompt_tokens', $event)"/><p class="field-hint">请求派发前按估算值检查输入，包括系统提示、历史消息和工具定义。额度估算会结合接口实际用量校准，不代表精确分词结果。</p></div><div class="field"><label for="model-output-tokens">最大输出 tokens</label><input id="model-output-tokens" type="number" min="1" step="1" :value="limitModel.max_output_tokens ?? ''" :disabled="locked || agentLimitsLoading" placeholder="默认 4096" @input="setModelLimit('max_output_tokens', $event)"/><p class="field-hint">单次请求的输出上限，不是整轮抽取的 token 预算。</p></div></div>
<div class="field"><label for="model-timeout-seconds">接口超时时间（秒）</label><input id="model-timeout-seconds" type="number" min="0.1" step="any" :value="limitModel.timeout_seconds ?? ''" :disabled="locked || agentLimitsLoading" placeholder="默认 60" @input="setModelLimit('timeout_seconds', $event)"/></div>
<p class="field-hint">配置保存成功后，下一次启动立即使用新值，无需重启服务。运行中的任务使用启动时的配置。</p>
<div class="field"><label for="model-context-tokens">模型总上下文窗口（tokens，可选）</label><input id="model-context-tokens" type="number" min="1" step="1" :value="limitModel.context_window_tokens ?? ''" :disabled="locked || agentLimitsLoading" placeholder="留空不额外限制" @input="setModelLimit('context_window_tokens', $event)"/><p class="field-hint">同时设置时，输入上限取“最大输入”与“总窗口减最大输出”中的较小值。</p></div>
<p class="field-hint" data-testid="effective-input-tokens">当前有效输入限制：{{ effectiveInputTokens === null ? '未额外设置' : `${effectiveInputTokens} tokens` }}。</p>
<p class="field-hint">模型输入、返回及工具结果保留在本地，供排查与恢复使用；运行后需手动清理。</p>
<button v-if="!traceRecording" class="btn" :disabled="locked" @click="traceRecording = true">启用运行轨迹</button>
<p v-if="limitsError" class="note amber" role="alert">{{ limitsError }}</p>
</div><div class="panel-footer"><span>API key 加密保存在服务端本机，刷新后无需重填</span><button class="btn primary" :disabled="locked || agentLimitsLoading || !!limitsError" @click="action(saveModelLimits)">保存配置</button></div></section><aside class="panel"><div class="panel-head"><h2>运行前检查</h2></div><div class="panel-body"><div class="setup-check"><strong>{{ config.sources?.length || 0 }} 个数据来源</strong><button class="text-button" @click="tab = 'inputs'">配置数据接入</button></div><div class="setup-check"><strong>{{ documentFiles.length }} 个知识文档</strong><button class="text-button" @click="tab = 'documents'">配置知识文档</button></div><div class="setup-check"><strong>{{ purpose.trim() ? '已填写抽取目标' : '尚未填写抽取目标' }}</strong><button class="text-button" @click="tab = 'goal'">配置抽取目标</button></div></div></aside></div><section class="panel advanced-panel"><div class="panel-head"><h2>完整项目配置</h2><span class="badge">JSON 编辑 · 服务端保存 YAML</span></div><div class="panel-body"><p class="field-hint editor-hint">此处与上方表单同步；两处修改不同字段时一并保存，同一字段有冲突时会提示。</p><label class="sr-only" for="advanced-config">完整项目 JSON 配置</label><textarea id="advanced-config" v-model="advanced" class="code-editor" :disabled="locked" spellcheck="false"/></div><div class="panel-footer"><span>完整配置不接受明文密码或 API key，请使用上方连接表单</span><div class="actions"><button v-if="advancedDirty && (formDirty || agentLimitsDirty)" class="btn" :disabled="locked" @click="discardFormEdits">放弃表单修改</button><button v-if="advancedDirty" class="btn" :disabled="locked" @click="resetAdvanced">放弃修改</button><button class="btn" :disabled="locked" @click="action(async () => { advanced = JSON.stringify(advancedConfig(), null, 2) })">格式化 JSON</button><button class="btn primary" :disabled="locked" @click="saveAdvanced">保存完整配置</button></div></div></section></template>
        </div>
      </template>
    </main>
    <DatabaseSourceDialog v-if="databaseOpen" ref="databaseDialog" :project-id="project?.project_id || ''" :source="databaseSource" :sources="config.sources || []" :files="project?.files || []" :retention-verified="config.deployment_retention_verified === true" :config-revision="configRevision" :busy="busy" :error="error" @close="closeDatabaseSource" @save="saveDatabaseSources" @connected="databaseConnected" @capability-uploaded="capabilityUploaded"/>
    <div v-if="newProjectOpen" class="modal-backdrop" @click.self="!busy && (newProjectOpen = false)"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="new-project-title"><form @submit.prevent="createProject"><div class="panel-head"><h2 id="new-project-title">新建项目</h2><button type="button" class="close" aria-label="关闭" :disabled="busy" @click="newProjectOpen = false">×</button></div><div class="panel-body"><p v-if="error" class="note amber" role="alert">{{ error }}</p><div class="field"><label for="new-name">项目名称</label><input id="new-name" v-model="newName" required maxlength="120" autofocus placeholder="例如：数据关系图谱"/></div><div class="field"><label for="new-id">项目编号（可选）</label><input id="new-id" v-model="newId" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="留空由服务端生成"/><p class="field-hint">用于 workspace 下的隔离文件夹，创建后保持不变。</p></div></div><div class="panel-footer"><span>文件夹不存在时会自动创建</span><button class="btn primary" :disabled="busy || !newName.trim()">创建项目</button></div></form></section></div>
    <div v-if="bindingFile" class="modal-backdrop" @click.self="!busy && (bindingFile = null)"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="binding-title"><div class="panel-head"><h2 id="binding-title">文档用途与使用范围</h2><button class="close" aria-label="关闭" :disabled="busy" @click="bindingFile = null">×</button></div><div class="panel-body"><p class="document-name">{{ bindingFile.name }}</p><p v-if="error" class="note amber" role="alert">{{ error }}</p><div class="field"><label for="doc-purpose">文档用途</label><select id="doc-purpose" v-model="bindingPurpose"><option v-for="(label, key) in purposeLabels" :key="key" :value="key">{{ label }}</option></select></div><div class="field"><label>使用范围</label><div class="radio-line"><label><input v-model="bindingMode" type="radio" value="global"/>全局使用</label><label><input v-model="bindingMode" type="radio" value="tables"/>绑定具体表</label></div></div><div v-if="bindingMode === 'tables'" class="binding-options"><p v-if="!metadata.tables.length" class="note amber">请先关闭此窗口并读取元数据，再选择表。</p><label v-for="table in metadata.tables" :key="tableKey(table)" class="check-option"><input v-model="bindingTables" type="checkbox" :value="tableKey(table)"/><span>{{ tableKey(table) }}</span></label><p v-if="bindingScope.columns.length" class="field-hint">已有列限制将保留：{{ bindingScope.columns.join('、') }} <button class="text-button" :disabled="busy" @click="bindingScope.columns = []">清除列限制</button></p></div><p v-else class="note">所有相关分析都可使用此文档；设置为全局会移除原有表和列限制。</p></div><div class="panel-footer"><span>只影响上下文使用范围，不授予数据访问权限</span><button class="btn primary" :disabled="busy || (bindingMode === 'tables' && !bindingTables.length)" @click="saveBinding">保存绑定</button></div></section></div>
    <div v-if="preview" class="modal-backdrop" @click.self="preview = null"><section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="preview-title"><div class="panel-head"><h2 id="preview-title">{{ preview.name }}</h2><button class="close" aria-label="关闭" @click="preview = null">×</button></div><div class="panel-body"><pre class="code file-preview">{{ preview.content }}</pre></div></section></div>
    <ConfirmDelete v-if="deletion" :title="deletionTitle" :description="deletionDescription" :detail="deletionDetail" :busy="busy" :locked="locked" :error="deletionError" @cancel="deletion = null" @confirm="confirmDeletion"/>
    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </div>
</template>
