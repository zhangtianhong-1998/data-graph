# Hermes 上下文管理与内核替换评估（2026-11-03）

建议本轮保留 AgentScope，采用 Hermes 的提前压缩、保留最近工具回合、请求与原始记录分离等机制。直接换成 Hermes `AIAgent` 技术上可行，但涉及工具注册、预算、持久化和运行环境迁移，无法作为本轮零规则问题的直接修复。换内核也不会自动解决字段映射、证据引用或规则发布问题。

本轮代码修改采用以下方向：默认 `working_context_tokens=32768` 作为独立软目标，允许设为 `null` 关闭；用户原有硬输入 1,048,576、最大输出 65,536 保持不变。工具数据采用共享列头与二维 `rows`，并显式标注 `positional_values`，减少每行重复列 ID。压缩只使用确定性处理，没有新增摘要模型调用，也没有引入 Hermes 运行依赖。软目标能否达到取决于必须保留的上下文大小，应在轨迹里显示压缩前后 token 估算及仍保留的内容；它不应成为第二个强制拒绝请求的上限。

本评估读取用户提供的 `/Users/sanjiuganmaoling/Desktop/vm/hermes-agent-main`，没有安装依赖、导入运行内核或调用模型。该目录没有 `.git`，`pyproject.toml` 版本为 `0.0.0`，因此以下结论只对应本地文件，不能据此确定上游发布版本。文中的 Hermes 路径均相对于此目录。

## 值得采用的机制

| 机制 | 本地代码依据 | ICT 的采用方式 |
|---|---|---|
| 提前压缩旧工具输出 | `agent/context_engine.py:110`；`agent/context_compressor_prune.py:28` | 增设独立的工作上下文阈值，不等到用户配置的百万 token 硬上限才处理历史。该阈值控制重复发送的内容，不限制整个实验的累计 token。 |
| 先确定性处理，再考虑模型摘要 | `agent/context_compressor.py:3286`、`:5473`、`:5520` | 用程序将旧 SQL 结果变成结构摘要。这样不增加模型调用，也不会让摘要生成再次占满超时预算。 |
| 保留起始约束和最近回合 | `agent/context_compressor_window.py:19`；`agent/context_compressor.py:3298` | 系统约束、当前表结构、对象模型、待解决错误和最近完整工具回合继续可见；不能刚返回查询结果就把它删掉。保留工具调用与结果的对应关系。 |
| 保留可恢复的来源 | `agent/context_compressor_tail.py:46`、`:71` | 老结果显示证据引用、列信息、计数、完整性以及如何重新获取。Hermes 使用 `session_search`，ICT 应使用自己的受控结果读取或查询工具，不能引用不存在的工具。 |
| 请求上下文与原始轨迹分离 | `agent/context_engine.py:121` | 每次请求只使用选定的上下文副本。原始 Agent 轨迹、工具结果及用户主动保留的运行记录不因压缩而删改。 |
| 避免每轮重写历史 | `agent/context_compressor_prune.py:77` | 只在能明显减少输入时压缩，压缩后留出增长空间。反复改写历史会破坏模型服务端的前缀缓存。 |
| 压缩失败有明确退路 | `agent/context_compressor.py:5539`；`agent/conversation_compression.py:3` | 压缩没有产生有效结果时保留原始数据；若请求仍超出硬上限，明确报告本次请求无法发送。不得让超时的摘要任务回来覆盖较新的状态。 |

Hermes 的完整压缩流程还会用辅助模型生成结构化摘要。摘要包含目标、约束、完成动作、当前状态、未解决问题和错误修复；后续摘要在上一份基础上更新，见 `agent/context_compressor.py:3959`。这对长对话有用，但 ICT 的证据要求更严格：模型摘要不能代替已签发证据，也不能自行新增列 ID、文档章节 ID、对象身份结论或 `supported` 审查结果。

ICT 第一阶段适合使用程序生成的结构摘要，保存已确认对象模型、提案状态、可用引用、查询列名与类型、结果行数、查询是否完整，以及有界统计。原始行应只在当前查询回合和按需回看时进入上下文；具体缓存落盘应遵循项目已有留存设置，并与完整轨迹的手动清理规则一致。当前这份评估不意味着已经实现磁盘结果缓存或模型摘要。

## Token 估算不能直接照抄

Hermes 也没有在所有调用前使用模型的精确 tokenizer。`agent/model_metadata.py:2347` 对 ASCII 近似按四字符一个 token，对中日韩文本近似按一字符一个 token，其余文本按 UTF-8 字节估算；`:2579` 会把工具 schema 计入请求。`ContextEngine.update_from_response` 再接收实际用量。

因此，使用 Hermes 并不会自动消除估算误差。ICT 应保留“估算值”和“服务商返回用量”的区别，并分别统计 system、工具 schema、当前任务和历史结果。字节上限仍可用于 HTTP 传输及内存保护，但不能冒充模型 token 窗口。

Hermes 的压缩触发值扣除了输出预留，见 `agent/context_compressor.py:2770`。它另有 `proactive_prune_tokens` 和 `threshold_tokens_cap`，见 `:2812`。其中提前清理默认可以是关闭状态；仅换内核而不配置这些值，也可能继续出现大窗口下迟迟不压缩的问题。

## 内核能否直接替换

Hermes 提供可调用入口：`AIAgent(...)`、`run_conversation(user_message, system_message, conversation_history)` 和 `chat(...)`，分别见 `run_agent.py:236`、`agent/turn_facade.py:22`、`:220`。其消息格式与 OpenAI 兼容。可以据此做独立运行后端，但当前接口并非直接传入 ICT 的 `Toolkit` 和 `ProposalSession` 就能运行。

| 兼容点 | 本地实现与迁移要求 |
|---|---|
| 工具 | `agent/agent_init.py:1011` 会发现插件并从全局 registry 获取工具；`tools/registry.py:1050` 创建全局注册表。需要将 ICT 工具注册为隔离的插件或专用工具集，并保持每个运行的会话状态隔离。 |
| 工具契约和审批 | `model_tools.py:866` 将工具调用交给 hooks、middleware 和 registry，返回 JSON 字符串。其终端审批不能代替 ICT 的只读 SQL 守卫、数据源授权、引用验证和提案校验。ICT 原有守卫必须继续执行。 |
| 运行方式 | `agent/conversation_loop.py` 使用同步循环，ICT 当前由 AgentScope 管理异步工具调用。需要适配取消、超时、前端任务生命周期及异常分类，不能在 Web 请求事件循环里直接阻塞执行。 |
| 配置及全局依赖 | `run_agent.py:9` 有启动环境处理；`agent/agent_init.py:2202` 设置日志，`:2228` 读取 Hermes 配置；初始化还涉及插件、记忆、技能和服务商路由。必须使用独立运行目录与明确配置，不能读取用户默认 Hermes 配置后意外改变 ICT 的行为。 |
| 持久化 | `run_agent.py:244` 支持关闭轨迹保存，`:270` 支持跳过上下文文件和记忆，`:272` 的 session DB 可选。但压缩/召回/锁流程有自己的会话存储协议；仍需逐项核对实际写盘，不能仅凭一个关闭开关承诺无副作用。 |
| 预算 | `agent/iteration_budget.py:25` 是循环次数计数器，`agent/conversation_loop.py:126` 会按墙钟预算提示收束。辅助摘要、重试和工具调用需统一计入 ICT 的模型调用、查询、阶段截止时间及整个实验预算。其默认计数不能直接视为 ICT 的物理请求计数。 |
| 结构化提交与发布 | Hermes 提供通用工具循环。`ProposalSession`、声明对象模型、`finish_discovery`、语义复核、规则编译和依赖组原子发布仍是 ICT 的业务逻辑，需要全部保留。通用最终文本不能替代合法提交。 |
| 依赖 | 本地 `pyproject.toml` 对 Python 3.14 环境固定 `openai==2.54.0`；ICT 当前固定 `openai==3.35.1`。Hermes 项目说明实际支持环境已转到 Python 3.14，ICT 则声明 Python 3.11 以上。直接装入同一虚拟环境存在依赖与平台回归成本。 |

如果以后确实需要更换内核，适合先做独立进程实验后端：只暴露 ICT 的只读探查和提交工具，使用独立 Hermes 配置；将所有模型物理请求接回统一预算、取消和轨迹系统；在相同输入下比较有效规则、证据完整性、调用数、最大单次输入和墙钟时间。独立进程可以减少全局 registry、配置和依赖冲突，但仍需完成上述业务适配。

## 是否存在可单独复用的轻接口

有明确的扩展接口，但没有在本地发现与 Hermes 宿主解耦的开箱即用压缩库。

`agent/context_engine.py:48` 定义 `ContextEngine` 抽象类，提供 `update_from_response`、`should_compress`、`compress`、`prune_tool_results_only`、`select_context` 和会话生命周期方法。这个接口划分值得采用。`select_context` 明确要求只改变本次请求，原始历史保持不变，特别适合 ICT。

完整 `ContextCompressor` 除主文件外还依赖多个 mixin、辅助模型路由、模型目录、提示构建、工具、会话压缩和持久化模块，见 `agent/context_compressor.py:16`。连抽象类的默认 `update_model` 也会导入 compressor，见 `agent/context_engine.py:233`。将文件复制进 ICT 后直接调用并不构成独立轻量依赖。

本轮建议实现同样职责清晰的请求压缩接口，保留现有 AgentScope 与 SDK。后续若确定性压缩仍不足，再为“已确认结构状态”增加一次有界、可关闭的辅助摘要；摘要失败不影响原始轨迹，也不改变规则是否通过验证。

## 许可证和验证范围

本地 `LICENSE` 为 MIT，版权声明为 `Copyright (c) 2025 Nous Research`。如果复制或分发实质代码，需要保留其版权与许可声明；本次采用机制设计，没有复制 Hermes 实现。

本次只完成源码审查。没有证明 Hermes 替换后可通过 ICT 全量验收，也没有证明上下文缩小后真实网关一定不再超时。真实网关的排队、吞吐、网络和推理时间仍需单独测量。

用于识别此次审查文件的 SHA-256：

```text
run_agent.py
da1fba4fb3cfd2153832087e904f8d47ac1a55e853e399cb7885f4bdf986e129
agent/context_engine.py
516a4961c59af4c1048dc523b83f135b1fd0089423775fbe3fc04ffdcdb45cbb
agent/context_compressor.py
50d0e7427669da61055fabc873493220ecb51086d7b5f2ec8cb7b5e67a4142da
agent/context_compressor_prune.py
567abd766f6963fde1a1a9d0863fd2c6fb42c9cfd37ee9036577dedbc40fad6a
```
