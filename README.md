# data-graph · 轻量知识图谱

从 Gauss 数据库或上传的 Excel 中发现实体列组、层级和引用规则，再用规则生成图谱。Markdown 可以提供数据说明，也可以贡献带出处和条件的分析知识。图中分开显示业务实体、表与列、文档来源。

**运行源码、测试和页面模板均在 `src/lightgraph/`。** 本项目不需要图数据库。SQLite 只保存抽取结果、规则、来源支持和发布记录；数据库查询返回的行、抽样、模型请求和响应不会进入结果库。

依赖的本轮实测版本记录在 `requirements.lock`；需要复现环境时先安装该文件，再安装项目。

当前运行能力与验证边界见 [实施与验收记录](IMPLEMENTATION_STATUS.md)。架构依据是 [设计 v1.0](design/lightweight_graph/README.md)。

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

## Excel 与 Markdown

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

两类 Markdown 用 front matter 区分：

```yaml
---
purpose: data_description
applies_to: [finance.report_item, finance.report_tree]
---
```

`data_description` 进入适用表的发现上下文；`analysis_knowledge` 独立抽取分析主张。混合文档按章节处理。文档中的建议、可能性和适用条件会保留，自动抽取的分析主张默认是候选。自动生成的数据说明标记为 `derived`，不会充当独立证据。

## Gauss 接入

参考 [examples/gauss.yaml](examples/gauss.yaml)。连接串和模型密钥只通过配置指定的环境变量读取，不写入配置或报告。

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

启用模型后，目标进入发现 Agent 的系统上下文。Agent 提出假设、调用工具、读取临时结果，再决定继续查询还是提交结构化规则候选；程序随后验证规则并批量构图。Agent 路径不要求列名匹配预设的 `_code`、`_name` 等模式。`model.mode: disabled` 保留确定性发现，适合离线合成实验，不能据此判断任意业务数据的语义发现效果。

`include`、`exclude` 和 `discovery_guidance` 是文字指引；`entity_kinds`、`relation_kinds`、`source_scope`、`excluded_columns` 和 `evidence_only_columns` 是程序检查的范围。`excluded_columns` 不允许查询或投影；`evidence_only_columns` 可以辅助探查，但不能成为输出的实体键、名称、属性或关系端点。无需为每个任务手写列清单，发现阶段也会提出各列的用途。数值列的定义可以代表指标或度量，排除观测值不等于丢弃这些列的元数据。目标配置变更会使发现依赖失效，重新发现规则。

目标不能放宽数据库权限、SQL 策略或数据保留要求。工具返回值、模型对话和查询参数只在当前运行的内存中使用；结果库保存通过验证的规则和图事实。完整流程及限制见 [发现 Agent 更新](design/lightweight_graph/AGENT_DISCOVERY_UPDATE.md)。

## 模型配置

默认 `model.mode: disabled`。发现 Agent 使用原生函数工具调用，模型接口需要支持 `tools` / `tool_calls`；只有普通 JSON 文本输出能力的接口无法完成交互探查。可使用具备该能力的 OpenAI-compatible 接口：

```yaml
model:
  mode: metadata_only
  base_url: https://your-model-gateway.example/v1
  model: your-model-name
  api_key_env: LIGHTGRAPH_API_KEY
  max_calls: 40
  max_tokens: 120000
  max_discovery_steps: 8
  max_tool_calls: 16
```

`metadata_only` 不发送实例样本，但仍会发送授权元数据与相关知识文档。`in_environment` 或 `approved_external` 表示相应数据使用条件已确认，允许发送经过列过滤、长度限制的内存样本。外部服务自身的日志与保留策略必须在客户环境验证；本地测试无法证明这一点。

`max_discovery_steps` 限制单次探索的模型轮数（1～32），`max_tool_calls` 限制工具调用数（1～64）；全程仍受 `max_calls`、`max_tokens`、查询预算和运行截止时间约束。到达上限时保留覆盖缺口，不将未验证提案当作完成。

## 结果、范围和增量

每次运行生成 `report.json`、`graph.sqlite`、`graph.json`、`graph.yaml`、节点/边 CSV 和独立 HTML 浏览页。报告区分完成、有缺口、部分完成与失败；E/D/I 的验证状态独立列出。

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

显式移除输入用 `removed_rule_ids` / `removed_document_ids`；遗漏路径不自动表示删除。规则回滚命令会使用旧定义重新核验当前来源，不能恢复没有保存过的历史单元格：

```bash
.venv/bin/lightgraph rollback examples/run.yaml items 1
```

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
  runtime/              配置、运行锁、允许的输出写入
  presentation/         JSON/YAML/CSV、导入、HTML
  tests/                单元、契约、端到端与保留路径测试
examples/                示例配置与用户可生成的合成上传包
design/lightweight_graph/  SDD、US、工作流、架构依据
runs/                    运行结果；不提交到版本库
```
