# data-graph · 轻量知识图谱

从 Gauss 数据库、Excel 和 CSV 中发现实体列组、层级和引用规则，再用规则生成图谱。一次运行可以混合这些来源。Markdown 可以提供全局说明、表列定义和带出处与条件的分析知识。图中分开显示业务实体、表与列、文档来源。

**运行源码、测试和页面模板均在 `src/lightgraph/`。** 本项目不需要图数据库。SQLite 只保存抽取结果、规则、来源支持和发布记录；数据库查询返回的行、抽样、模型请求和响应不会进入结果库。

依赖的本轮实测版本记录在 `requirements.lock`；需要复现环境时先安装该文件，再安装项目。

当前运行能力与验证边界见 [实施与验收记录](IMPLEMENTATION_STATUS.md)。架构依据是 [设计 v1.0](design/lightweight_graph/README.md)。

工作台页面设计见 [交互与接入设计](design/lightweight_graph/WORKBENCH_DESIGN.md) 和 [用户故事](design/lightweight_graph/WORKBENCH_US.md)。本轮提供独立的合成可交互原型，未接入真实数据库、模型或 Web 后端：

```bash
python3 -m http.server 4173 --bind 127.0.0.1 --directory src/lightgraph/workbench_prototype
```

打开 `http://127.0.0.1:4173`。原型可编辑目的与文档范围、演示发现和取消、查看两类图及运行记录；配置仅保留在页面内存。说明与验收见 [原型 README](src/lightgraph/workbench_prototype/README.md)。

公开仓库包含源码、测试、设计文档和示例配置。合成 Excel、运行结果与安装包由下面的命令在本地生成；不会随源码提交。

## 安装与小实验

需要 Python 3.11 或更新版本。在项目目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
.venv/bin/lightgraph verify --output runs/acceptance --rows 5000
```

`verify` 分别执行固定规则实验 E 和自动发现实验 D，并核对共享实体、缺层、同层兄弟节点、指标与度量关系、重复运行及输出中不含排除列内容。它使用合成数据，不调用模型。真实 Gauss 和真实模型验收单独记为未执行。

结果位于 `runs/acceptance/acceptance.json`，两份可视化在 `runs/acceptance/E/viewer.html` 和 `runs/acceptance/D/viewer.html`。验收目录必须为空，避免把两次实验混在一起。

## Excel、CSV 与 Markdown

生成一个含 8 张表、约 5,000 行的合成输入包：

```bash
.venv/bin/lightgraph sample examples/sample --rows 5000
.venv/bin/lightgraph run examples/run.yaml
.venv/bin/lightgraph run examples/discovery.yaml
```

`run.yaml` 使用已写好的规则；`discovery.yaml` 根据数据和说明文档发现规则。两者使用不同结果目录。只发现并验证规则、暂不保存图谱时：

```bash
.venv/bin/lightgraph discover examples/discovery.yaml
```

随后可将生成的 `rules.yaml` 配入 `rules:`，或直接再次 `run`：正常运行会从同一结果库复用依赖仍有效的规则，重新查询当前数据。新增判别值、schema 或说明文档变化会进入复核。复用规则不等于复用数据库查询结果。分析文档只修改一个章节时，仅重抽受影响章节；上级条件改变会使相应子章节失效。章节缓存只含已形成的候选主张，不保存抽样或模型原文。

Excel 每张工作表对应一张表；支持常规表头、前导零、日期、隐藏行列和已有公式缓存。关键列缺公式缓存会阻止发布。合并表头、多块区域需要先拆成明确的工作表；程序不执行 Excel 公式或外链。

CSV 必须明确指定编码、分隔符、是否有表头、NULL 标记和类型解析方式。当前只支持 `type_inference: text`，保留 `001` 等前导零和原有空格；NULL 标记按完整字段匹配。无表头时还需配置 `columns`。编码错误、重复表头、列数不一致和格式错误会停止解析，不会自动修正。Excel 与 CSV 使用内存 SQLite，不生成解析缓存或临时数据库文件。

先生成上面的合成输入包，再运行 [混合文件示例](examples/mixed_sources.yaml)：

```bash
.venv/bin/lightgraph discover examples/mixed_sources.yaml
```

示例同时读取 Excel 和仓库中的合成 CSV，无需数据库或模型密钥。各来源的 `id` 必须唯一；表的完整身份由 `source_id`、`schema` 和表名组成。跨源比较分别在两端执行必要列的 DISTINCT，再在有上限的内存中核验复合键、NULL、未匹配和歧义。两端覆盖不足、预算用尽或一致性无法证明时返回 `unresolved`，不能发布完整引用或据此撤回旧结果。比较使用各来源独立的一致快照，不提供分布式同一时点快照，也不执行任意联邦 SQL。当前自动跨表发现最多处理 64 张参与表和 64 个已验证实体端点；超限明确延期，尚未实现跨表端点分页。

Markdown 用 front matter 分开声明用途和范围：

```yaml
---
purpose: data_description
scope:
  source_ids: [gauss]
  tables: [gauss.finance.report_item, gauss.finance.report_tree]
---
```

`data_description` 提供定义依据；`analysis_knowledge` 可以指导探索，也独立抽取相关分析主张；`mixed` 允许按章节区分用途。未绑定表列的文档在当前授权目录内全局适用，正文不必出现表名。`scope` 可限定来源、表和列；兼容 `applies_to`，但短表名必须能唯一解析。不存在或有歧义的绑定报错，不会回退为全局文档。

`sections` 用完整标题路径配置章节的 `purpose` 和 `scope`，子章节继承或收窄父级范围。示例见 [mixed_context.md](examples/mixed_context.md)。跨表任务会补齐实际涉及表的说明；检索得到的章节登记文档版本、位置、范围和完整读取状态，编译时再次核对。必要上下文超过预算时延期，不截掉定义、条件或例外。文档中的建议、可能性和适用条件会保留；分析主张默认是候选，分析经验不能单独批准实体共享。自动生成的说明标记为 `derived`，不会充当独立证据。

## Gauss 接入

参考 [examples/gauss.yaml](examples/gauss.yaml)。数据库连接串通过配置指定的环境变量读取。模型密钥可用环境变量，也可放在不提交 Git 的本地 Agent 配置中，详见下面的模型配置；密钥不进入运行报告。

```bash
.venv/bin/lightgraph connect-check examples/gauss.yaml
.venv/bin/lightgraph connect-check examples/gauss.yaml --verify-runtime --output gauss-capabilities.json
```

第二条命令包含受控的超时和取消探针，适用于已批准的隔离测试环境。能力清单绑定实际产品、版本和驱动；变化后必须重新检查。配置 `capability_manifest` 指向清单，并在客户完成运行环境保留检查后设置 `deployment_retention_verified: true`，再执行 `run`。

只读账号权限、只读事务、表列白名单、工具内 SQL AST 检查共同限制源库操作。发现 Agent 可以调用统计工具，也可以提交只读 SQL；两者都经过同一查询检查，不通过的 SQL 整条拒绝。系统检查查询次数、返回行数、字节和截止时间，不把截断当作完整分区。`LIMIT` 不能限制扫描成本；实际 Gauss 的超时和取消仍需环境验收。租户行权限应由数据库账号、授权视图或数据库行策略提供，`goal.source_scope` 和结果导出的 `scopes` 都不能替代源库权限。

## 抽取方向与发现 Agent

运行前在 `goal` 中说明本次要构建什么图。下面的配置不指定业务表名或字段映射：

```yaml
goal:
  purpose: 构建经营分析所需的指标、度量和维度图谱
  include: [指标定义与计算依赖, 维度成员与上下级关系]
  exclude: [每笔交易记录, 具体收入与成本观测值]
  discovery_guidance:
    - 结合元数据、统计、样本和说明文档判断列组
    - 规则验证后对必要列去重，不逐行调用模型
  observation_values: false
```

启用模型后，每次发现、元数据解释、反例修订和文档抽取调用都携带目标、阶段和子任务。Agent 提出假设、调用工具、读取临时结果，再决定继续查询还是提交结构化规则候选；程序随后验证规则并批量构图。工具根据参数和 SQL AST 更新实际涉及表，下一轮调用使用相应说明；切换任务时重新组装上下文。Agent 路径不要求列名匹配预设的 `_code`、`_name` 等模式。`model.mode: disabled` 保留确定性发现，适合离线合成实验，不能据此判断任意业务数据的语义发现效果。

`include`、`exclude` 和 `discovery_guidance` 是文字指引；`entity_kinds`、`relation_kinds`、`source_scope`、`excluded_columns` 和 `evidence_only_columns` 是程序检查的范围。`excluded_columns` 不允许查询或投影；`evidence_only_columns` 可以辅助探查，但不能成为输出的实体键、名称、属性或关系端点。无需为每个任务手写列清单，发现阶段也会提出各列的用途。数值列的定义可以代表指标或度量，排除观测值不等于丢弃这些列的元数据。目标配置变更会使发现依赖失效，重新发现规则。

目标不能放宽数据库权限、SQL 策略或数据保留要求。工具返回值、模型对话和查询参数只在当前运行的内存中使用；结果库保存通过验证的规则和图事实。完整流程及限制见 [发现 Agent 更新](design/lightweight_graph/AGENT_DISCOVERY_UPDATE.md)。

## AgentScope 与模型配置

发现 Agent 使用 AgentScope 1.0.21 的 `ReActAgent`，工具注册到 AgentScope Toolkit，查询权限、SQL 守卫和预算仍由项目的工具执行器检查。模型网关需要兼容 OpenAI Chat Completions 协议，支持 `tools` / `tool_calls`、`tool_choice` 和 `response_format: json_object`。只有普通文本或 JSON 输出能力的接口无法完成工具探查。

独立配置示例是 [agent.example.yaml](examples/agent.example.yaml)，运行入口见 [agentscope_discovery.yaml](examples/agentscope_discovery.yaml)。先复制配置，再填写模型地址、名称和密钥来源：

```bash
cp examples/agent.example.yaml examples/agent.local.yaml
```

```yaml
# examples/agent.local.yaml
framework: agentscope
name: lightgraph-discovery
llm:
  mode: metadata_only
  base_url: https://your-model-gateway.example/v1
  model: your-deployed-model
  api_key: ""
  api_key_env: LIGHTGRAPH_API_KEY
  stream: true
  stream_options_include_usage: true
  allow_insecure_http: false
  timeout_seconds: 60
  max_calls: 40
  max_tokens: 120000
  max_discovery_steps: 8
  max_tool_calls: 16
```

`api_key` 非空时使用直填密钥，为空时读取 `api_key_env` 指定的环境变量。推荐使用环境变量。`agent.local.yaml` 和 `agents.local.yaml` 已加入 Git 忽略规则；使用其他文件名时需自行避免提交密钥。密钥在内存中使用 `SecretStr`，配置导出和对象显示均不包含该字段，配置读取失败只报告错误代码。

运行 YAML 使用 `agent_config: agent.local.yaml` 引用它，路径相对运行 YAML 所在目录解析。独立配置与运行文件中的非空 `model:` 不能同时出现，避免地址或密钥被另一处静默覆盖。旧的内嵌 `model:` 格式继续可用，字段与 `llm:` 相同；没有配置时仍为 `mode: disabled`。

准备合成输入并配置好模型后，可执行：

```bash
.venv/bin/lightgraph sample examples/sample --rows 5000
.venv/bin/lightgraph discover examples/agentscope_discovery.yaml
```

`stream: true` 使用模型 SSE 流，`false` 接收完整 JSON；旧配置默认 `false`。两种模式使用相同的工具和输出校验，工具名称与参数完整到达后才执行工具，不执行半截参数。`stream_options_include_usage` 控制是否请求流末尾的 token 用量；网关不支持该选项时设为 `false`，缺少用量时按保守预算记账。这里的流式指模型传输，前端任务事件接口尚未实现，原型不会实时显示这次模型调用。

远端默认要求 HTTPS。客户的可信内网网关只有 HTTP 时，可以显式设置 `allow_insecure_http: true`；该设置不改变数据查询权限或允许发送的内容。

`metadata_only` 只向模型提供授权元数据与相关知识文档；模型工具拒绝实例查询，包括统计、只读 SQL 和跨源核验。后续程序端的规则验证仍受来源权限和查询预算约束。`in_environment` 或 `approved_external` 表示相应数据使用条件已确认，允许发送经过列过滤、长度限制的内存样本。外部服务自身的日志与保留策略必须在客户环境验证；本地测试无法证明这一点。

`max_discovery_steps` 限制单次探索的模型轮数（1～32），`max_tool_calls` 限制工具调用数（1～64）；全程仍受 `max_calls`、`max_tokens`、查询预算和运行截止时间约束。到达上限时保留覆盖缺口，不将未验证提案当作完成。`max_prompt_bytes`、`max_response_bytes` 和 `max_output_tokens` 分别限制请求、响应和生成长度。完整字段见配置示例及 [AgentConfig Schema](src/lightgraph/schemas/AgentConfig.schema.json)。

具体调用路径和实现边界见 [AgentScope 接入设计](design/lightweight_graph/AGENTSCOPE_RUNTIME.md)。

## 结果、范围和增量

正常 `run` 生成 `report.json`、`graph.sqlite`、`graph.json`、`graph.yaml`、节点/边 CSV 和独立 HTML 浏览页；`discover` 输出验证后的规则与报告，不保存业务图结果库。两种流程都输出 `metadata.yaml`，并在 `metadata/declarations/` 与 `metadata/interpretations/` 保留不可变修订。报告区分完成、有缺口、部分完成与失败；E/D/I 的验证状态独立列出。

`metadata.yaml` 中的 `declared` 保留表列、原注释、主键/唯一键/组合外键，以及连接器能力和约束读取范围；`type_origin` 区分数据库声明、Excel 解析推断和 CSV 文本类型。`semantic_annotations` 单独记录目标相关的列角色、冲突代码、覆盖范围、规则和文档依赖，状态为 `candidate` 或 `rule_validated`。目标变化不会改写原声明。目录不保存样本、原始值域、SQL 参数或模型对话，也不会把派生解释当成独立业务依据。

```bash
.venv/bin/lightgraph report runs/example
.venv/bin/lightgraph inspect runs/example/graph.sqlite --scope A --scope public --domain customer_demo
.venv/bin/lightgraph search runs/example/graph.sqlite 销售费用 --scope A --scope public --domain customer_demo
.venv/bin/lightgraph export runs/example/graph.sqlite runs/export-a --scope A --scope public --domain customer_demo --include-candidates
.venv/bin/lightgraph import-graph runs/example/graph.json runs/imported/graph.sqlite
.venv/bin/lightgraph retain-check runs/example --marker RETENTION_CANARY_9f2d17
.venv/bin/lightgraph validate examples/sample/rules.yaml
.venv/bin/lightgraph schema src/lightgraph/schemas
```

`--scope` 控制本地导出的支持范围，调用方仍须先取得对应授权；CLI 本身不是多用户登录系统。导入只接受空结果库，恢复已导出的图事实及范围，不恢复源库连接、执行规则或源数据快照。

同义实体的共享依赖已确认的定义与身份规则，不按重名合并。别名、属性、节点和边分别有来源支持；撤回 A 的引用不会删除 B 仍支持的实体。改变定义或共享策略会生成绑定修订。

新发布通过完整性、依赖和端点检查后才切换可见结果。有关联的实体与引用规则一起提交。目标和依赖未变时，源读取中断、发现冲突、分区不完整或发布失败不会被当成删除证据。改变目标、发现模式或工作流版本会先使旧解释失效；历史仍保留，新发现失败时也不会把旧解释冒充新目标的结果。文档主张与其对实体的候选对齐分开维护，避免文档引用阻挡实体删除。

增量依赖记录每张表实际适用的文档集合，新增文档、绑定变化和章节修订都会触发相关表复核；无关表可继续复用。文档抽取缓存同时包含目标、子任务和上下文选择版本。失败、延期和未访问表不写成已完成，下次运行继续尝试；已完成表不必随其他表的失败重复调用模型。发布后的标记写入失败会单独报告，不能把已经发布的事实记成已回滚。

显式移除输入用 `removed_rule_ids` / `removed_document_ids`；遗漏路径不自动表示删除。规则回滚命令会使用旧定义重新核验当前来源，不能恢复没有保存过的历史单元格：

```bash
.venv/bin/lightgraph rollback examples/run.yaml items 1
```

## 相对路径与层级入口

宽表中的 L1/L2 表示相对路径位置时，可在经过验证的身份域内使用 `level_policy: relative_path`。规则需声明 `verified_identity_domain`、身份与属性依据、层级体系、层序和位置含义。同一实体可以出现在不同路径深度；深度和 taxonomy 不进入该实体的 ID，实际来源代码列组仍有独立绑定。成员位置以带来源支持的 `source_position` 保存。

`ordinal_slots` 将 L1/L3 之间视为缺 L2；`ordered_slots` 表示编号只排列现有槽位。NULL 始终切段。连续重复只有在填充含义获确认后才能压缩；跨规则的环和多父在同一体系、业务条件及版本中核验，`allow_dag` 也不允许环。旧的 `level_qualified` 和 `code_with_fill_merge` 保留原有行为。字段和固定规则示例见 [层级规则说明](src/lightgraph/rules/README.md)。

已发布图可按入口查看，入口的 `display_depth` 为 0。下面的 ID 需替换为当前授权图中的实体 ID：

```bash
.venv/bin/lightgraph hierarchy-view runs/example/graph.sqlite \
  --root ENTITY_ID --taxonomy finance --scope A --domain customer_demo --max-depth 3
```

可用重复的 `--context key=value` 指定业务条件或层级版本，`--publication ID` 查看历史发布。入口变化只查询已发布图，不查询来源、不调用模型、不改变父子事实。旧身份改为相对路径时，需要同一逻辑规则的新修订完整重投影；发布事务核对迁移候选、属性冲突和依赖闭包，保存旧绑定与迁移审计。不能按显示名称批量合并。

## 目录

```text
src/lightgraph/
  contracts.py          输入、规则、事实、支持、报告契约
  sources/  query/       接入、短期内存批次、SQL、预算
  discovery/            结构发现、模型候选、反例修订、缓存漂移检查
  documents/            Markdown、章节、说明上下文、分析主张
  rules/                规则编译、完整核验、批量执行
  identity/  store/      实体身份、支持账本、原子发布与历史
  metadata*.py          表列图、语义角色对齐候选
  catalog.py            版本化声明与解释 YAML
  context.py            每次调用的目标、任务、范围与文档引用
  cross_source.py       两端 DISTINCT 的有界只读比较
  runtime/              配置、运行锁、允许的输出写入
  presentation/         JSON/YAML/CSV、导入、HTML
  tests/                单元、契约、端到端与保留路径测试
examples/                示例配置与用户可生成的合成上传包
design/lightweight_graph/  SDD、US、工作流、架构依据
runs/                    运行结果；不提交到版本库
```
