const messages: Record<string, string> = {
  network_unavailable: '无法连接服务，请确认后端已启动。', invalid_response: '服务返回的内容无法读取，请刷新后重试。',
  request_timeout: '请求超时，请检查后端服务、数据库地址和网络后重试。',
  request_cancelled: '请求已取消。',
  project_not_found: '项目不存在，请重新选择。', project_exists: '项目编号已存在，请更换编号。',
  invalid_project_id: '项目编号只能包含字母、数字、短横线和下划线。',
  invalid_project_or_file_id: '项目编号须以字母或数字开头，其余只能包含字母、数字、短横线和下划线。',
  config_invalid: '配置未通过校验，请检查来源、模型和抽取范围。',
  model_credential_missing: '请在 Agent 与模型中填写当前会话的 API key，或配置服务器环境变量。', source_credential_missing: '请重新连接数据库，或检查服务器的连接环境变量。',
  session_required: '连接会话已失效，请刷新页面后重新填写凭据。', session_invalid: '连接会话已失效，请刷新页面后重新填写凭据。',
  credential_session_invalid: '连接会话已失效，请刷新页面后重新填写凭据。', credential_session_capacity_reached: '服务当前会话已满，请稍后重试。',
  model_endpoint_required: '请先填写并保存模型服务地址和模型名称。',
  database_tls_required: '远程数据库要求 TLS 证书校验，请使用包含 TLS 配置的连接串。',
  database_sslmode_invalid: '连接串中的 sslmode 无效，请修正该参数，或删除该参数后在表单中选择。',
  database_sslmode_not_supported: '此 SSL 模式配置仅用于 Gauss 数据库。',
  database_host_invalid: '数据库主机地址无效，请检查后重试。',
  database_catalog_encoding_invalid: '数据库元数据编码无法识别，请检查 UTF-8 编码设置。',
  database_catalog_boolean_invalid: '数据库元数据中的布尔值格式不支持，请检查数据库兼容设置。',
  database_identifier_invalid: '表名或 Schema 格式不支持，请检查表清单。',
  database_table_schema_mismatch: '选中的表与连接的 Schema 不一致，请检查表名。',
  database_table_selection_invalid: '请明确列出要读取的表，不使用通配符。',
  source_column_scope_conflict: '已有列级权限引用了未选中的表，请保留这些表或先调整完整配置。',
  source_kind_conflict: '已有来源不能更换数据库类型，请使用新编号接入另一数据库。',
  source_id_conflict: '来源编号已被其他来源使用，请更换编号。',
  source_id_invalid: '来源编号只能包含字母、数字、短横线和下划线。',
  read_only_transaction_unverified: '无法确认数据库只读事务，连接已停止。请检查账号和数据库设置。',
  table_list_empty: 'Excel 中没有找到表名，请使用表名列或 Schema 与表名两列。',
  table_list_invalid: '表清单无法解析，请使用 XLSX 文件。',
  table_list_formula_forbidden: '表清单不能包含公式，请先粘贴为纯文本值。',
  table_list_size_limit: '表清单超过服务端限制，请缩小范围后导入。',
  database_connection_failed: '数据库连接失败，请检查连接信息和网络。', database_connection_invalid: '请检查数据库地址、端口、库名和账号。',
  saved_connection_invalid: '保存的连接信息格式无效，请重新填写。',
  saved_connection_corrupt: '保存的连接信息无法解密，请删除后重新连接。',
  saved_connection_key_missing: '本机连接密钥缺失，请恢复密钥或删除保存的连接后重新填写。',
  saved_connection_storage_failed: '连接信息保存或加载失败，请检查本机存储权限。',
  saved_connection_path_unsafe: '连接存储路径不安全，请检查服务端配置。',
  database_connection_fields_required: '请填写数据库地址、库名和用户名。',
  database_connection_credentials_conflict: '连接串中的账号或密码与单独填写的内容不同，请只保留一处或填写一致。',
  database_connection_option_forbidden: '连接串包含不支持的参数，请使用主机、端口、数据库和账号信息。',
  database_metadata_failed: '连接已处理，但元数据读取失败，请检查授权表与只读账号权限。',
  deployment_retention_unverified: '连接真实数据库前，需要完成部署环境的数据留存检查。',
  run_already_active: '此项目正在运行，请等待完成或先取消。', project_busy: '项目正在运行，暂时无法修改。',
  project_run_active: '此项目正在运行，请等待完成或先取消。', extraction_purpose_required: '请先填写抽取目的。',
  project_inputs_required: '请先上传数据或知识文档。',
  file_type_not_supported: '文件类型不支持，请选择 XLSX、CSV、Markdown 或 YAML。',
  unsupported_file_type: '文件类型不支持，请选择 XLSX、CSV、Markdown 或 YAML。',
  file_too_large: '文件超过服务器上传限制。', file_not_found: '文件不存在，请刷新文件列表。',
  scope_invalid: '文档绑定无效，请重新选择表。', invalid_document_table_binding: '文档绑定的表不在当前数据源中。',
  source_scope_missing: '数据库来源需要填写允许查询的表。', credentials_not_allowed: '配置中不能包含明文密钥，请使用服务器环境变量。',
}
export class ApiError extends Error {
  constructor(public code: string, public status = 0) { super(messages[code] || `操作未完成（${code}）`); this.name = 'ApiError' }
}
// Tokens and credentials stay in this page's memory, never browser storage.
const sessions = new Map<string, string>()
const pendingSessions = new Map<string, Promise<void>>()
function sessionHeader(path: string): Record<string, string> {
  const project = path.match(/^\/projects\/([^/]+)(?:\/|$)/)?.[1]
  const token = project ? sessions.get(project) : undefined
  return token ? { 'X-Workbench-Session': token } : {}
}
export function clearSessions() { sessions.clear(); pendingSessions.clear() }
export async function ensureSession(projectId: string): Promise<void> {
  const id = encodeURIComponent(projectId)
  if (sessions.has(id)) return
  if (pendingSessions.has(id)) return pendingSessions.get(id)
  const pending = request<{ session_id: string }>(`${projectPath(projectId)}/session`, { method: 'POST', body: '{}', timeoutMs: 10000 }).then(response => {
    if (!response.session_id) throw new ApiError('invalid_response')
    sessions.set(id, response.session_id)
  }).finally(() => pendingSessions.delete(id))
  pendingSessions.set(id, pending)
  return pending
}
export type RequestOptions = RequestInit & { timeoutMs?: number }
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { timeoutMs = 60000, signal, ...init } = options
  const controller = new AbortController()
  let timedOut = false
  const abort = () => controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  if (signal?.aborted) abort()
  // Bound both waiting for headers and reading the response body. Never retry writes automatically.
  const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
  try {
    const response = await fetch(`/api${path}`, { ...init, signal: controller.signal, credentials: 'same-origin', cache: 'no-store',
      headers: { ...sessionHeader(path), ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...options.headers } })
    let data: unknown
    try { data = await response.json() } catch { throw new ApiError('invalid_response', response.status) }
    if (!response.ok) {
      const code = (data as { error?: { code?: unknown } })?.error?.code
      // Render only the short public error code; never reflect raw server errors or HTML.
      throw new ApiError(typeof code === 'string' && /^[a-zA-Z0-9_.:-]{1,100}$/.test(code) ? code : `http_${response.status}`, response.status)
    }
    return data as T
  } catch (error) {
    if (timedOut) throw new ApiError('request_timeout')
    if (controller.signal.aborted) throw new ApiError('request_cancelled')
    if (error instanceof ApiError) throw error
    throw new ApiError('network_unavailable')
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}
export const json = (value: unknown): string => JSON.stringify(value)
export const projectPath = (id: string): string => `/projects/${encodeURIComponent(id)}`
