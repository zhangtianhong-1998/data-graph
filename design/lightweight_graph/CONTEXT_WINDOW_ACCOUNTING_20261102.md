# 上下文窗口与用量计量

## 查到的问题

旧实现把完整请求的 UTF-8 字节数用于三个不同判断：请求大小上限、累计 token 预算预留、提供方不返回 usage 时的输入用量。这会把中文、JSON 和英文都按一个字节一个 token 计算。目录预检也沿用这个数，可能在请求发送前就因为累计预算而缩小可见表范围。

历史消息超过字节上限时，旧流程立即把后续回合切到提交阶段，再删除早期工具结果。即使删除后已经有空间，Agent 也不能继续查询缺失的证据。

## 当前实现

| 配置 | 单位与用途 |
|---|---|
| `max_calls` | 整次运行的模型调用次数 |
| `max_tokens` | 整次运行的累计输入和输出 tokens；`null` 表示不限 |
| `max_prompt_bytes` | 可选的请求 UTF-8 字节保护，默认 `null`；不作为默认上下文窗口 |
| `max_response_bytes` | 可选的响应字节保护，默认 `null`；不代替输出 token 上限 |
| `max_prompt_tokens` | 可选的单次输入 token 估算上限 |
| `context_window_tokens` | 可选的输入、输出总窗口；扣除 `max_output_tokens` 后才容纳输入 |
| `max_output_tokens` | 请求给模型的最大输出 tokens |

字节保护字段和可选输入窗口字段默认是 `null`。既有工作区中明确保存的配置没有自动改写；前端提供最大输入 token、最大输出 token、接口超时及可选字节保护的编辑入口。原来的 `max_prompt_bytes: 1048576` 依旧表示 1 MiB。如果服务商支持 1,048,576 输入 tokens，设置 `max_prompt_tokens: 1048576` 即可；旧配置若还保留较小的字节上限，可在高级设置中取消。显式设置两种上限时，它们独立生效。如果服务商所说的 1,048,576 是输入和输出共享的窗口，则应设置 `context_window_tokens`，输出预留会从中扣除。SQL 查询结果、内存和持久化文件使用字节计量，与模型窗口分开。

目录预检、历史裁剪和发送前检查使用同一份实际请求结构，包含工具 Schema。预算预留使用输入 token 估算加最大输出，返回后按提供方 `usage.prompt_tokens` 和 `usage.completion_tokens` 记账。

当前没有安装与任意兼容网关完全匹配的 tokenizer。`mixed_text_v1` 按 ASCII 文本、JSON 标点和非 ASCII 文本分别估算；英文普通文本约四字符一个 token，中文不套用这个比例。这是启发式估算，不能当作模型真实分词结果，也不能证明服务商支持某个窗口大小。

提供方缺少 usage 时，报告分别记录 `estimated_input_tokens`、`estimated_output_tokens` 及对应调用数。模型的隐藏推理等不可见内容无法由本地准确计量。传输中断后的未知用量使用保守预留，单列 `unknown_usage_reserved_tokens`。这些字段都是计数，不保存文本。

## 上下文保留规则

每轮从当前任务重新组织上下文，携带字段知识、已签发引用和提交反馈。超出窗口时，先删除最早的完整源工具调用与结果，保留最近一次源工具交换，以及提交、声明和字段校验反馈。多工具调用不能拆开，未配对的调用不能靠删除部分响应来凑大小。原始会话仍只在本次任务内存中存在，裁剪不会改写该会话。

裁剪后能放下请求，就继续探索；只有仍超限，才尝试缩小为提交工具。调用次数、源查询次数和最后的提交回合仍有原来的上限。模型会收到省略说明：缺失结果不代表反证，不能凭记忆补写原始行；需要时可在余量内重新查询授权来源。

后续按用户要求增加了本地运行轨迹：`record_trace: true`（默认）保留实际模型输入、响应和工具结果，直到用户清理。它们与图谱及字段知识分开存放，不会自动回灌后续任务，也不构成独立业务证据。`record_trace: false` 保留原有不落盘模式。详见 [运行轨迹说明](DURABLE_RUN_TRACE_20261102.md)。

## 参考与验证边界

参考了本地 DeepSeek Harness 的 `packages/compaction/compaction-basic/src/index.ts`、`region.ts` 和 `packages/core/agent-loop/src/agent.ts`：统一计量、输出预留、完整工具配对、保留近期材料、在调用边界组装消息。其持久会话日志和摘要事务没有移植到本项目。

离线回归覆盖中英文估算差异、工具 Schema 计量、独立字节与 token 限制、输出预留、提供方 usage 优先、完整工具配对、裁剪后继续查询、草稿反馈保留、修复调用预算以及流中断不执行工具。它们验证运行机制，不代表真实模型已解决语义分歧；真实抽取质量仍需单独运行和评分。

`timeout_seconds` 是 HTTP 连接及读写等待的超时配置，不是整次模型生成的固定墙钟上限。报告只保留七种传输诊断计数：`read_timeout`、`connect_timeout`、`write_timeout`、`pool_timeout`、`connection_error`、`protocol_error`、`unknown`。公共错误码仍为 `model_transport_error`，重试次数不变，不采集异常消息、地址或响应正文。
