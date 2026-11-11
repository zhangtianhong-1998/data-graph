# data-graph · 按用户目标自主构图

根据用户输入的目的，从授权事实数据、元数据与可选知识文档中发现对象、属性、身份和关系，生成带来源、验证记录和版本的图谱。经营分析是应用场景之一，平台不预设指标、行业、表结构或字段名称。用户可以补充业务解释，无需先编写抽取规则。

当前实现图谱构建，采用 **DeepSeek Harness 实际内核 + build-graph skill + 通用 YAML 映射**。本体目标保留在产品设计中，本轮未实现本体 skill、Data Agent 或问数应用。

## 安装与启动

当前按源码 checkout 交付。Python 需要 3.11+；内核需要 Node.js `^22.19.0 || >=24.0.0` 与 `pnpm@11.7.0`。macOS/Linux 需要 C 编译器及 Node 开发头文件，供会话持久化模块使用。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python scripts/bootstrap_harness.py
npm --prefix src/workbench ci
npm --prefix src/workbench run build
.venv/bin/lightgraph serve --port 8001
```

Windows 使用 `.venv\Scripts\python.exe` 和 `.venv\Scripts\lightgraph.exe`。内核启动器按当前 Python 的 scripts 安装目录解析，兼容虚拟环境与全局 Python 的 `Scripts` 布局。跨平台安装、原生内核及模拟模型测试由 GitHub Actions 执行，客户部署仍需单独验证。

打开 [http://127.0.0.1:8001](http://127.0.0.1:8001)，配置来源、知识文档、构建目标和模型后保存并启动。配置保存后，下一次运行立即使用新值；运行中的任务保持启动时快照。

新建项目默认最大输入 1048576 tokens、最大输出 65536 tokens、模型调用 2000 次、运行时限 60000 秒、接口超时 800 秒。已有明确配置保留，可在页面点击“使用默认额度”后保存。常用设置直接填写，完整 JSON 编辑器默认折叠。

实例验证可能执行多次查询，MCP 使用原生 SSE 及时发送响应头和心跳，避免长任务因五分钟没有 HTTP 响应而中断。模型接口超时、单次 SQL 超时与整轮运行时限分别生效；心跳不会延长预算。更新代码后需在新运行中使用，已启动的进程保持原代码与配置。详见[工具生命周期与超时说明](design/lightweight_graph/MATERIALIZATION_RECOVERY_20261109.md)。

启动时可选“继续上次工作”或“从零开始（保留历史）”。继续运行复用当前草稿、证据和验证；从零运行使用独立成果目录与会话索引，仍沿用保存的输入和授权，旧成果不会删除。之后的当前画布与继续运行跟随新目录，运行详情记录各自模式及成果路径。

页面填写的模型 API key 加密保存在本机，刷新页面或重启服务后无需重填；密钥绑定项目、服务地址与模型名，可主动清除，服务器环境变量方式仍可使用。

模型密钥写入和清除会核对页面所依据的配置修订；写入还核对预期服务地址和模型。其他页面已修改配置时拒绝迟到操作，重新加载配置后需要确认密钥对应的服务。自行调用接口的客户端也必须提供这些前提。

仓库的 `vendor/deepseek-harness/` 包含固定版本内核与 Python SDK，实际从该目录启动，不依赖个人机器上的 Desktop 应用。来源 commit、许可证、文件哈希和排除清单见[内核来源说明](vendor/deepseek-harness/VENDOR_README.md)。`bootstrap_harness.py --check` 检查已安装依赖及官方 CLI 启动，不调用模型。

## 输入与配置

支持 Excel、CSV、Gauss/MySQL 适配器与 Markdown 文档。数据库实例读取仍受授权范围、只读查询和部署配置约束；连接器存在不代表客户数据库已验收。保留既有来源连接、表列权限、`scopes` 和 `document_scopes`。行支持的可见范围由程序配置 `scope_bindings` 绑定到物理列；无绑定时贡献标记为 `public`，不会根据对象键或列名猜测权限。前端先按范围过滤支持，再重建名称、属性和关系；业务 namespace 本身不授予访问权。`metadata_only` 只允许元数据和知识材料，不发布实例图谱。关闭模型时仅读取目录，不自动猜测对象或关系。

数据库配置页直接提供“允许读取事实数据”：新连接默认不勾选，仅允许读取元数据；勾选后，Agent 可只读查询所选表和列。运行时自动检查当前连接的只读事务、超时和取消能力，无需手工生成或上传能力 YAML。检查未通过时禁止数据查询。保存连接仍只读取元数据，选项从下一次运行生效；全局模型选择“仅元数据”时仍禁止事实读取。查询结果和运行轨迹沿用项目留存设置，关闭该选项不删除既有产物。

查询次数支持在模型配置页选择“不限次数”，对应 `max_db_queries: null`；正整数设置总次数上限。次数包含所有来源的元数据、事务控制及数据查询，报告分别记录预算准入次数与数据操作次数。次数不限不取消只读、授权、单次超时、运行时限、返回量和内存限制。额度耗尽后停止源查询，允许有限收尾，已经验证的成果保留。

模型配置页还可直接设置累计返回行数、累计返回数据量（MiB）和构图工作内存（MiB）。默认累计 40 万行、64 MiB，工作内存 32 MiB；前两项可分别选择不限。填写 1024 MiB 表示 1 GiB。累计量包含整轮所有来源的探查和验证，分批执行或释放内存不会清零；报告显示已用量和生效限额。设置保存后在下一次运行生效，详见[返回量与验证边界](design/lightweight_graph/SOURCE_RETURN_BUDGET_20261110.md)。

构建结果可选“生成实例图谱”或“仅生成结构与映射”（`construction_target: structure`）。结构检查不查询事实数据，也不证明实例关系或业务语义成立。结构方案可用 `lightgraph export-structure CONFIG OUTPUT.yaml` 单独导出。实例模式按映射的必要列和明确筛选范围生成节点及关系；跨表、跨来源和同表其他行的端点支持等值查找，必须实际找到目标记录。

每次验证保存独立编号与读取时间窗口，发布固定的已验证产物，不再重查整个活库。失败重验不会覆盖成功快照；后续数据检查发现矛盾时，选择历史成功版本必须显式指定。主名称、别名和补充展示分别映射，名称遗漏会返回可操作提示。完整规则和边界见[架构修订](design/lightweight_graph/RELATION_MAPPING_AND_PUBLICATION_20261104.md)。

业务图页的“查看结构方案”可打开当前或历史修订的类型图与字段绑定，不要求先生成实例。数据库执行失败与映射数据冲突分别诊断；独立来源可继续生成候选，含失败依赖的草稿仍需修复后验证。修改方案时会提示删除的来源、名称角色和路径上下文，见[混合来源故障与能力边界](design/lightweight_graph/MIXED_SOURCE_FAILURES_20261105.md)。

事实来源断连时，Agent 可从已保存的混合草稿另存度量、维度等列定义草稿，单独验证并发布。已有列定义使用本次授权目录，无需读取经营数值；事实成员及其关联仍留在主草稿中等待验证。目录变化或权限失效不会被当作可离线复用的网络故障，见[快照恢复与独立列定义发布](design/lightweight_graph/RECOVERY_AND_DEFINITIONS_20261110.md)。

新会话的参数绑定或 DISTINCT 固定探查读回不符时，可丢弃会话并在三次恢复额度内重新检查；只读资格、权限和目录错误仍停止访问。进程诊断显示具体检查项目、失败类型和恢复次数。同次验证中读取列与筛选条件相同的度量关系复用完整投影，逐条保留关系依据；恢复后仍需验证并发布原主草稿才能补上关联边。详见[预检恢复与关联取数复用](design/lightweight_graph/PREFLIGHT_AND_RELATION_REUSE_20261110.md)。

字段本身也可按目标建为定义对象，例如从元数据或文档中的字段说明建立度量定义。`metadata_column` 使用来源、Schema、表、列作为身份，不查询金额值；Agent 必须引用已读取且适用的材料。它可与实际行中的指标关联，分别记录“声明适用”或“观察到非空值”的依据，不自动连接全图指标。空维度编码可用显式 `is_not_null` 筛选；列定义、谓词和跨源内存边界见[本轮设计](design/lightweight_graph/METADATA_OBJECTS_AND_RELATIONS_20261105.md)。

圆形画布分批加载全部已授权节点和关系，显示加载进度并支持暂停、继续；分批传输不再把跨页边丢掉。数据库确认断链后可在新只读快照前有界重建并重新检查目录和能力，不重放已读取的部分结果。运行轨迹下的“查看进程诊断”提供安全的驱动状态与退出记录，可独立导出。探查顺序、恢复及性能边界见[后续设计修订](design/lightweight_graph/DISCOVERY_RECOVERY_AND_FULL_GRAPH_20261105.md)。

数据库连接按一次完整只读快照使用，结束后释放，下次查询新建并校验；同一校验内多条查询仍共享快照。恢复失败会锁定该来源，其他来源的独立工作可以继续。反复调用已锁定来源会触发有界收尾，避免几十次无效校验；已有草稿和候选保留。具体边界见[连接生命周期设计](design/lightweight_graph/SNAPSHOT_CONNECTION_LIFECYCLE_20261105.md)。

构图启动时，数据库的连接或可隔离预检故障也会保留为失败来源，健康来源可继续工作；依赖失败来源的草稿不能发布，整轮报告保留缺口。凭据、授权配置及全局预算错误仍按原约束退出。跨源引用检查与实例匹配保留驱动值类型，不将 Decimal、日期等值与同形文本自动合并；新类型语义下的发布需要重新验证旧草稿，历史产物保留。修复及兼容边界见[2026-11-06 修订](design/lightweight_graph/P1_CORRECTNESS_REPAIRS_20261106.md)。

数据接入方式保持不变。抽取目标与知识材料统一使用 Markdown：目标在页面直接编辑，知识文档继续通过现有 Markdown 上传、用途与范围设置管理。目标随项目配置修订保存，运行时写入 `construction/goal.md`；文档按需读取，初始上下文只有目标与材料目录。文字说明不能改变表列授权或程序约束。

运行配置仍使用 YAML，`goal_markdown` 承载目标原文。省略它时，旧 `goal.purpose` 和相关说明会转换为 Markdown。以下是结构示例，来源路径、模型与构建目的需要按实际任务填写：

```yaml
sources:
  - id: source1
    kind: excel_workbook
    path: ./input.xlsx

goal_markdown: |
  # 本次目标
  根据需要填写要发现的对象、属性和关系范围。

  ## 补充说明
  可填写业务定义、关注点和验收要求。

documents: []
model:
  mode: in_environment
  base_url: http://127.0.0.1:9000/v1
  model: your-model
  api_key_env: LIGHTGRAPH_API_KEY
  max_calls: 2000
  max_prompt_tokens: 1048576
  max_output_tokens: 65536
  timeout_seconds: 800
max_run_seconds: 60000
record_trace: true
output_dir: ./runs/graph
```

模型调用次数是主要额度，重试和压缩调用也计入总数；`max_calls: null` 不另设调用次数限制，`0` 不允许调用。运行时间、接口超时和单次输出仍分别生效。旧累计 token、每表轮数、阶段比例及字节额度只保留读取兼容，不控制新内核。

普通界面只提供模型连接与有效运行额度，后台适配模型地址和接口参数，不展示内核选择或压缩实现。服务地址可填写 Base URL 或完整 Chat Completions 地址；非本机 HTTP 服务仍须显式允许，后台不会自动降低连接约束。

输入容量与工作上下文阈值分开设置。输入及总窗口都留空时，不额外设置输入准入限制；内核仍以 1,048,576 tokens 作为压缩和路由的默认输入容量元信息。设置输入额度后，请求派发前按估算值检查，超过额度不发送给服务商。Harness 当前以字符数估算并用服务商 usage 校准，不是精确分词器；估算值、默认容量和服务商实际窗口分别看。`working_context_tokens: null` 关闭自动压缩，包括窗口溢出后的自动压缩恢复。

原生会话记录是运行、压缩与恢复的基础，必须开启。模型输入、返回及工具结果可包含获准样本；记录保留在本地，由用户主动清理。凭据不写入知识产物。前端显示实际调用轨迹，不把阶段计数或未返回的内部思考当作 Agent 推理。

大工具回复会保存为本地结果材料，模型先接收有上限的摘要，再按字段和页面读取需要的内容；材料读取不重查数据库。每轮模型调用前，还会整理过大的历史结果与已执行草稿参数，保留原始事件和恢复入口。多轮累积继续交给 Harness 原生摘要压缩。这些处理自动生效，无需增加配置；`working_context_tokens: null` 关闭的是原生摘要压缩，单份结果与历史大内容的限制仍保留。详见[工具结果与上下文管理](design/lightweight_graph/CONTEXT_MATERIALS_20261110.md)。

## 运行与产物

`run`、`discover`、`build-graph` 和工作台使用同一个 Harness 入口，没有旧发现引擎回退：

```bash
.venv/bin/lightgraph run config.yaml
.venv/bin/lightgraph discover config.yaml
.venv/bin/lightgraph export-graph config.yaml graph.yaml
.venv/bin/lightgraph validate draft.yaml
```

`run` 构建并允许发布，`build-graph` 是同一过程的别名；`discover` 只探索和验证，不发布。`validate` 仅检查新 YAML 草稿格式，不代替数据验证或语义验收。旧固定规则输入会明确拒绝；历史结果库的只读 `inspect`、`search`、`export` 保留，不会重新启动旧发现流程。

Agent 自主调用 `describe_sources`、`inspect_source`、`read_material`、`save_graph_draft`、`validate_graph_draft`、`publish_graph_draft` 和 `read_workspace`。系统不按表名或列名预建业务规则，不强制对象与关系分时完成。执行器根据 Agent 保存的通用映射，验证来源、键、标签、端点、关系约束和完整性。

主要知识产物位于运行目录，统一为 YAML：

- `physical_catalog.yaml`：本次授权物理目录。
- `construction/drafts/`、`evidence/`：草稿修订、验证结果和证据记录。
- `construction/published/` 与 `manifest.yaml`：已发布版本及当前版本索引。
- `construction/config.yaml`、`construction/runtime-result.yaml` 与 `report.yaml`：有效目标、内核运行结果和构建报告。

Harness 原生会话与调用轨迹使用原生 JSONL，单独保留，不冒充图谱事实。草稿校验失败可继续修改；已保存内容不会因单次模型失败自动删除。发布要求当前修订通过数据检查且来源仍有效，语义判断标记为 `agent_authored`。发布成功不等于客户语义已全部证明。

大草稿和完整验证诊断可按修订分页重读，模型无需靠会话搜索重建已保存的 YAML。实例验证增量累积图谱及来源支持，并记录工作集估计；单次 SQL、模型请求与整轮工具等待分别计时。图谱工具通信故障或连续摘要截断会触发有界停止，保留成果供下次继续。设计和验收边界见[实例验证与运行恢复](design/lightweight_graph/MATERIALIZATION_RECOVERY_20261109.md)。

草稿的 `unresolved` 只记录本次目标尚未完成的事项，可以为空；`decisions` 记录已采用的判断，`suggestions` 记录后续建议。建议不会把成功运行改为有缺口。旧草稿中的未决原文仍保留，需要明确修订后才改变状态。压缩摘要不能改写原始目标；目标和 skill 保留在不参与历史压缩的系统上下文中。

没有名称列的节点可使用 `display_columns`，或从已关联对象生成显示名称；显示名称不参与身份计算。页面分别展示业务身份、访问范围和来源支持，搜索支持编码和别名。运行报告按草稿包、实体类型、关系类型和映射统计；工具结果的历史压缩单独标记。用量包含摘要调用与缓存输入，缺失的服务商用量标为未知。

## 已验证与未验证

实际 vendored SDK/内核已经通过本地模拟网关测试：原生工具循环、YAML 保存/验证/发布、调用记账、压缩和原生日志均有工程验证。前端保存、配置刷新与构建已有回归。具体数量与剩余缺口见[实施状态](IMPLEMENTATION_STATUS.md)。

2026-11-03 的五表合成数据真实模型实验用 27 次调用生成 462 个节点、930 条边；按业务身份、属性和有向边独立核对，核心 Gold 全部匹配。审计发现建议被当成未完成目标、合法 SQL 被误拒、组合名称及报告显示不清等问题，已修复并通过离线回放与工程回归，详见[修复与验证记录](design/lightweight_graph/HARNESS_RUN_REPAIRS_20261103.md)。修复版本尚未重新进行真实模型全量实验，单次合成结果不证明改名表列、异构业务模式或客户数据下的发现质量。

绑定旧阶段调度与提案格式的测试已按[退役清单](design/lightweight_graph/retired_workflow_tests.yaml)移除，其背后的权限、留存、恢复和原子发布保障由新入口测试覆盖。旧冻结实验包装不能完整封装新内核，相关启动入口已移除，新的可复现冻结包装待重做。Windows/Linux 实际运行、客户 Gauss/MySQL、客户 Gold 与生产性能分别验收。

## 设计与历史

- [当前目标与架构](design/lightweight_graph/GENERIC_GRAPH_CONSTRUCTION.md)
- [用户故事](design/lightweight_graph/US.md)、[实施计划](design/lightweight_graph/IMPLEMENTATION_PLAN.md)
- [通用构图验收清单](design/lightweight_graph/construction_acceptance.yaml)
- [图谱构建 skill](src/lightgraph/construction/skills/build-graph/SKILL.md)
- [迁移前 README 与实验](design/lightweight_graph/history/root-readme-before-harness.md)
- [迁移前实施记录](design/lightweight_graph/history/implementation-before-harness.md)

历史实验、输入文件和已生成结果均保留。历史文档中的 AgentScope、RuleSpec、固定阶段和旧测试数字只描述当时版本。

本轮连接与发布行为见[短连接和冲突隔离设计](design/lightweight_graph/CONNECTIONS_AND_CONFLICTS_20261109.md)：每组完整投影后释放连接；连接恢复最多三次；仅有名称冲突时可发布无冲突部分，保留冲突证据和目标缺口。
