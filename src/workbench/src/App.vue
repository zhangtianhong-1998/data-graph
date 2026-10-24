<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import GraphCanvas from './GraphCanvas.vue'
import DatabaseSourceDialog from './DatabaseSourceDialog.vue'
import DataInputs from './DataInputs.vue'
import ConfirmDelete from './ConfirmDelete.vue'
import { configurationTabs, resultTabs, isResultTab } from './onboarding'
import type { ConfigurationTab, ResultTab, WorkbenchTab } from './onboarding'
import { ApiError, ensureSession, clearSessions, clearSession, json, projectPath, request } from './api'
import { assertNoCredentials, isActive, runLabels, splitLines, stageLabel } from './state'
import { WorkbenchLocation } from './workbenchLocation'
import type { DocumentScope, Graph, Metadata, Project, ProjectFile, Run, RunConfig, SourceConfig } from './types'


const projects = ref<Project[]>([])
const project = ref<Project | null>(null)
const config = ref<RunConfig>({})
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
const purpose = ref(''), includes = ref(''), excludes = ref(''), guidance = ref('')
const model = ref<NonNullable<RunConfig['model']>>({})
const advanced = ref('')
const modelKey = ref('')
const credentials = ref<{ database_sources: string[]; model: boolean }>({ database_sources: [], model: false })
const metadata = ref<Metadata>({ tables: [], nodes: [], edges: [] })
const business = ref<Graph>({ nodes: [], edges: [] })
const runs = ref<Run[]>([])
const selectedRun = ref<Run | null>(null)
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
const dirty = computed(() => !!modelKey.value || purpose.value !== (config.value.goal?.purpose || '') || includes.value !== (config.value.goal?.include || []).join('\n') || excludes.value !== (config.value.goal?.exclude || []).join('\n') || guidance.value !== (config.value.goal?.discovery_guidance || []).join('\n') || JSON.stringify(model.value) !== JSON.stringify(config.value.model || {}))
const advancedDirty = computed(() => advanced.value !== JSON.stringify(config.value, null, 2))
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
function report(exception: unknown) { error.value = exception instanceof Error ? exception.message : '操作未完成，请重试。' }
function fillForms(value: RunConfig) {
  config.value = value
  purpose.value = value.goal?.purpose || ''; includes.value = (value.goal?.include || []).join('\n'); excludes.value = (value.goal?.exclude || []).join('\n'); guidance.value = (value.goal?.discovery_guidance || []).join('\n')
  model.value = structuredClone(value.model || {}); advanced.value = JSON.stringify(value, null, 2)
}
function formConfig(): RunConfig {
  const value: RunConfig = { ...config.value, goal: { ...config.value.goal, purpose: purpose.value, include: splitLines(includes.value), exclude: splitLines(excludes.value), discovery_guidance: splitLines(guidance.value) } }
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
  if (project.value && project.value.project_id !== id && (dirty.value || advancedDirty.value) && !window.confirm('当前项目有未保存的配置修改。切换项目会放弃这些修改，继续吗？')) return
  workbenchLocation.selectProject(id)
  const epoch = ++generation
  clearTimeout(pollTimer); loading.value = true; error.value = ''; selectedRun.value = null; preview.value = null; bindingFile.value = null; databaseOpen.value = false; modelKey.value = ''; credentials.value = { database_sources: [], model: false }
  const p = projectPath(id)
  try {
    const values = await Promise.all([request<Project>(p), request<RunConfig>(`${p}/config`)])
    if (epoch !== generation) return
    project.value = values[0]; fillForms(values[1]); await ensureSession(id); metadata.value = { tables: [], nodes: [], edges: [] }; business.value = { nodes: [], edges: [] }; runs.value = []
    // An unavailable database must not prevent opening and repairing its project configuration.
    const details = await Promise.allSettled([request<Metadata>(`${p}/metadata`), request<Graph>(`${p}/graph?layer=business`), request<{ runs: Run[] }>(`${p}/runs`), request<{ database_sources: string[]; model: boolean }>(`${p}/credentials`)])
    if (epoch !== generation) return
    if (details[0].status === 'fulfilled') { metadata.value = details[0].value }
    if (details[1].status === 'fulfilled') business.value = details[1].value
    if (details[2].status === 'fulfilled') runs.value = details[2].value.runs
    if (details[3].status === 'fulfilled') credentials.value = details[3].value
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
    purpose: purpose.value !== (config.value.goal?.purpose || '') ? purpose.value : undefined,
    includes: includes.value !== (config.value.goal?.include || []).join('\n') ? includes.value : undefined,
    excludes: excludes.value !== (config.value.goal?.exclude || []).join('\n') ? excludes.value : undefined,
    guidance: guidance.value !== (config.value.goal?.discovery_guidance || []).join('\n') ? guidance.value : undefined,
    model: JSON.stringify(model.value) !== JSON.stringify(config.value.model || {}) ? JSON.parse(JSON.stringify(model.value)) as typeof model.value : undefined,
  } : null
  const agentConfigRemoved = preserveEdits && !!config.value.agent_config && !nextConfig.agent_config
  project.value = nextProject; fillForms(nextConfig)
  if (agentConfigRemoved) modelKey.value = ''
  if (edits) {
    if (edits.purpose !== undefined) purpose.value = edits.purpose
    if (edits.includes !== undefined) includes.value = edits.includes
    if (edits.excludes !== undefined) excludes.value = edits.excludes
    if (edits.guidance !== undefined) guidance.value = edits.guidance
    if (edits.model !== undefined && !agentConfigRemoved) model.value = edits.model
  }
  projects.value = projects.value.map(p => p.project_id === nextProject.project_id ? nextProject : p)
}
async function refreshProjectConfig(preserveEdits = false) {
  const values = await Promise.all([request<Project>(path.value), request<RunConfig>(`${path.value}/config`)])
  applyProjectConfig(values[0], values[1], preserveEdits)
}
async function refreshGraphs() {
  const p = path.value, epoch = generation
  const values = await Promise.all([request<Metadata>(`${p}/metadata`), request<Graph>(`${p}/graph?layer=business`)])
  if (epoch === generation) { metadata.value = values[0]; business.value = values[1] }
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
      runs.value = runs.value.map(item => item.run_id === run.run_id ? run : item)
      if (selectedRun.value?.run_id === run.run_id) selectedRun.value = run
      if (!isActive(run)) { await refreshGraphs(); notify(`运行${runLabels[run.status]}，请查看结果与报告。`) }
    } catch (exception) { if (epoch === generation) report(exception) }
    schedulePoll(epoch)
  }, 1000)
}
async function refreshCredentials() {
  credentials.value = await request<{ database_sources: string[]; model: boolean }>(`${path.value}/credentials`)
}
async function saveForms() {
  fillForms(await request<RunConfig>(`${path.value}/config`, { method: 'PUT', body: json(formConfig()) }))
  if (modelKey.value) {
    const key = modelKey.value; modelKey.value = ''
    try { await request(`${path.value}/credentials/model`, { method: 'PUT', body: json({ api_key: key }) }) }
    finally { await refreshCredentials() }
  } else await refreshCredentials()
}
async function clearModelCredential() {
  await action(async () => { modelKey.value = ''; await request(`${path.value}/credentials/model`, { method: 'DELETE' }); await refreshCredentials(); notify('已清除本次会话的模型密钥') })
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
    const result = await request<{ project?: Project; config?: RunConfig; detached_rule_paths?: string[] }>(endpoint, { method: 'DELETE', ...(target.kind === 'table' ? { body: json({ table: target.table }) } : {}) })
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
    if (dirty.value) await saveForms()
    let count = 0
    try {
      for (const file of files) {
        const body = new FormData(); body.append('file', file)
        const uploaded = await request<ProjectFile>(`${path.value}/files`, { method: 'POST', body }); count++
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
    if (dirty.value) await saveForms()
    if (bindingMode.value === 'tables' && !bindingTables.value.length) throw new Error('请至少选择一张表，或改为全局使用。')
    const scope = bindingMode.value === 'global' ? { source_ids: [], tables: [], columns: [] } : { ...bindingScope.value, source_ids: [], tables: [...bindingTables.value] }
    await request<ProjectFile>(`${path.value}/files/${encodeURIComponent(bindingFile.value.file_id)}/binding`, { method: 'PATCH', body: json({ purpose: bindingPurpose.value, scope }) })
    bindingFile.value = null; await refreshProjectConfig(); notify('文档用途和使用范围已保存')
  })
}
async function importYaml(file: ProjectFile) {
  await action(async () => {
    await request<RunConfig>(`${path.value}/config/import`, { method: 'POST', body: json({ file_id: file.file_id }) }); await refreshProjectConfig(); notify('YAML 已导入为当前项目配置')
  })
}
async function refreshMetadata() {
  await action(async () => { if (dirty.value) await saveForms(); metadata.value = await request<Metadata>(`${path.value}/metadata/refresh`, { method: 'POST', body: '{}' }); notify('元数据已更新') })
}
async function startRun() {
  await action(async () => {
    await saveForms()
    const run = await request<Run>(`${path.value}/runs`, { method: 'POST', body: '{}' })
    runs.value = [run, ...runs.value.filter(item => item.run_id !== run.run_id)]; selectedRun.value = run; tab.value = 'runs'; schedulePoll(generation)
  })
}
async function cancelRun(run: Run) {
  await action(async () => { const result = await request<Run>(`${path.value}/runs/${encodeURIComponent(run.run_id)}/cancel`, { method: 'POST', body: '{}' }); runs.value = runs.value.map(item => item.run_id === result.run_id ? result : item); selectedRun.value = result; schedulePoll(generation); if (!isActive(result)) await refreshGraphs() })
}
async function inspectRun(run: Run) { await action(async () => { selectedRun.value = await request<Run>(`${path.value}/runs/${encodeURIComponent(run.run_id)}`) }) }
async function saveAdvanced() {
  await action(async () => {
    let value: unknown
    try { value = JSON.parse(advanced.value) } catch { throw new Error('JSON 格式不正确，请检查引号、逗号和括号。') }
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('配置必须是 JSON 对象。')
    assertNoCredentials(value); fillForms(await request<RunConfig>(`${path.value}/config`, { method: 'PUT', body: json(value) })); notify('完整配置已保存')
  })
}
async function openDatabaseSource(source: SourceConfig | null = null) {
  await action(async () => {
    if (dirty.value) await saveForms()
    error.value = ''; databaseSource.value = source; databaseOpen.value = true
    workbenchLocation.openDatabase(project.value!.project_id, source?.id ?? null)
  })
}
function closeDatabaseSource() {
  databaseOpen.value = false
  if (project.value) workbenchLocation.closeDatabase(project.value.project_id)
}
async function databaseConnected(value: RunConfig, result: Metadata) {
  closeDatabaseSource()
  await action(async () => { fillForms(value); metadata.value = result; await refreshProjectConfig(); await refreshCredentials(); notify('数据库已接入，选中表的元数据已生成') })
}

async function saveDatabaseSources(sources: SourceConfig[], retentionVerified: boolean) {
  await action(async () => {
    const value = { ...formConfig(), sources, deployment_retention_verified: retentionVerified }
    fillForms(await request<RunConfig>(`${path.value}/config`, { method: 'PUT', body: json(value) }))
    await databaseDialog.value?.completeDraft()
    closeDatabaseSource(); await refreshCredentials(); notify('数据库来源已保存，点击读取元数据可连接服务器。')
  })
}
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
      <div v-if="error" class="error-strip" role="alert"><span>{{ error }}</span><button aria-label="关闭错误提示" @click="error = ''">×</button></div>
      <div v-if="loading" class="page-empty"><div class="spinner"></div><p>正在读取项目</p></div>
      <div v-else-if="!project" class="page-empty"><span class="empty-graphic">◇</span><h1>从一个项目开始</h1><p>上传数据、指定抽取目的，让 Agent 发现关键列和关系。<br/>Excel、CSV、Markdown 和配置文件会保存在项目自己的文件夹中。</p><button class="btn primary" @click="newProjectOpen = true">新建项目</button><button v-if="error" class="btn" @click="action(() => reloadProjects(projects[0]?.project_id))">重新连接</button></div>
      <template v-else>
        <section class="project-head">
          <div class="phase-bar">
            <div class="phase-tabs" role="tablist" aria-label="工作阶段" @keydown="phaseKeydown">
              <button id="configuration-phase" role="tab" aria-controls="phase-content" :aria-selected="!inResults" :tabindex="!inResults ? 0 : -1" :class="{ active: !inResults }" @click="tab = configurationTab">项目配置</button>
              <button id="results-phase" role="tab" aria-controls="phase-content" :aria-selected="inResults" :tabindex="inResults ? 0 : -1" :class="{ active: inResults }" @click="tab = resultTab">运行与结果<span v-if="activeRun" class="status-dot" aria-label="正在运行"></span></button>
            </div>
            <div class="actions"><span v-if="dirty" class="badge amber">有未保存修改</span><button class="btn primary" :disabled="locked || (!config.sources?.length && !documentFiles.length) || !purpose.trim()" :title="!purpose.trim() ? '请先填写抽取目的' : '保存配置后启动抽取'" @click="startRun">{{ activeRun ? '正在抽取' : dirty ? '保存并开始抽取' : '开始抽取' }}</button></div>
          </div>
          <nav class="tabs" :aria-label="inResults ? '运行与结果' : '项目配置'"><button v-for="item in tabs" :key="item.id" class="tab" :class="{ active: tab === item.id }" @click="tab = item.id">{{ item.label }}<span v-if="item.id === 'runs' && runs.length" class="count">{{ runs.length }}</span></button></nav>
        </section>
        <div id="phase-content" class="content" :class="{ 'inputs-content': tab === 'inputs' }" role="tabpanel" :aria-labelledby="inResults ? 'results-phase' : 'configuration-phase'">
          <div v-if="activeRun" class="run-banner"><span class="spinner tiny"></span><div><strong>{{ runLabels[activeRun.status] }}</strong><span>{{ stageLabel(activeRun.stage) }} · {{ activeRun.run_id }}</span></div><button class="btn small" :disabled="busy" @click="cancelRun(activeRun)">取消运行</button></div>
          <template v-if="tab === 'goal'">
            <div class="intro"><div><h2>这次希望发现什么？</h2></div></div>
            <div class="layout-two"><section class="panel"><div class="panel-head"><h2>抽取目标</h2><span class="badge">全流程生效</span></div><div class="panel-body"><div class="field"><label for="purpose">分析目的</label><textarea id="purpose" v-model="purpose" :disabled="locked" class="goal-input" placeholder="例如：构建经营分析所需的指标、度量和组织层级关系，不抽取逐笔交易金额。"/></div><div class="two-fields"><div class="field"><label for="includes">重点关注</label><textarea id="includes" v-model="includes" :disabled="locked" placeholder="每行一个目标，例如：&#10;指标编码与中英文名称&#10;组织上下级关系"/></div><div class="field"><label for="excludes">排除内容</label><textarea id="excludes" v-model="excludes" :disabled="locked" placeholder="每行一项，例如：&#10;逐笔业务金额&#10;展示排序字段"/></div></div><div class="field"><label for="guidance">发现提示</label><textarea id="guidance" v-model="guidance" :disabled="locked" placeholder="可补充业务经验，例如：同一对象在不同分析范围中可能处于不同层级，不要仅凭列位置确定全局层级。"/></div></div><div class="panel-footer"><span>修改只在保存后写入项目配置</span><button class="btn primary" :disabled="locked" @click="action(async () => { await saveForms(); notify('抽取目标已保存') })">保存目标</button></div></section><aside class="stack"><section class="panel"><div class="panel-head"><h2>项目概况</h2></div><div class="panel-body"><div class="scope-row"><span>数据来源</span><strong>{{ config.sources?.length || 0 }}</strong></div><div class="scope-row"><span>知识文档</span><strong>{{ documentFiles.length }}</strong></div><div class="scope-row"><span>已读取的表</span><strong>{{ metadata.tables.length }}</strong></div><div class="scope-row"><span>业务图节点</span><strong>{{ business.nodes.length }}</strong></div></div></section></aside></div>
          </template>
          <template v-if="tab === 'inputs'">
            <input ref="uploadInput" type="file" hidden multiple accept=".xlsx,.csv,.yaml,.yml" @change="upload"/>
            <DataInputs :key="project.project_id" :project-id="project.project_id" :sources="config.sources || []" :files="project.files" :metadata="metadata" :locked="locked" @connect="openDatabaseSource" @upload="uploadInput?.click()" @refresh="refreshMetadata" @preview="showContent" @import="importYaml" @delete-file="file => askDeletion({ kind: 'file', file })" @delete-source="source => askDeletion({ kind: 'source', source })" @delete-table="(source, table) => askDeletion({ kind: 'table', source, table })"/>
          </template>
          <template v-if="tab === 'documents'">
            <div class="intro"><div><h2>知识文档</h2></div><button class="btn primary" :disabled="locked" @click="documentInput?.click()">＋ 上传 Markdown</button><input ref="documentInput" type="file" hidden multiple accept=".md,.markdown" @change="upload"/></div>
            <div class="document-layout"><section class="panel"><div class="panel-head"><h2>Markdown 文档</h2><span class="badge">{{ documentFiles.length }} 个文档</span></div><div v-if="!documentFiles.length" class="empty-state"><h3>添加数据说明或分析知识</h3></div><div v-for="file in documentFiles" :key="file.file_id" class="doc-row"><span class="file-icon">MD</span><div class="file-info"><h3>{{ file.name }}</h3><div class="chips"><span v-if="!Array.isArray(config.documents) || !config.documents.includes(file.relative_path)" class="badge amber">未参与抽取</span><span class="badge blue">{{ purposeLabels[file.purpose] || '待分类' }}</span><span class="badge">{{ file.scope?.tables?.length ? `绑定 ${file.scope.tables.length} 张表` : file.scope?.source_ids?.length || file.scope?.columns?.length ? '限定范围' : '全局使用' }}</span></div><p v-if="file.scope?.tables?.length" class="break">{{ file.scope.tables.join('、') }}</p><p>{{ size(file.size) }}</p><div class="actions"><button class="btn small" :disabled="busy" @click="showContent(file)">查看原文</button><button class="btn small" :disabled="locked" @click="openBinding(file)">设置用途与范围</button><button class="btn small danger-text" :disabled="locked" :aria-label="`删除文档 ${file.name}`" @click="askDeletion({ kind: 'file', file })">删除</button></div></div></div></section></div>
          </template>
          <template v-if="tab === 'metadata' || tab === 'business'"><div class="intro"><div><h2>{{ tab === 'metadata' ? '元数据关联图' : '业务知识图谱' }}</h2></div><button class="btn" :disabled="busy" @click="action(refreshGraphs)">刷新图谱</button></div><p v-if="tab === 'metadata' && metadata.requires_refresh" class="note graph-notice">{{ metadata.tables.length ? '目前仅显示已读取来源的元数据；其他来源仍需连接并读取。' : '尚未读取元数据。请在「项目配置 → 数据接入」连接来源并读取元数据。' }}</p><p v-if="tab === 'metadata' && metadata.stale_engine_metadata" class="note amber graph-notice">元数据声明已更新。重新运行抽取后，会刷新与新声明对应的关系。</p><GraphCanvas :graph="tab === 'metadata' ? metadata : business" :layer="tab" :loading="busy"/><section v-if="tab === 'metadata' && metadata.tables.length" class="panel metadata-table"><div class="panel-head"><h2>表与列</h2></div><details v-for="table in metadata.tables" :key="tableKey(table)"><summary>{{ tableKey(table) }} <span class="muted">{{ table.columns.length }} 列</span></summary><div class="table-scroll"><table><thead><tr><th>字段</th><th>类型</th><th>注释</th></tr></thead><tbody><tr v-for="column in table.columns" :key="column.name"><td>{{ column.name }}</td><td>{{ column.data_type || '—' }}</td><td>{{ column.comment || '—' }}</td></tr></tbody></table></div></details></section></template>
          <template v-if="tab === 'runs'"><div class="intro"><div><h2>运行记录</h2></div></div><section class="panel"><div v-if="!runs.length" class="empty-state"><h3>尚未开始抽取</h3><p>先上传数据、配置目标和模型，再开始第一轮运行。</p></div><div v-else class="table-scroll"><table><thead><tr><th>运行编号</th><th>状态</th><th>当前阶段</th><th>创建时间</th><th>临时数据清理</th><th></th></tr></thead><tbody><tr v-for="run in runs" :key="run.run_id"><td class="mono">{{ run.run_id }}</td><td><span class="badge" :class="run.status === 'completed' ? 'green' : run.status === 'failed' ? 'red' : 'blue'">{{ runLabels[run.status] }}</span></td><td>{{ stageLabel(run.stage) }}</td><td>{{ time(run.created_at) }}</td><td>{{ run.cleanup_status === 'completed' ? '已完成' : run.cleanup_status === 'pending' ? '待完成' : '未确认' }}</td><td><button class="btn small" :disabled="busy" @click="inspectRun(run)">查看报告</button></td></tr></tbody></table></div></section><section v-if="selectedRun" class="panel run-report"><div class="panel-head"><h2>运行详情</h2><span class="badge">{{ selectedRun.run_id }}</span></div><div class="panel-body"><div class="report-summary"><div><small>执行状态</small><strong>{{ runLabels[selectedRun.status] }}</strong></div><div><small>当前阶段</small><strong>{{ stageLabel(selectedRun.stage) }}</strong></div><div><small>结束时间</small><strong>{{ time(selectedRun.finished_at) }}</strong></div></div><p v-if="selectedRun.error_code" class="note amber">{{ new ApiError(selectedRun.error_code).message }}</p><pre v-if="selectedRun.report" class="code">{{ JSON.stringify(selectedRun.report, null, 2) }}</pre><p v-else class="note">{{ isActive(selectedRun) ? '运行尚未结束，报告会在后端生成后显示。' : '此运行没有可读取的报告，请结合状态和错误码判断。' }}</p></div></section></template>
          <template v-if="tab === 'settings'"><div class="intro"><div><h2>Agent 与模型</h2></div></div><div class="layout-two"><section class="panel"><div class="panel-head"><h2>模型连接</h2><span class="badge blue">AgentScope</span></div><div class="panel-body"><p v-if="config.agent_config" class="note amber">当前配置引用独立 Agent 文件 {{ config.agent_config }}，请在完整配置中调整；这里不覆盖该文件。</p><template v-else><div class="field"><label for="model-mode">模型模式</label><select id="model-mode" v-model="model.mode" :disabled="locked"><option value="disabled">关闭 LLM，只使用规则</option><option value="metadata_only">仅元数据，不向模型发送样本</option><option value="in_environment">环境内模型，可使用受限样本</option><option value="approved_external">已批准的外部模型</option></select></div><div class="field"><label for="model-url">服务 Base URL</label><input id="model-url" v-model="model.base_url" :disabled="locked" placeholder="https://model.example.com/v1" autocomplete="off"/><p class="field-hint">使用兼容 Chat Completions 的地址；不要在 URL 中包含密钥。</p></div><div class="two-fields"><div class="field"><label for="model-name">模型名称</label><input id="model-name" v-model="model.model" :disabled="locked" placeholder="模型服务提供的名称"/></div><div class="field"><label for="model-key">API key</label><input id="model-key" v-model="modelKey" :disabled="locked" type="password" autocomplete="new-password" :placeholder="credentials.model ? '本次会话已设置，留空保留' : '填写模型服务的 API key'"/></div></div><p class="credential-status field-hint"><span class="badge" :class="credentials.model ? 'green' : ''">{{ credentials.model ? '本次会话已配置密钥' : '尚未配置会话密钥' }}</span><button v-if="credentials.model" class="text-button" :disabled="locked" @click="clearModelCredential">清除密钥</button></p><details class="model-advanced"><summary>使用服务器环境变量（可选）</summary><div class="field"><label for="model-key-env">API key 环境变量名</label><input id="model-key-env" v-model="model.api_key_env" :disabled="locked" placeholder="LIGHTGRAPH_API_KEY" autocomplete="off"/></div></details><label class="checkbox-line"><input v-model="model.stream" type="checkbox" :disabled="locked"/>使用流式模型接口（SSE）</label></template></div><div class="panel-footer"><span>API key 仅本次会话有效，刷新后须重新填写</span><button class="btn primary" :disabled="locked || !!config.agent_config" @click="action(async () => { await saveForms(); notify('模型配置已保存') })">保存配置</button></div></section><aside class="panel"><div class="panel-head"><h2>运行前检查</h2></div><div class="panel-body"><div class="setup-check"><strong>{{ config.sources?.length || 0 }} 个数据来源</strong><button class="text-button" @click="tab = 'inputs'">配置数据接入</button></div><div class="setup-check"><strong>{{ documentFiles.length }} 个知识文档</strong><button class="text-button" @click="tab = 'documents'">配置知识文档</button></div><div class="setup-check"><strong>{{ purpose.trim() ? '已填写抽取目标' : '尚未填写抽取目标' }}</strong><button class="text-button" @click="tab = 'goal'">配置抽取目标</button></div></div></aside></div><section class="panel advanced-panel"><div class="panel-head"><h2>完整项目配置</h2><span class="badge">JSON 编辑 · 服务端保存 YAML</span></div><div class="panel-body"><p class="field-hint editor-hint">未保存的编辑不会用于抽取。</p><label class="sr-only" for="advanced-config">完整项目 JSON 配置</label><textarea id="advanced-config" v-model="advanced" class="code-editor" :disabled="locked" spellcheck="false"/></div><div class="panel-footer"><span>完整配置不接受明文密码或 API key，请使用上方连接表单</span><div class="actions"><button v-if="advancedDirty" class="btn" :disabled="locked" @click="advanced = JSON.stringify(config, null, 2)">放弃修改</button><button class="btn primary" :disabled="locked" @click="saveAdvanced">保存完整配置</button></div></div></section></template>
        </div>
      </template>
    </main>
    <DatabaseSourceDialog v-if="databaseOpen" ref="databaseDialog" :project-id="project?.project_id || ''" :source="databaseSource" :sources="config.sources || []" :files="project?.files || []" :retention-verified="config.deployment_retention_verified === true" :busy="busy" :error="error" @close="closeDatabaseSource" @save="saveDatabaseSources" @connected="databaseConnected"/>
    <div v-if="newProjectOpen" class="modal-backdrop" @click.self="!busy && (newProjectOpen = false)"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="new-project-title"><form @submit.prevent="createProject"><div class="panel-head"><h2 id="new-project-title">新建项目</h2><button type="button" class="close" aria-label="关闭" :disabled="busy" @click="newProjectOpen = false">×</button></div><div class="panel-body"><p v-if="error" class="note amber" role="alert">{{ error }}</p><div class="field"><label for="new-name">项目名称</label><input id="new-name" v-model="newName" required maxlength="120" autofocus placeholder="例如：经营分析知识图谱"/></div><div class="field"><label for="new-id">项目编号（可选）</label><input id="new-id" v-model="newId" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_-]*" placeholder="留空由服务端生成"/><p class="field-hint">用于 workspace 下的隔离文件夹，创建后保持不变。</p></div></div><div class="panel-footer"><span>文件夹不存在时会自动创建</span><button class="btn primary" :disabled="busy || !newName.trim()">创建项目</button></div></form></section></div>
    <div v-if="bindingFile" class="modal-backdrop" @click.self="!busy && (bindingFile = null)"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="binding-title"><div class="panel-head"><h2 id="binding-title">文档用途与使用范围</h2><button class="close" aria-label="关闭" :disabled="busy" @click="bindingFile = null">×</button></div><div class="panel-body"><p class="document-name">{{ bindingFile.name }}</p><p v-if="error" class="note amber" role="alert">{{ error }}</p><div class="field"><label for="doc-purpose">文档用途</label><select id="doc-purpose" v-model="bindingPurpose"><option v-for="(label, key) in purposeLabels" :key="key" :value="key">{{ label }}</option></select></div><div class="field"><label>使用范围</label><div class="radio-line"><label><input v-model="bindingMode" type="radio" value="global"/>全局使用</label><label><input v-model="bindingMode" type="radio" value="tables"/>绑定具体表</label></div></div><div v-if="bindingMode === 'tables'" class="binding-options"><p v-if="!metadata.tables.length" class="note amber">请先关闭此窗口并读取元数据，再选择表。</p><label v-for="table in metadata.tables" :key="tableKey(table)" class="check-option"><input v-model="bindingTables" type="checkbox" :value="tableKey(table)"/><span>{{ tableKey(table) }}</span></label><p v-if="bindingScope.columns.length" class="field-hint">已有列限制将保留：{{ bindingScope.columns.join('、') }} <button class="text-button" :disabled="busy" @click="bindingScope.columns = []">清除列限制</button></p></div><p v-else class="note">所有相关分析都可使用此文档；设置为全局会移除原有表和列限制。</p></div><div class="panel-footer"><span>只影响上下文使用范围，不授予数据访问权限</span><button class="btn primary" :disabled="busy || (bindingMode === 'tables' && !bindingTables.length)" @click="saveBinding">保存绑定</button></div></section></div>
    <div v-if="preview" class="modal-backdrop" @click.self="preview = null"><section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="preview-title"><div class="panel-head"><h2 id="preview-title">{{ preview.name }}</h2><button class="close" aria-label="关闭" @click="preview = null">×</button></div><div class="panel-body"><pre class="code file-preview">{{ preview.content }}</pre></div></section></div>
    <ConfirmDelete v-if="deletion" :title="deletionTitle" :description="deletionDescription" :detail="deletionDetail" :busy="busy" :locked="locked" :error="deletionError" @cancel="deletion = null" @confirm="confirmDeletion"/>
    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </div>
</template>
