const messages: Record<string, string> = {
  network_unavailable: '无法连接服务，请确认后端已启动。', invalid_response: '服务返回的内容无法读取，请刷新后重试。',
  project_not_found: '项目不存在，请重新选择。', project_exists: '项目编号已存在，请更换编号。',
  invalid_project_id: '项目编号只能包含字母、数字、短横线和下划线。',
  invalid_project_or_file_id: '项目编号须以字母或数字开头，其余只能包含字母、数字、短横线和下划线。',
  config_invalid: '配置未通过校验，请检查来源、模型和抽取范围。',
  model_credential_missing: '服务器未配置模型密钥环境变量。', source_credential_missing: '服务器未配置数据库连接环境变量。',
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
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, { ...options, credentials: 'same-origin', cache: 'no-store',
      headers: { ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...options.headers } })
  } catch { throw new ApiError('network_unavailable') }
  let data: unknown
  try { data = await response.json() } catch { throw new ApiError('invalid_response', response.status) }
  if (!response.ok) {
    const code = (data as { error?: { code?: unknown } })?.error?.code
    // Render only the short public error code; never reflect raw server errors or HTML.
    throw new ApiError(typeof code === 'string' && /^[a-zA-Z0-9_.:-]{1,100}$/.test(code) ? code : `http_${response.status}`, response.status)
  }
  return data as T
}
export const json = (value: unknown): string => JSON.stringify(value)
export const projectPath = (id: string): string => `/projects/${encodeURIComponent(id)}`
