<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { updateDatabaseSources } from './database'
import type { DatabaseForm } from './database'
import { connectionFormFromPayload, connectionPayload, isGaussJdbcDsn, selectedTables, validateSourceId } from './onboarding'
import type { ConnectionForm, ConnectionPayload, DatabaseTable } from './onboarding'
import { ensureSession, json, projectPath, request } from './api'
import type { Metadata, ProjectFile, RunConfig, SourceConfig } from './types'

const props = defineProps<{ projectId: string; source: SourceConfig | null; sources: SourceConfig[]; files: ProjectFile[]; retentionVerified: boolean; busy: boolean; error: string }>()
const emit = defineEmits<{ close: []; save: [sources: SourceConfig[], retentionVerified: boolean]; connected: [config: RunConfig, metadata: Metadata] }>()
const form = ref<DatabaseForm>({ id: props.source?.id || '', kind: props.source?.kind === 'mysql_database' ? 'mysql_database' : 'gauss_database', dsnEnv: typeof props.source?.dsn_env === 'string' ? props.source.dsn_env : '', tables: Array.isArray(props.source?.allowed_tables) ? props.source.allowed_tables.join('\n') : '', capabilityManifest: typeof props.source?.capability_manifest === 'string' ? props.source.capability_manifest : '' })
const connection = ref<ConnectionForm>({ kind: form.value.kind, format: 'fields', dsn: '', host: '', port: form.value.kind === 'mysql_database' ? '3306' : '5432', database: '', username: '', password: '', schema: '', sslmode: 'auto' })
const mode = ref<'direct' | 'environment'>('direct')
const progress = ref(''), notice = ref(''), rememberConnection = ref(true), savedConnection = ref(false), skippedCount = ref(0)
const controller = new AbortController()
const retention = ref(props.retentionVerified), localError = ref(''), pending = ref(false), tested = ref(false), tableLoaded = ref(false), truncated = ref(false)
const tables = ref<DatabaseTable[]>([]), checked = ref<string[]>([]), filter = ref(''), listFileInput = ref<HTMLInputElement | null>(null)
const manifests = computed(() => props.files.filter(file => file.kind === 'yaml'))
const missingManifest = computed(() => form.value.capabilityManifest && !manifests.value.some(file => file.relative_path === form.value.capabilityManifest))
const locked = computed(() => props.busy || pending.value)
const jdbcConnection = computed(() => connection.value.kind === 'gauss_database' && isGaussJdbcDsn(connection.value.dsn))
const sslHint = computed(() => {
  if (['disable', 'allow', 'prefer'].includes(connection.value.sslmode || '')) return '此模式可能使用未加密连接，请按数据库要求选择。'
  if (connection.value.sslmode === 'require') return '要求加密，不校验服务器主机名。'
  if (connection.value.sslmode === 'auto') return '优先使用连接串中的设置；未指定时，远程连接默认校验证书和主机名。'
  return ''
})
const path = computed(() => projectPath(props.projectId))
const visibleTables = computed(() => tables.value.filter(table => table.qualified_name.toLowerCase().includes(filter.value.toLowerCase())))
const tableCount = computed(() => new Set([...checked.value, ...form.value.tables.split('\n').map(value => value.trim()).filter(Boolean)]).size)
watch(() => form.value.kind, kind => { connection.value.kind = kind; connection.value.port = kind === 'mysql_database' ? '3306' : '5432' })
watch(connection, () => { tested.value = false; tableLoaded.value = false; tables.value = []; checked.value = []; truncated.value = false; skippedCount.value = 0 }, { deep: true })
function clearSecrets() { connection.value.password = ''; connection.value.dsn = '' }
function close() { if (locked.value) return; clearSecrets(); emit('close') }
onUnmounted(() => { controller.abort(); clearSecrets() })
onMounted(async () => {
  if (!props.source) return
  await perform(async () => {
    await ensureSession(props.projectId)
    if (controller.signal.aborted) return
    try {
      const result = await request<{ saved: boolean; connection?: ConnectionPayload }>(`${path.value}/database/connections/${encodeURIComponent(props.source!.id)}`, { signal: controller.signal })
      if (controller.signal.aborted) return
      savedConnection.value = result.saved
      if (result.saved && result.connection) {
        if (result.connection.kind !== form.value.kind) throw new Error('保存的连接与数据源类型不一致，请重新填写。')
        connection.value = connectionFormFromPayload(result.connection)
        rememberConnection.value = true
      }
    } catch (error) {
      // Corrupt encrypted profiles can still be deleted without decrypting their contents.
      const code = (error as { code?: string } | null)?.code
      if (!controller.signal.aborted && (code === 'saved_connection_corrupt' || code === 'saved_connection_key_missing')) savedConnection.value = true
      throw error
    }
  }, '正在加载已保存连接…')
})
async function deleteSavedConnection() {
  if (!props.source) return
  await perform(async () => {
    await ensureSession(props.projectId)
    if (controller.signal.aborted) return
    await request(`${path.value}/database/connections/${encodeURIComponent(props.source!.id)}`, { method: 'DELETE', signal: controller.signal })
    if (controller.signal.aborted) return
    savedConnection.value = false; rememberConnection.value = false
    notice.value = '已删除保存的连接，当前填写内容仅用于本次会话。'
  }, '正在删除已保存连接…')
}
async function perform(operation: () => Promise<void>, message = '正在处理…', validate: () => void = () => {}) {
  if (locked.value) return
  pending.value = true; localError.value = ''; notice.value = ''; progress.value = message
  try { validate(); if (!controller.signal.aborted) await operation() }
  catch (error) { if (!controller.signal.aborted) localError.value = error instanceof Error ? error.message : '操作未完成，请重试。' }
  finally { if (!controller.signal.aborted) { pending.value = false; progress.value = '' } }
}
async function testConnection() {
  await perform(async () => {
    tested.value = false
    await request(`${path.value}/database/test`, { method: 'POST', body: json({ connection: connectionPayload(connection.value) }), signal: controller.signal, timeoutMs: 30000 })
    if (controller.signal.aborted) return
    tested.value = true
    await loadTables()
  }, '正在测试连接…', () => { connectionPayload(connection.value) })
}
async function loadTables() {
  progress.value = '正在读取表清单…'
  const result = await request<{ tables: DatabaseTable[]; truncated: boolean; skipped_count?: number }>(`${path.value}/database/tables`, { method: 'POST', body: json({ connection: connectionPayload(connection.value) }), signal: controller.signal, timeoutMs: 45000 })
  if (controller.signal.aborted) return
  if (!tableLoaded.value) {
    const manual = form.value.tables.split('\n').map(name => name.trim()).filter(Boolean)
    const matched = result.tables.filter(table => manual.includes(table.qualified_name) || manual.includes(table.name))
    checked.value = [...new Set([...checked.value, ...matched.map(table => table.qualified_name)])]
    form.value.tables = manual.filter(name => !matched.some(table => table.name === name || table.qualified_name === name)).join('\n')
  }
  tables.value = result.tables; truncated.value = result.truncated; skippedCount.value = result.skipped_count || 0; tableLoaded.value = true
}
async function discoverTables() {
  await perform(loadTables, '正在读取表清单…', () => { connectionPayload(connection.value) })
}
async function importTableList(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file) return
  await perform(async () => { const body = new FormData(); body.append('file', file); const result = await request<{ tables: string[] }>(`${path.value}/database/table-list`, { method: 'POST', body }); form.value.tables = [...new Set([...form.value.tables.split('\n').filter(Boolean), ...result.tables])].join('\n') })
  input.value = ''
}
function toggleVisible() {
  const visible = visibleTables.value.map(table => table.qualified_name)
  checked.value = visible.every(name => checked.value.includes(name)) ? checked.value.filter(name => !visible.includes(name)) : [...new Set([...checked.value, ...visible])]
}
async function submit() {
  if (mode.value === 'environment') {
    localError.value = ''
    try { emit('save', updateDatabaseSources(props.sources, props.source?.id || null, form.value, manifests.value.map(file => file.relative_path)), retention.value) }
    catch (error) { localError.value = error instanceof Error ? error.message : '请检查数据库来源配置。' }
    return
  }
  await perform(async () => {
    validateSourceId(form.value.id.trim(), props.source, props.sources)
    const allowedTables = selectedTables(form.value.tables, checked.value)
    if (form.value.capabilityManifest && !manifests.value.some(file => file.relative_path === form.value.capabilityManifest)) throw new Error('请重新选择能力验证文件。')
    connectionPayload(connection.value)
    await ensureSession(props.projectId)
    if (controller.signal.aborted) return
    const result = await request<{ config: RunConfig; metadata: Metadata }>(`${path.value}/database/connect`, { method: 'POST', body: json({ source_id: form.value.id.trim(), remember_connection: rememberConnection.value, connection: connectionPayload(connection.value), allowed_tables: allowedTables, deployment_retention_verified: retention.value, capability_manifest: form.value.capabilityManifest || null }), signal: controller.signal, timeoutMs: 120000 })
    if (controller.signal.aborted) return
    clearSecrets(); emit('connected', result.config, result.metadata)
  }, '正在抽取元数据…')
}
</script>

<template>
  <div class="modal-backdrop">
    <section class="modal wide database-modal" role="dialog" aria-modal="true" aria-labelledby="database-title">
      <form @submit.prevent="submit" autocomplete="off">
        <div class="panel-head"><div><h2 id="database-title">{{ source ? '编辑数据库来源' : '接入数据库' }}</h2></div><button type="button" class="close" aria-label="关闭" :disabled="locked" @click="close">×</button></div>
        <div class="panel-body">
          <div class="two-fields"><div class="field"><label for="database-kind">数据库类型</label><select id="database-kind" v-model="form.kind" :disabled="locked || !!source"><option value="gauss_database">Gauss / openGauss</option><option value="mysql_database">MySQL</option></select></div><div class="field"><label for="database-id">来源编号</label><input id="database-id" v-model="form.id" :disabled="locked || !!source" required maxlength="64" placeholder="例如：business_mysql"/></div></div>
          <div class="radio-line connection-mode"><label><input v-model="mode" type="radio" value="direct" :disabled="locked"/>直接连接</label><label><input v-model="mode" type="radio" value="environment" :disabled="locked"/>服务器环境变量（高级）</label></div>
          <template v-if="mode === 'direct'">
            <section class="connection-step"><h3><span>1</span>填写连接信息</h3><div class="radio-line connection-format"><label><input v-model="connection.format" type="radio" value="fields" :disabled="locked"/>分项填写</label><label><input v-model="connection.format" type="radio" value="dsn" :disabled="locked"/>连接串</label></div>
              <template v-if="connection.format === 'fields'"><div class="host-port"><div class="field"><label for="database-host">主机地址</label><input id="database-host" v-model="connection.host" :disabled="locked" placeholder="db.example.com" autocomplete="off"/></div><div class="field"><label for="database-port">端口</label><input id="database-port" v-model="connection.port" :disabled="locked" inputmode="numeric"/></div></div><div class="two-fields"><div class="field"><label for="database-name">数据库名</label><input id="database-name" v-model="connection.database" :disabled="locked" autocomplete="off"/></div><div class="field"><label for="database-schema">Schema（可选）</label><input id="database-schema" v-model="connection.schema" :disabled="locked" :placeholder="form.kind === 'mysql_database' ? '默认使用数据库名' : '例如：public'"/></div></div></template>
              <template v-else><div class="field"><label for="database-dsn">连接串</label><input id="database-dsn" v-model="connection.dsn" :disabled="locked" autocomplete="off" :placeholder="form.kind === 'mysql_database' ? 'mysql://用户名:密码@主机:3306/数据库' : 'jdbc:gaussdb://主机:8000/数据库'"/></div><div class="field"><label for="dsn-schema">Schema（可选）</label><input id="dsn-schema" v-model="connection.schema" :disabled="locked"/></div></template>
              <template v-if="connection.format === 'fields' || jdbcConnection"><div class="two-fields"><div class="field"><label for="database-username">用户名</label><input id="database-username" v-model="connection.username" :disabled="locked" autocomplete="off"/></div><div class="field"><label for="database-password">密码</label><input id="database-password" v-model="connection.password" :disabled="locked" type="password" autocomplete="new-password"/></div></div></template>
              <div v-if="form.kind === 'gauss_database'" class="field">
                <label for="database-sslmode">SSL 模式</label>
                <select id="database-sslmode" v-model="connection.sslmode" :disabled="locked" :aria-describedby="sslHint ? 'database-sslmode-hint' : undefined">
                  <option value="auto">自动（沿用连接串或默认设置）</option>
                  <option value="disable">disable · 不使用 SSL</option>
                  <option value="allow">allow · 优先非 SSL</option>
                  <option value="prefer">prefer · 优先 SSL</option>
                  <option value="require">require · 必须使用 SSL</option>
                  <option value="verify-ca">verify-ca · 校验证书</option>
                  <option value="verify-full">verify-full · 校验证书和主机名</option>
                </select>
                <p v-if="sslHint" id="database-sslmode-hint" class="field-hint">{{ sslHint }}</p>
              </div>
              <div class="actions"><button type="button" class="btn" :disabled="locked" @click="testConnection">测试连接</button><span v-if="tested" class="badge green" role="status">连接成功</span></div><div class="field"><label class="checkbox-line"><input id="remember-connection" v-model="rememberConnection" type="checkbox" :disabled="locked"/>保存连接信息到本机</label><p class="field-hint">{{ rememberConnection ? '添加或更新成功后加密保存，下次打开自动加载。' : '本次连接仅在会话内使用，更新后会删除此前保存的连接。' }}</p><button v-if="savedConnection" type="button" class="btn small" :disabled="locked" @click="deleteSavedConnection">删除已保存连接</button></div>
            </section>
            <section class="connection-step"><div class="selection-head"><h3><span>2</span>选择要分析的表</h3><button type="button" class="btn small" :disabled="locked" @click="discoverTables">{{ tableLoaded ? '刷新表清单' : '读取表清单' }}</button></div><div v-if="tableLoaded" class="table-picker"><input v-model="filter" aria-label="筛选数据库表" placeholder="按表名筛选" :disabled="locked"/><div class="table-picker-head"><span>{{ visibleTables.length }} 张表</span><button type="button" :disabled="locked || !visibleTables.length" @click="toggleVisible">选择 / 取消当前筛选</button></div><div class="table-picker-items"><label v-for="table in visibleTables" :key="table.qualified_name" class="table-choice"><input v-model="checked" type="checkbox" :value="table.qualified_name" :disabled="locked"/><span>{{ table.qualified_name }}</span><small>{{ table.table_type }}</small></label><p v-if="!visibleTables.length" class="empty-state">没有匹配的表</p></div><p v-if="skippedCount" class="field-hint">已跳过 {{ skippedCount }} 张不支持表名的表。</p><p v-if="truncated" class="note amber">表清单已达到服务端限制，可缩小 Schema 范围后重试，或补充手动表名。</p></div>
              <div class="field manual-tables"><div class="selection-head"><label for="database-tables">补充表名</label><button type="button" class="btn small" :disabled="locked" @click="listFileInput?.click()">从 Excel 导入表清单</button><input ref="listFileInput" type="file" hidden accept=".xlsx" @change="importTableList"/></div><textarea id="database-tables" v-model="form.tables" :disabled="locked" :placeholder="form.kind === 'mysql_database' ? '每行一张表，例如：\nanalytics.metrics\nanalytics.organizations' : '每行一张表，例如：\npublic.metrics\npublic.organizations'"/><p class="field-hint">已选 {{ tableCount }} 张表</p></div>
            </section>
          </template>
          <template v-else><div class="field"><label for="database-env">数据库连接环境变量名</label><input id="database-env" v-model="form.dsnEnv" :disabled="locked" required placeholder="LIGHTGRAPH_MYSQL_DSN" autocomplete="off"/></div><div class="field"><label for="environment-tables">允许查询的表</label><textarea id="environment-tables" v-model="form.tables" :disabled="locked" required placeholder="每行一张表，包含 Schema；不使用通配符。"/></div></template>
          <details class="connection-advanced"><summary>运行权限与高级选项</summary><div class="field"><label for="database-manifest">能力验证文件（可选）</label><select id="database-manifest" v-model="form.capabilityManifest" :disabled="locked"><option value="">暂不指定</option><option v-if="missingManifest" :value="form.capabilityManifest" disabled>原文件不在上传列表，请重新选择</option><option v-for="file in manifests" :key="file.file_id" :value="file.relative_path">{{ file.name }}</option></select></div><div class="retention-box"><label class="checkbox-line"><input v-model="retention" type="checkbox" :disabled="locked"/>此项目的部署环境已完成数据留存检查</label><p class="field-hint">此项影响真实实例数据的抽取。未确认时仍可连接并读取元数据。</p></div><p v-if="source?.allowed_columns" class="field-hint">已有列级访问限制会保留。</p></details>
        </div>
        <div v-if="localError || error || pending || tested || notice" class="dialog-feedback" aria-live="polite">
          <p v-if="localError || error" class="note amber" role="alert">{{ localError || error }}</p>
          <p v-else-if="pending" class="note" role="status">{{ progress }}</p>
          <p v-else-if="notice" class="note success" role="status">{{ notice }}</p>
          <p v-else-if="tested" class="note success" role="status">连接成功{{ tableLoaded ? `，已读取 ${tables.length} 张表` : '' }}</p>
        </div>
        <div class="panel-footer"><span>{{ mode === 'direct' ? rememberConnection ? '连接信息加密保存在运行服务的本机，不写入项目 YAML' : '连接信息仅用于本次会话' : '保存后可在数据接入页读取元数据' }}</span><button type="submit" class="btn primary" :disabled="locked">{{ pending ? '处理中…' : mode === 'direct' ? source ? '更新并抽取元数据' : '添加并抽取元数据' : '保存数据库来源' }}</button></div>
      </form>
    </section>
  </div>
</template>
