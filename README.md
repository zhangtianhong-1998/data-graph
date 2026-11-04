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

Windows 使用 `.venv\Scripts\python.exe` 和 `.venv\Scripts\lightgraph.exe`。当前已实现 Windows 进程回收及路径适配，本轮尚未在 Windows 机器上实际验证。

打开 [http://127.0.0.1:8001](http://127.0.0.1:8001)，配置来源、知识文档、构建目标和模型后保存并启动。配置保存后，下一次运行立即使用新值；运行中的任务保持启动时快照。

仓库的 `vendor/deepseek-harness/` 包含固定版本内核与 Python SDK，实际从该目录启动，不依赖个人机器上的 Desktop 应用。来源 commit、许可证、文件哈希和排除清单见[内核来源说明](vendor/deepseek-harness/VENDOR_README.md)。`bootstrap_harness.py --check` 检查已安装依赖及官方 CLI 启动，不调用模型。

## 输入与配置

支持 Excel、CSV、Gauss/MySQL 适配器与 Markdown 文档。数据库实例读取仍受授权范围、只读查询和部署配置约束；连接器存在不代表客户数据库已验收。保留既有来源连接、表列权限、`scopes` 和 `document_scopes`。行支持的可见范围由程序配置 `scope_bindings` 绑定到物理列；无绑定时贡献标记为 `public`，不会根据对象键或列名猜测权限。前端先按范围过滤支持，再重建名称、属性和关系；业务 namespace 本身不授予访问权。`metadata_only` 只允许元数据和知识材料，不发布实例图谱。关闭模型时仅读取目录，不自动猜测对象或关系。

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
  max_calls: 80
  max_prompt_tokens: 1048576
  max_output_tokens: 65536
  timeout_seconds: 600
max_run_seconds: 3600
record_trace: true
output_dir: ./runs/graph
```

模型调用次数是主要额度，重试和压缩调用也计入总数；`max_calls: null` 不另设调用次数限制，`0` 不允许调用。运行时间、接口超时和单次输出仍分别生效。旧累计 token、每表轮数、阶段比例及字节额度只保留读取兼容，不控制新内核。

普通界面只提供模型连接与有效运行额度，后台适配模型地址和接口参数，不展示内核选择或压缩实现。服务地址可填写 Base URL 或完整 Chat Completions 地址；非本机 HTTP 服务仍须显式允许，后台不会自动降低连接约束。

输入容量与工作上下文阈值分开设置。输入及总窗口都留空时，不额外设置输入准入限制；内核仍以 1,048,576 tokens 作为压缩和路由的默认输入容量元信息。设置输入额度后，请求派发前按估算值检查，超过额度不发送给服务商。Harness 当前以字符数估算并用服务商 usage 校准，不是精确分词器；估算值、默认容量和服务商实际窗口分别看。`working_context_tokens: null` 关闭自动压缩，包括窗口溢出后的自动压缩恢复。

原生会话记录是运行、压缩与恢复的基础，必须开启。模型输入、返回及工具结果可包含获准样本；记录保留在本地，由用户主动清理。凭据不写入知识产物。前端显示实际调用轨迹，不把阶段计数或未返回的内部思考当作 Agent 推理。

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
