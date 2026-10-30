# AgentScope 接入与模型配置

更新日期：2026-10-29。发现循环使用 AgentScope；模型请求、SSE 解码与工具分片累计改由固定版本 OpenAI SDK 承担。抽取目标、Markdown 作用域、SQL 守卫、规则核验和发布流程沿用现有契约。

## 运行路径

```text
运行 YAML → agent_config → 模型权限与预算
  → T0/T1/T2/T3/T4/T5 任务包
  → AgentScope ReActAgent.reply
      → DiscoveryFormatter：目标、阶段、适用文档、当前观察
      → DiscoveryChatModel：预算/字节/完整性包装 → OpenAI SDK Chat Completions
      → Toolkit：校验并执行工具，返回 ToolResponse
      → ReActAgent 读取观察，继续推理或提交 ProposalBatch
  → 引用检查 → 独立规则核验 → 受控执行与图谱发布
```

`agentscope_runtime.py` 使用实际的 `ReActAgent.reply` 和 `Toolkit.call_tool_function`，没有保留另一套手写发现循环。定制部分负责文本格式、模型适配、执行限额和样本生命周期。Markdown 主张抽取等单次结构化任务继续使用同一传输客户端，不需要工具时不额外创建 Agent。

框架固定为 `agentscope==1.0.21`，使用该版本的 `ReActAgent`、`ChatModelBase`、`Toolkit`、`Msg` 和 `ToolResponse` 接口。`requirements.lock` 锁定验证过的依赖；`mcp<2` 避免 AgentScope 1.x 导入已经移除的 MCP 接口。当前应用没有开放 MCP 外部工具，也未注册 shell、文件写入或通用网络工具。框架接口以[官方 ReActAgent 文档](https://doc.agentscope.io/tutorial/task_agent.html)为参考，具体行为以固定版本源码和集成测试为准。

## 工具及执行边界

所有发现工具都注册为 AgentScope 工具，模型看到的名称、授权对象和参数 Schema 来自当前 `ToolExecutor`。

| 工具 | 能力 |
| --- | --- |
| `get_metadata` | 读取授权目录，包含来源能力覆盖与字段声明 |
| `search_knowledge` | 查找本次涉及表适用的全局或绑定说明，登记文档版本 |
| `profile_columns` | 有限画像、基数、联合 DISTINCT 等编译操作 |
| `check_fd` | 检查列组函数依赖及反例计数 |
| `check_reference` | 检查授权列组的引用、覆盖和歧义 |
| `execute_readonly_sql` | 执行授权范围内、通过守卫的有限查询 |

执行前检查参数类型、必填项、未知参数和长度；调用后只向模型返回受限观察。SQL 经过原有 QuerySpec、编译器、SQLGuard 和来源适配器复核。工具注册不能扩大表列权限。模型模式为 `metadata_only` 时，实例统计和样本查询仍被拒绝。

工具异常转换成固定错误码，不回显驱动异常和 SQL 原文。预算耗尽必须终止当前探索，不能被 AgentScope 转成普通错误后继续调用。一次响应提出多个工具时顺序执行；某个工具触发预算或取消后，后续工具不执行。未知工具不能走框架默认的原文错误路径。SQLGlot 对不支持语句的 fallback 在记录 SQL 警告前被拒绝。

## 流式与非流式

独立配置文件示例见 [`agent.example.yaml`](../../examples/agent.example.yaml)，完整用法见 [README](../../README.md#agentscope-与模型配置)。运行文件通过 `agent_config` 引用它。分析目的保留在运行 YAML 的 `goal`，不和模型连接参数混在一起。

- `llm.stream: false`：接收完整 Chat Completions JSON。
- `llm.stream: true`：由 OpenAI SDK 解码 SSE，并用其 `ChatCompletionStreamState` 累计正文、工具参数及末尾用量。
- 流式必须收到完成标志和 `[DONE]`，并通过响应与参数校验后才允许执行工具。传输层在内存中组装完整响应，再以 AgentScope 的流式响应接口交付一次完整 `ChatResponse`。
- 两种模式共用字节限制、截止时间、重试次数和 token 预算；不自动从流式降为非流式。不完整流、生成截断、超限或连接中断都不能触发工具。
- 这次支持的是模型接口的流式传输。没有新增浏览器 SSE、实时 token 展示或工作台后端。

网关需支持 OpenAI 兼容的 `tools/tool_calls`、`tool_choice` 和 JSON 输出。只支持文本生成的端点不能执行自主发现。配置允许直填 `api_key` 或读取环境变量；直填非空值优先，密钥不进入配置序列化和运行报告。

`sdk_transport.py` 使用 `openai==3.35.1`。为了在调用工具前拦截超限事件、缺少结束标志和断流，适配层使用了该版本的 SSE 解码器及累计器接口，其中部分是 SDK 内部接口；升级 SDK 必须重跑协议和留存测试。SDK 自带重试关闭，外层每次重试都计入调用预算。工具回合不附 `response_format`；无工具结构化任务继续使用严格 Schema 或显式 JSON 兼容模式。对象形式的非流式 arguments 会规范化，字符串参数的重复键拒绝执行。缺少 `finish_reason` 的非流式响应也不执行工具。

SDK 对不符合类型的响应可能只给序列化警告，默认警告会包含输入值。适配层关闭这类原值警告并显式验证消息和分片类型，只返回固定错误码；开启 OpenAI 调试日志时拒绝发送请求。

## 上下文、限额与清理

每次模型调用重新构建任务上下文；工具探索了另一张表后，下一轮补入该表适用的说明。原始元数据、样本和 Markdown 均作为证据，不能修改系统权限。最终提案仍须引用已发放的列、文档、实体或探查 ID。

`max_discovery_steps` 限制框架推理轮数，`max_tool_calls` 限制单次探索的工具数。达到上限时禁止框架额外调用模型做总结。HTTP 重试也消耗全局调用和 token 预算。发现流程版本已更新，旧版本的完成标记不能直接跳过新的发现路径。

至少四回合的任务在总回合数内预留最后两回合，用于本地提交、字段反馈和结束操作；短任务保留最后一回合。来源查询不能占用提交阶段。提示字节默认上限为 128 KiB，可显式配置；目录窗口还受剩余 token 额度约束，保留当前任务表并缩减可选表。配置增大不会取消任何额度检查。

内存使用 `EphemeralMemory`，禁止 `state_dict` 和会话恢复；不启用长期记忆、自动压缩、Studio、模型原文打印或原文事件队列。如果宿主开启 AgentScope tracing，在原文交给 tracing 前拒绝运行。成功、异常和取消均清理消息及工具闭包，再关闭发现执行器。查询数据只短期存在于进程内存，不进入结果库或运行文件；外部模型服务和客户数据库的日志策略仍需部署侧验证。

当前 CLI 和数据库适配器是同步入口，通过 `asyncio.run` 驱动框架。数据库工具留在原线程，避免破坏 SQLite 等连接的线程约束；HTTP 请求也受同步传输超时控制。本次没有声称提供 Web 服务端的异步取消或多任务调度能力。

## 验证边界

自动测试使用合成数据、内存数据库和 HTTP 替身。验证真实框架循环和 Toolkit 调用、两种协议、多轮观察、作用域更新、参数校验、预算、断流、取消和清理，不调用付费模型。完整测试结果记录在 [实施记录](../../IMPLEMENTATION_STATUS.md)。

尚未验证客户模型网关的具体协议差异、真实 Gauss 行为和生产数据上的发现质量。框架迁移与协议测试通过，不等于业务语义已达标。
