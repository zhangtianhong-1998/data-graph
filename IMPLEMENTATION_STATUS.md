# 实施与验收状态

更新日期：2026-11-03。当前目标是按用户目的，从授权数据与材料自主发现图谱；经营分析只是一类场景。采用实际 DeepSeek Harness 内核，Agent 自主决定探查、保存、验证、修订和发布，知识产物使用 YAML。本体 skill、Data Agent 和问数应用本轮未实施。

## 当前实现

| 部分 | 已实现范围 |
| --- | --- |
| 内核与入口 | 仓库固定 Harness 源码，导入其 Python SDK 并启动官方 CLI；`run`、`discover`、`build-graph`、工作台统一入口，不回退到 AgentScope 编排 |
| 图谱发现协议 | build-graph skill 与新的实体/关系 YAML 映射；对象、字段和关系由 Agent 依据目标与证据提出，不填写旧 RuleSpec |
| 数据工具 | 授权元数据、材料读取、统计、FD、引用与只读 SQL；Excel metadata_only 不导入事实或做类型推断，类型记为 unknown |
| 支持与权限 | 保留原来源连接及表列授权；程序配置 scope_bindings，逐条过滤实体存在、名称、别名、属性和关系支持；范围切换不要求重新抽取 |
| 验证与发布 | 保存版本化草稿，执行数据检查，验证当前修订及来源后发布；语义判断来源标为 agent_authored |
| 目标与材料 | 目标使用 Markdown 编辑并随项目修订保存，运行快照为 goal.md；旧目标兼容转换，初始上下文仅目标与材料目录，知识文档按需读取 |
| 前端配置 | 普通界面隐藏内核及压缩实现，保留模型连接、总调用、运行时限、输入/输出容量和超时；后台适配接口参数 |
| 留存与轨迹 | 原生会话记录必需，成果与轨迹由用户主动清理；前端按实际模型请求、响应和工具事件展示，保留原生事件 |
| 历史资料 | 旧实验与结果不删除；旧入口说明移至历史文档，不作为当前运行方式 |

语义正确性仍需独立验收。保存草稿、数据检查通过、正式发布、Harness 正常结束是不同结论，不能互相替代。

## 已执行的工程验证

- 内核源固定在 commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`，官方 CLI 版本 `0.2.1-alpha.1`。9,170 个来源文件有哈希清单，355 份具名包清单的 5,225 项 workspace 依赖路径完整。
- 已安装锁定 pnpm 依赖、构建本机会话持久化模块，`bootstrap_harness.py --check` 实际启动官方 CLI。完整 Desktop 分发包没有构建。
- 新构建入口、工具、YAML 执行与发布、权限、前端配置及 worker 的集中回归 **249 项通过**，包含实际 Harness 配本地模拟 HTTP 网关的集成测试。完整工具链得到 3 个节点、2 条边；压缩调用也计入预算，原始材料保留在会话记录中。这里的“实际内核”不表示使用了真实 LLM。
- 最后修正 Linux 原生模块路径、移除未使用的 stdio 运行兜底后，运行时单元测试与实际内核集成再次执行，**25 项通过**，其中 7 项为实际内核集成。产品 `run_construction` 和真实工作进程均已走过官方 SDK、HTTP MCP 与本地模拟模型接口。
- 前端全量 **240 项通过**，TypeScript 检查和 Vite 生产构建通过。Python wheel 构建通过，并核对包含运行时插件和图谱 skill；当前仍按源码 checkout 运行，wheel 本身不包含 Node 内核及其依赖。
- Excel/CSV 相关源适配器回归 80 项、Skill 格式检查及差异空白检查通过。不同测试集合存在包含关系，不能相加作为全量数量。

本轮按[退役清单](design/lightweight_graph/retired_workflow_tests.yaml)移除了 111 个绑定旧调度与提案协议的测试函数及 9 个无引用夹具文件；权限、留存、来源读取、恢复与发布保障保留或迁到新入口。配置 JSON Schema 已同步，最终完整 Python 回归 **2,605 项通过，无失败或跳过**（95.39 秒，`LIGHTGRAPH_TEST_HARNESS=1`）。这次明确启用了实际内核与本地模拟网关测试。随后补充的原生请求进度计数回归 **1 项通过**，确认重试也按实际请求更新调用数。剩余功能缺口见[迁移清单](design/lightweight_graph/CONSTRUCTION_MIGRATION_GAPS.md)。

Markdown 目标、按需材料读取与入口回归 **24 项通过**，包含实际工作进程使用脚本模型发布图谱；scope、worker、凭据与 API 组合回归 **96 项通过**，原来源驱动、连接及表列授权回归 **291 项通过**。测试集合有交集，不累加计数。

## 上下文与运行边界

Harness 使用原生上下文管理。输入容量、输出预留和工作上下文阈值分别配置；输入与总窗口均未指定时，不额外设置输入准入限制；默认 1,048,576 tokens 仍作为内核路由和压缩的容量元信息。已设置的输入额度在原生 llm/stream 派发前按估算值检查；实测低额度拒绝时没有发出 HTTP 请求。上游 token-meter 使用字符数估算并结合服务商 usage 校准，不能称为精确分词。`working_context_tokens: null` 关闭自动压缩及其窗口溢出恢复。

当前轨迹规范化保留完整原生事件。`request/header` 只是请求配置和工具定义变化，不能当作一次完整模型输入；实际输入从原生 `llm/stream` 调用处记录，模型未返回的推理不补造。

原生 Windows 进程回收和安装路径已实现，本轮尚未在 Windows 上实际执行。macOS 本机测试不能代替 Windows/Linux 验收。已有 `scopes` 与 `document_scopes` 可继续使用。`scope_bindings` 由用户或程序绑定物理列，Agent 不能声明授权；无绑定的贡献为 `public`。实体存在、名称、别名、属性与关系各自保存支持，视图先过滤支持再重建。仅切换可见范围不使图失效，来源/列授权或绑定改变则拒绝读取旧图。旧自定义范围若无绑定且不含 `public`，运行仍正常，但业务视图会显示 `scope_filtered`。数据库适配器复用已有只读约束，客户 Gauss/MySQL 接入、客户 Gold、稳定性和生产规模仍待验证。

## 尚未验收

- 真实 LLM 在改名表列、宽/长/关联表、多位置和跨表身份场景中的自主发现质量。
- 包含 Python SDK、Node 内核和依赖的新冻结实验包装；旧 prepare-evaluation/run-frozen 入口已移除。
- 客户业务语义、真实数据库接入、跨平台运行与生产性能。
- 本体构建 skill 与 Data Agent 应用。

当前通用验收清单见 [construction_acceptance.yaml](design/lightweight_graph/construction_acceptance.yaml)。历史测试、旧模型实验和旧固定规则结果保留在[迁移前实施记录](design/lightweight_graph/history/implementation-before-harness.md)，不能作为新路径验收证据。
