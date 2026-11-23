const messages: Record<string, string> = {
  sql_preview_model_unavailable: '请先配置抽取模型和 API 密钥，再试写 SQL。子图上下文可在下方查看。',
  sql_preview_timeout: 'SQL 试写超时。可以先查看已召回的子图。',
  sql_preview_model_failed: 'DSH 未能完成本次生成。请检查项目模型连接。',
  sql_preview_invalid_output: '模型草稿未通过 SQL 语法或来源字段检查。',
  sql_preview_context_too_large: '子图上下文超出模型输入上限，请缩小问题范围。',
  graph_generation_changed: '图谱版本或访问配置已变化，已停止合并旧数据，请重新加载。',
  graph_batch_invalid: '图谱批次不一致，加载已停止，请重新加载。',
  graph_batch_incomplete: '返回的图谱数量不完整，加载已停止，请重新加载。',
  construction_scoped_access_not_supported: '该历史运行使用的版本尚不支持此访问范围。当前版本已支持逐条来源证据过滤，请保留原权限配置。',
  scope_binding_conflict: '同一来源表存在不同的范围列绑定，请核对 scope_bindings 后重试。',
  graph_support_visibility_missing: '该图缺少逐条来源证据的可见范围，请按当前绑定重新验证和发布。',
  graph_read_conflict: '图谱读取期间项目配置已变化，请重新加载图谱。',
  graph_access_context_changed: '当前授权无法读取此图。若只移动过目录，可在历史版本中核验原项目路径。',
  graph_version_not_found: '找不到此历史版本，请刷新版本列表。',
  graph_access_migration_evidence_mismatch: '原项目信息与发布记录不一致，无法核验旧图。',
  workflow_checkpoint_invalid: '该节点的存档缺失或校验不通过，请重新运行生成它的节点。',
  workflow_resume_input_changed: '需要复用的上游节点已变更，请从变更的节点重新运行。',
  workflow_resume_source_invalid: '请选择已结束的工作流运行。',
  workflow_checkpoint_missing: '没有可复用的上游节点存档，请从该上游节点重新运行。',
  workflow_resume_state_missing: '找不到属于这次运行的节点记录，无法从这里恢复。',
  workflow_terminal_artifact_required: '仅能直接发布工作流唯一输出节点已提交的完整图。',
  workflow_legacy_merge_unavailable: '这次旧运行不满足原合并产物恢复条件，可查看已有历史版本。',
  workflow_legacy_worker_unverified: '无法将旧合并节点的执行记录与这次运行对应，已保留原图。',
  workflow_legacy_upstream_unverified: '旧合并节点的上游快照无法核验，不能重新组合。',
  workflow_legacy_delta_unverified: '旧合并节点的已提交增量无法核验，不能恢复发布。',
  invalid_previous_project_root: '请填写搬迁前的项目绝对路径，包括原盘符或根目录。',
  workflow_partial_artifact_unverified: '缺少可验证的部分发布记录，已保留原项目图。',
  workflow_checkpoint_immutable: '同一运行已有不同的节点存档，请用新的运行恢复。',
  network_unavailable: '无法连接服务，请确认后端已启动。', invalid_response: '服务返回的内容无法读取，请刷新后重试。',
  request_timeout: '请求超时，请检查后端服务、数据库地址和网络后重试。',
  request_cancelled: '请求已取消。',
  invalid_model_limits: '模型额度配置无效，请检查输入、输出、超时和可选字节限制。',
  run_trace_not_found: '此运行没有可导出的轨迹，可能未记录或已被手动清理。',
  run_trace_page_invalid: '轨迹分页参数无效，请重新打开运行记录。',
  run_trace_invalid: '轨迹包含无法读取的记录，已保存的文件仍保留。',
  run_trace_active: '运行中不能清理轨迹，请等待运行结束。',
  run_trace_export_active: '轨迹正在导出，请等待下载完成后再清理。',
  trace_write_failed: '轨迹写入失败，已有记录会保留；请检查磁盘空间和文件权限。',
  run_trace_incomplete: '轨迹未完整记录，只能查看和导出已保存的部分。',
  project_not_found: '项目不存在，请重新选择。', project_exists: '项目编号已存在，请更换编号。',
  invalid_project_id: '项目编号只能包含字母、数字、短横线和下划线。',
  invalid_project_or_file_id: '项目编号须以字母或数字开头，其余只能包含字母、数字、短横线和下划线。',
  config_revision_required: '请重新读取项目配置后再操作。',
  config_revision_conflict: '配置已被其他页面修改，本地草稿已保留。请检查服务器配置后重新加载。',
  capability_manifest_invalid: '能力清单格式无效，或不包含当前来源编号。',
  runtime_capability_unverified: '数据库的只读、超时或取消能力未通过检查，请核对数据库权限和驱动后重试。',
  source_circuit_open: '该来源已停止本次查询，请检查数据库状态后重新运行；已有草稿保留。',
  source_retry_limit_exhausted: '重复请求已停止的来源，运行已进入收尾；请恢复数据库后重新运行。',
  database_reconnect_failed: '数据库重连失败，请查看进程诊断并核对数据库连接状态。',
  source_fact_data_forbidden: '此数据库仅读取元数据。需要抽取数据时，请在数据库配置中勾选“允许读取事实数据”并保存。',
  metadata_only_query_forbidden: '当前仅允许读取元数据，请检查数据库的事实数据选项及模型模式。',
  resolution_revision_conflict: '候选裁决已被其他页面修改，请刷新候选后核对。',
  resolution_version_conflict: '图谱版本或业务范围已变化，请重新加载当前发布图后核对候选。',
  resolution_scope_conflict: '所选对象和关系的可见范围不同，这次裁决无法应用到整张图。',
  resolution_cluster_review_required: '所选对象已参与归并，请把同组原对象一起选入，或先撤销原裁决。',
  resolution_decision_conflict: '所选对象已有生效的裁决，请核对裁决记录。',
  resolution_hierarchy_conflict: '归并后关系会指向自身，或无法确认层级仍然无环。请核对关系方向和端点。',
  resolution_relation_conflict: '关系端点与发布的类型定义不符，请核对原关系。',
  resolution_ledger_conflict: '裁决记录与当前发布版本不符，无法读取或保存裁决。',
  resolution_inherited_delta_conflict: '这个旧版本缺少继承图的版本记录，请在有完整继承记录的发布图上裁决。',
  invalid_resolution_members: '请在这组候选中选择 2 至 256 个不同的原对象。',
  resolution_attribute_conflict: '对象的定义属性存在冲突，暂不能确认为同一对象。',
  resolution_type_conflict: '所选对象的类型或角色不支持这次归并。',
  resolution_cannot_link_conflict: '这组对象已有不同对象裁决，请先核对并撤销冲突裁决。',
  resolution_hierarchy_self_loop: '归并会使上下级关系指向自身，请重新检查。',
  resolution_requires_accepted_endpoints: '候选对象尚未接受，请先完成语义确认。',
  resolution_evidence_changed: '候选的来源依据已变化，请重新运行并检查。',
  config_invalid: '配置未通过校验，请检查来源、模型和抽取范围。',
  invalid_project_config: '项目配置未通过校验，请检查以下字段。',
  config_path_must_be_unambiguous_project_file: '配置中的文件未在当前项目注册，请从数据接入或知识文档页面上传后再选择。',
  config_path_must_be_project_file: '配置路径必须指向当前项目已上传的文件。',
  model_credential_missing: '请在模型配置中保存 API key，或配置服务器环境变量。', source_credential_missing: '请重新连接数据库，或检查服务器的连接环境变量。',
  session_required: '页面会话已失效，请刷新后继续，已保存的密钥和连接信息会自动恢复。', session_invalid: '页面会话已失效，请刷新后继续，已保存的密钥和连接信息会自动恢复。',
  credential_session_invalid: '页面会话已失效，请刷新后继续，已保存的密钥和连接信息会自动恢复。', credential_session_capacity_reached: '服务当前会话已满，请稍后重试。',
  model_endpoint_required: '请先填写并保存模型服务地址和模型名称。',
  model_credential_binding_conflict: '模型服务地址或名称已变化，密钥未保存。请重新加载配置并核对密钥对应的服务。',
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
  table_list_yaml_invalid: 'YAML 表清单格式错误。请在 tables 下逐行列出表名。',
  database_table_list_too_large: '表清单文件过大，请减少表名后重试。',
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
  metadata_preview_persistence_failed: '项目元数据副本保存失败，操作未完成。请检查工作目录的写入权限后重试。',
  deployment_retention_unverified: '连接真实数据库前，需要完成部署环境的数据留存检查。',
  run_already_active: '此项目正在运行，请等待完成或先取消。', project_busy: '项目正在运行，暂时无法修改。',
  project_run_active: '此项目正在运行，请等待完成或先取消。', extraction_purpose_required: '请先填写抽取目的。',
  project_inputs_required: '请先上传数据或知识文档。',
  file_in_use: '此文件仍被来源配置引用，请先移除相关来源或修改配置。',
  database_source_required: '仅数据库来源支持单独移除数据表。',
  document_binding_requires_metadata: '请先读取元数据，再重新绑定文档。',
  ambiguous_document_table_binding: '存在多张同名表，请按来源和 Schema 选择具体表。',
  invalid_document_source_binding: '限定的来源已不可用，请重新选择文档范围。',
  invalid_document_column_binding: '文档中的列限制与所选表不匹配，请核对列名和表名。',
  document_section_scope_expansion: '章节引用的表或列超出了文档范围，请核对章节设置和关联表。',
  invalid_document_manifest: '知识文档清单校验失败，请核对以下条目。',
  upload_utf8_required: '请用 UTF-8 编码保存文档和清单。',
  upload_too_large: '文件超过上传大小限制，请缩小文件后再导入。',
  request_too_large: '本次导入超过请求大小限制，请拆分清单后再导入。',
  project_file_in_use: '此文件仍被来源配置引用，请先移除相关来源或修改配置。',
  source_not_found: '数据来源不存在，请刷新项目。',
  source_table_not_found: '数据表不在当前来源中，请刷新项目。',
  file_type_not_supported: '文件类型不支持，请选择 XLSX、CSV、Markdown 或 YAML。',
  unsupported_file_type: '文件类型不支持，请选择 XLSX、CSV、Markdown 或 YAML。',
  file_too_large: '文件超过服务器上传限制。', file_not_found: '文件不存在，请刷新文件列表。',
  scope_invalid: '文档绑定无效，请重新选择表。', invalid_document_table_binding: '文档绑定的表不在当前数据源中。',
  source_scope_missing: '数据库来源需要填写允许查询的表。', credentials_not_allowed: '配置中不能包含明文密钥，请使用服务器环境变量。',
  embedding_service_unavailable: '无法连接配置的向量嵌入服务，请确认服务地址和端口已启动。',
  embedding_timeout: '向量服务未在单批请求时限内响应。请查看服务负载或调整单批超时。',
  embedding_endpoint_required: '请填写并保存向量服务的 Base URL 和模型名称。',
  embedding_response_invalid: '向量服务返回格式或向量维度无效，请检查是否兼容 /v1/embeddings 接口。',
  embedding_insecure_endpoint: '非本机的 HTTP 向量服务需勾选“允许使用此内部 HTTP 服务”后才可调用。',
  embedding_provider_unsupported: '不支持的向量提供方配置。',
  embedding_credential_binding_conflict: '向量服务地址或名称已变更，密钥未保存，请重新加载配置并核对。',
  invalid_embedding_config: '向量服务配置格式无效，请检查服务地址、模型名称和批次大小。',
  retrieval_index_stale: '索引尚未建立或已过期，请重新向量化当前图谱。',
  invalid_business_context_selection: '内容适用范围无效，请重新选择。',
  business_context_invalid: '内容适用范围无效，请重新选择。',
  inherited_lookup_context_unmatched: '继承图中有匹配对象，但其依据不适用于当前事实的范围。请核对引用及适用条件。',
  inherited_lookup_context_conflict: '关系两端的依据适用条件冲突，尚不能建立这条关系。',
  context_cycle_analysis_incomplete: '限定关系的环检测尚未完成，当前不能确认无环。',
  retrieval_graph_required: '请先生成业务图谱，再执行向量化。',
  retrieval_job_active: '当前项目正在向量化，请在任务结束后重试。',
  retrieval_jobs_busy: '向量化任务正在运行，请稍后重试。',
  retrieval_job_interrupted: '上次向量化随服务重启中断，已有索引保留，可重新启动。',
  retrieval_build_failed: '向量化失败，已有索引保留，请检查向量服务后重试。',
  workflow_not_enabled: '当前项目尚未启用并保存并行工作流配置。',
  workflow_unit_not_found: '指定的并行抽取链路不存在，请刷新工作流配置。',
  workflow_units_not_ready: '合并所需的上游结果尚未就绪，请查看对应节点的运行记录。',
  workflow_unit_publication_missing: '节点已结束，但缺少已发布的图谱。请查看该节点的记录并从该节点重新运行。',
  workflow_input_changed: '并行工作流配置或输入已变化，无法仅重跑单条链路，请先执行一次完整并行抽取。',
  workflow_merge_input_required: '合并节点缺少上游输入，请在画布中连接抽取或合并节点。',
  workflow_dag_requires_run: '请重新运行工作流以更新合并节点的结果。',
  workflow_execution_failed: '并行工作流协同执行失败，请查看各链路状态与日志。',
  workflow_units_failed: '所有并行链路均未成功产出，请查看链路错误信息。',
  workflow_unit_partial: '部分并行链路未完整完成，可单独重跑失败链路。',
  inherited_graph_scope_not_authorized: '所选继承基准图谱的作用域超出当前项目授权范围。',
  inherited_graph_digest_mismatch: '继承基准图谱的摘要与预期不一致，已拦截读取。',
  inherited_graph_not_found: '指定的继承基准图谱不存在或尚未发布有效图谱。',
}
export class ApiError extends Error {
  constructor(public code: string, public status = 0, diagnostics: unknown = [], nodeId?: unknown) {
    const fields = Array.isArray(diagnostics) ? diagnostics.slice(0, 16).filter(item => item && typeof item.path === 'string' && /^[A-Za-z0-9_.\[\]<>-]{1,160}$/.test(item.path) && typeof item.hint === 'string' && item.hint.length <= 200).map(item => `${item.path}：${item.hint}`) : []
    const node = typeof nodeId === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(nodeId) ? [`涉及节点：${nodeId}`] : []
    super([...node, messages[code] || `操作未完成（${code}）`, ...fields].join('\n')); this.name = 'ApiError'
  }
}
// Tokens and credentials stay in this page's memory, never browser storage.
const sessions = new Map<string, string>()
const pendingSessions = new Map<string, Promise<void>>()
function sessionHeader(path: string): Record<string, string> {
  const project = path.match(/^\/projects\/([^/]+)(?:\/|$)/)?.[1]
  const token = project ? sessions.get(project) : undefined
  return token ? { 'X-Workbench-Session': token } : {}
}
export function clearSession(projectId: string) { const id = encodeURIComponent(projectId); sessions.delete(id); pendingSessions.delete(id) }
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
// Revision metadata belongs to the response object, never the editable config.
const responseRevisions = new WeakMap<object, string>()
export function responseRevision(value: unknown): string | undefined {
  return value !== null && typeof value === 'object' ? responseRevisions.get(value) : undefined
}
export type RequestOptions = RequestInit & { timeoutMs?: number | null; responseType?: 'json' | 'blob' }
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { timeoutMs = 60000, responseType = 'json', signal, ...init } = options
  const controller = new AbortController()
  let timedOut = false
  const abort = () => controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  if (signal?.aborted) abort()
  // Bound both waiting for headers and reading the response body. Never retry writes automatically.
  const timer = timeoutMs === null ? undefined : setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
  try {
    const response = await fetch(`/api${path}`, { ...init, signal: controller.signal, credentials: 'same-origin', cache: 'no-store',
      headers: { ...sessionHeader(path), ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...options.headers } })
    if (response.ok && responseType === 'blob') return await response.blob() as T
    let data: unknown
    try { data = await response.json() } catch { throw new ApiError('invalid_response', response.status) }
    if (!response.ok) {
      const code = (data as { error?: { code?: unknown } })?.error?.code
      // Render only the short public error code; never reflect raw server errors or HTML.
      throw new ApiError(typeof code === 'string' && /^[a-zA-Z0-9_.:-]{1,100}$/.test(code) ? code : `http_${response.status}`, response.status,
        ['invalid_project_config', 'invalid_model_limits', 'invalid_document_manifest'].includes(String(code)) ? (data as { error?: { diagnostics?: unknown } })?.error?.diagnostics : [], (data as { error?: { node_id?: unknown } })?.error?.node_id)
    }
    if (data !== null && typeof data === 'object' && response.headers.get('etag')) responseRevisions.set(data, response.headers.get('etag')!)
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
