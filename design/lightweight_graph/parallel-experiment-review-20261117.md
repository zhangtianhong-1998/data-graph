两路并行实验：诊断与改造方案

日期：2026-11-17。代码基线：`fb0bbd8`。本轮只做诊断和设计，未修改业务代码、配置或实验产物。

**1. 结论与证据范围**

这次反馈暴露了四个相互影响的问题：历史结果读取依赖当前运行配置；工作流用运行状态决定产物是否可发布；“合并”默认还包含一次自主抽取；缺少可恢复的节点检查点。它们叠加后，会出现“已经花了数小时、有已发布数据，但项目页看不到，也无法从合并继续”的结果。

建议先修可见性与恢复，再优化重复计算。标签分歧应成为可追溯的质量信息，不应默认让整个实体及其关联关系消失。身份是否相同、名字显示什么、结果覆盖多少，应分别判断。

| 证据 | 本轮使用方式 |
| --- | --- |
| 当前本地源码 | 已逐项核对，可确认控制流程和现有功能 |
| 用户提供的运行分析 | 用于分析本次实验的时长、调用量和冲突；原记录无法同步，未重新统计 |
| Semantica 公开源码 | 已读取实际实现，以下链接为本次核查的 main 分支文件 |
| 当前回归测试 | 本轮选取的图访问、工作流贡献失败、继承合并、安全回归四个测试文件，41 项通过，耗时 8.43 秒 |

测试通过只说明当前实现符合现有测试；其中部分测试恰恰要求“路径变化后拒绝旧图”和“partial 阻止下游合并”。新方案需要修改这些行为与对应测试，不能用现有测试通过宣称问题已解决。

**2. 为什么有节点、关系统计，前端却没有图**

反馈中的原因链与当前代码一致：

1. merge worker 在自己的工作目录提交了图谱；报告记录了数量。
2. 父编排仅接受 `completed / completed_with_gaps` 的 merge worker，遇到 `partial` 直接跳过后续组合。
3. 终端节点没有进入可用输出集合，根发布标为 `workflow_units_not_ready`。
4. 查看器读取项目输出的 `construction/manifest.yaml → current_graph`。首次运行没有旧根图可保留，因而没有可显示结果。

证据：[合并 worker 的状态门槛](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/execution.py:717)、[终端发布门槛](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/execution.py:773)、[查看器的读取入口](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/construction_graph.py:49)。

同类矛盾也存在于抽取节点：首次 partial 可以登记到 `ready_unit_attempts`，随后在加载贡献和检查合并依赖时又被状态门槛拒绝。只修改 merge 的一处判断不够。[相关代码](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/execution.py:608)

因此，前面对“发布没有被阻止”的解释只覆盖了 worker 内部发布，没有覆盖项目发布。把这种情况称为“工程层面完全成功”也不准确：用户获取结果的最后一步确实失败了。

反馈中的 `published_definition_conflict` 也不能直接等同于 33 个名称分歧：当前 merger 在实体/关系定义不兼容时就会抛这个错误。没有原始诊断明细，不能确认所有问题都由同一组标签冲突引起；修改方案要分别处理定义冲突和名称分歧。[定义冲突检查](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/merger.py:185)

但不能一律接纳所有 `partial`。输入文件在运行期间变化也可能得到这个状态。建议将以下四项分别记录：

| 维度 | 建议值 | 用途 |
| --- | --- | --- |
| 执行状态 | completed / partial / failed / cancelled / interrupted | 本次任务执行到什么程度 |
| 产物状态 | none / committed / invalid | 是否存在已提交、可验证的产物 |
| 覆盖与质量 | coverage=complete/incomplete/unknown；quality_flags=[] | 缺哪些目标、是否有名称分歧或隔离 |
| 项目发布状态 | published / retained_previous / blocked | 当前可见图来自哪里，为什么未更新 |

发布条件应检查具体产物：提交记录、摘要、身份与端点约束、依赖兼容性、当前结果访问权限，以及是否存在禁止提升的输入变化。已完整验证的干净子集可以发布，同时显示覆盖不完整。执行失败时，先前已提交的检查点仍保留为可查看历史；是否成为本轮最终输出需要明确策略，不能仅凭文件存在自动提升。

一个终端分支不可用时，应按目标依赖决定处理范围。依赖它的新增关系暂停；其他已经提交的独立分支仍可查看。允许发布部分输出的工作流要列出缺失分支和目标；要求全部分支齐备的工作流则保留旧项目图，同时开放新检查点的历史查看。

建议按影响范围处理异常，而非统一升级为全局失败：

| 异常 | 建议处理范围 |
| --- | --- |
| 同一已确认身份的名字分歧 | 保留实体和全部观察，附质量诊断 |
| 个别关系端点缺失、局部关系成环 | 依关系约束隔离受影响的边及依赖结果，记录覆盖缺口 |
| 实体类型或身份定义不兼容 | 不强行合并；隔离有明确边界的定义与依赖贡献，保留独立结果供检查 |
| 某来源暂不可用或额度耗尽 | 停止依赖它的新验证，保留已提交的完整分区；不能把半次投影当完整结果 |
| 无法确定冲突边界、快照损坏或访问权限失效 | 阻止受影响产物发布，保留旧项目头；其他授权且完整的历史快照仍可读 |

这些隔离策略要在运行前声明，并输出被排除的节点、边、定义和目标。无法证明剩余图满足自身约束时，不应自动发布。

**3. 指纹问题：应拆开三类校验**

当前 `access_fingerprint` 包含源文件路径、凭据版本、继承配置，也包含隔离域、允许的表列和作用域绑定。查看器要求它与发布时完全一致。路径迁移或连接配置变化可能与真正的授权变更一起被判为不可读。[指纹内容](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/access.py:34)、[拒绝读取的位置](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/construction_graph.py:65)

不能直接删除指纹校验。应改成：

| 校验 | 应绑定的内容 | 不应承担的责任 |
| --- | --- | --- |
| 历史结果访问授权 | 项目、快照所属隔离域、当前用户/会话可见范围、明确的权限撤销 | 不因源文件换盘符、模型参数变化就拒绝历史浏览 |
| 运行与缓存复用 | 稳定来源身份、数据版本、映射、身份规则、直接上游快照、实际任务参数 | 不因移动画布节点、改展示名称就重跑抽取 |
| 文件完整性 | 图摘要、Parquet 文件 SHA-256、格式版本、文件路径边界 | 不替代业务身份与访问授权 |

快照属于哪个项目、谁可读，应独立于“现在还连不连接原数据库”。凭据变更可能仅是轮换，也可能切换到了不同数据库，需要来源身份与权限策略来判断；不能默认所有凭据变化都无害。

历史读取不应先解析一份要求所有源文件仍然存在的运行配置。只读取快照所需的项目身份和结果权限；继续逐条过滤 supports，并从可见证据重新生成名称、属性和关系。[当前 supports 过滤](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/access.py:67)、[完整性校验](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/graph_storage.py:220)

旧数据迁移也不能简单把旧指纹改成当前指纹。如果只有旧哈希，没有发布时的配置或授权描述，无法从哈希反推出“仅路径变化”。有历史配置的，结合项目相对路径和稳定来源 ID 建立迁移记录；缺少证明的，明确显示迁移缺项，由项目所有者显式登记历史结果访问规则。迁移不重写原图，也不让旧快照冒充当前源数据。

**4. 历史图谱：需要可选择的快照，而不只是保留文件**

项目已有 `output/generations/<run>` 和 `output-head.yaml`，但是图谱 API 没有版本参数；当前配置只解析当前 generation。[输出代次](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/workspace.py:555)、[图谱 API](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/app.py:758)

`publication_history` 主要记录单草稿发布；完整合并图的 `current_graph` 会被更新。因此，目录里有 22 个文件，不等于有 22 个可切换的完整项目图：还可能包含单草稿图和未提交残留。[草稿发布](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/workspace.py:435)、[项目发布](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/merger.py:523)

建议新增不可变快照登记记录，至少包含：

```text
version_id, project_id, run_id, node_id, attempt_id
graph_kind: project_full | node_full | node_delta | draft
parent_versions, graph_digest, relative_path, created_at
source_read_windows, schema_version, identity_version
coverage, quality_flags, result_access_policy
```

每次完整图发布都登记快照，随后原子更新当前头。进程崩溃留下但没有提交记录的文件，不能自动列为已发布版本。

建议接口：

```text
GET /api/projects/{pid}/graph/versions
GET /api/projects/{pid}/graph?version=<id>
GET /api/projects/{pid}/graph/batches?version=<id>&generation=<token>
GET /api/projects/{pid}/graph/details?version=<id>&...
```

图谱页提供“当前发布 / 历史运行 / 节点产物”选择，显示时间、覆盖状态和所属节点。画布、分页、两跳查询、详情、导出都固定在同一 version；切换时取消旧请求，避免晚到的数据混入新图。仅浏览历史不修改 `output-head`，不改变下一次运行的输入，不触发模型或数据库请求。

历史的分页 token 应绑定快照摘要、有效权限和查询条件。新运行发布另一个版本时，正在查看的历史图保持稳定；权限撤销时旧缓存必须失效。列表中的数量也应按可见范围计算，不能先泄漏原始全图统计。

旧数据优先登记各 manifest 当前头、各 generation 当前头和 worker 已提交头。重建更早的完整图需要当时有效的各草稿版本与固定继承基图，不能按文件修改时间猜测。

**5. 256 分钟合并的耗时判断**

以下数字均来自用户提供的分析，未复算原始事件：

| 阶段 | 反馈时长 | 可以得出的判断 |
| --- | --- | --- |
| 两路抽取 | 157 / 155 分钟，重叠执行 | 并行已经发生，关键路径约为较慢一路 |
| merge_1 | 256 分钟 | 约占总墙钟 413 分钟的 62%，应优先处理 |
| 模型调用 | 78 分钟，512 次 | 约占 merge 的 30%，不能称为不重要 |
| validate_graph_draft | 94.6 分钟 | 验证是最大的已列工具耗时 |
| publish_graph_draft | 10.1 分钟 | 仍要关注序列化、重复合成和写盘 |
| inspect_source | 13.3 分钟 | 补充抽取带来额外源访问 |
| read_workspace/read_material | 7.9 分钟 | 上下文与材料读取也有成本 |
| harness/压缩等 | 约 50 分钟 | 归因较粗，不能全部视为框架开销 |

这些大项约合 253.9 分钟，接近 256 分钟，但不足以证明各项互不重叠。连接、资格检查、SQL 是验证工具内部的子耗时，不能再叠加到 94.6 分钟之上。按反馈的 273 次连接、每次约 20 秒估算约 91 分钟，只能作为建连开销的敏感性估算，不能据此认定验证时间几乎全花在建连。

同样，模型调用中位数约 6 秒不能代表 512 次调用的总时长；长尾、压缩请求和不同调用类别都可能改变总量。应减少不必要的任务与往返次数，再评估模型延迟，而非仅依据中位数排除模型侧成本。

当前报告还区分 `db_queries`（数据操作）与 `db_query_admissions`（包含目录、事务等 SQL）。10448 与旧版 954 是否使用同一指标、覆盖同一任务，需要保留疑问；不能仅以两者比值断言逐节点查询。[现有计数定义](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/runner.py:317)

代码支持的原因如下。

**5.1 合并节点实际又启动了抽取任务，界面默认选择了这个模式。**

后端已有 `deterministic` 和 `merge_and_extract`，并默认前者；前端创建节点和自动生成工作流时却设置后者。编排先确定性合并上游，再启动 merge worker，因此用户眼里的“合并”实际上可能包含数小时的新发现、起草、验证和发布。[后端配置](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/runtime/config.py:224)、[前端默认](/Users/sanjiuganmaoling/Desktop/vm/ict/src/workbench/src/WorkflowEditor.vue:161)、[执行分支](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/execution.py:689)

现有提示已经要求用 `__base_graph__` 复用上游，但工具权限和执行预算仍允许继续探索，不能靠提示保证只做增量。[当前合并任务提示](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/workflow.py:299)

建议默认“合并已有结果”，使用现成确定性合并。需要新跨表关系时，显式添加“补充抽取”节点，指定待补关系、允许查询的来源、预算和停止条件。确定性合并先提交可见结果；补充抽取失败只影响新增目标，不能把已经合并的图一起藏起来。

这会改变任务范围：纯合并不会自动产生原本只能通过补充抽取得到的新关系。因此应分别对比基础图和补充关系的数量、质量，不能只比较时间。

**5.2 缓存已经存在，主要不足是跨验证调用的复用。**

`execute_draft` 已有 `projection_cache` 和 `lookup_indexes`；端点 lookup 是读投影后建内存索引，并非逐节点发送 SQL。还已有可选 co-projection，以及 CSV/Excel 不变输入的验证复用。[投影缓存](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/executor.py:95)、[lookup 索引](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/executor.py:513)、[已有验证复用](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/tools.py:823)

每次数据库实例验证仍重新执行草稿，单次缓存不会自动跨修订保留。建议分两步：先让 merge 只读已提交上游快照；再做投影或 mapping 级缓存和依赖失效。缓存键至少绑定映射语义、类型化身份、源版本/冻结提取批次、权限、过滤口径和执行器语义版本。

数据库的 `read_window` 只是客户端观察时段，不是数据库快照版本。没有可靠数据版本时，可以明确复用“此前验证的历史快照”，不能把缓存说成“已经验证当前数据库”。本次修订改了一个端点映射时，还应重检依赖它的关系、跨映射冲突和全局无环约束，不能仅重算改动行。

**5.3 每个投影重新建立物理连接并重复资格检查，确有优化空间。**

当前每组 distinct 计数与完整投影使用独立 snapshot，结束后释放连接；下一组重新连接、检查目录与能力。[投影执行](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/executor.py:339)、[连接释放](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/sources/gauss.py:694)、[再次资格检查](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/sources/reconnect.py:213)

建议先实现一次工具调用内的会话复用：物理连接保留到本次验证结束，各投影仍用独立只读事务；返回模型之前释放会话。新建或故障后的连接仍完整检查，发生异常的会话丢弃，不让部分旧快照数据与重连后数据拼成完整结果。再按测量结果评估更长期的连接池，避免直接让长事务跨越模型思考阶段。

co-projection 可合并同源同条件的部分投影，但列联合会提高 distinct 基数和内存，应保留回退机制，不能只打开开关就假定总是更快。

**5.4 标签分歧可提前检测，但不是单靠 structure 阶段就能证明。**

同码不同名是实例数据问题。可以在第一次必要投影时汇总，或对已确定的 key/label 映射提前执行分组查询；仅检查表结构无法发现这类分歧。跨表、复合键、作用域、NULL 和过滤条件必须与正式身份语义一致。更关键的是让已知名称分歧走确定性处理，不触发反复改草稿、验证和重试。

性能目标应先用操作量验收：纯合并 0 次模型请求、0 次源数据库查询；恢复 merge 时 0 次上游抽取；未改 mapping 不重复物化；相同原始证据不重复计票。随后在同一输入、同一机器上测墙钟、峰值内存和覆盖差异。现有材料不足以承诺“256 分钟降为 10–20 分钟”或“整体必然低于 3 小时”。

**6. 三表两两组合：合并身份，保留各个观察与引用**

你的思路适合已经确认共享身份的实体。假设两路是 A+B 与 A+C：同一 A 表记录被两路观察到，应合成一个实体，保留原始来源引用以及两条抽取执行记录；A、B、C 真正不同的来源观察分别保存。

需要先确认两个节点确实使用同一身份域、实体类型、键定义、键值类型和作用域。同名或相同 `code=001` 不能自动证明同一实体。当前 merger 已经对此作限制：未声明共享的类型会产生 unit-scoped ID；共享且身份一致时已经会追加 supports/provenance。[身份共享条件](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/merger.py:86)、[合并 supports](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/merger.py:219)

建议统一三层记录：

| 记录 | 示例内容 | 去重规则 |
| --- | --- | --- |
| 实体身份 | 产品身份域、entity type、类型化编码、业务作用域 | 只有契约一致才合并 |
| 原始观察 | 逻辑来源、表、源键、字段、观察值、scope、筛选口径、观察批次 | 同一观察只记一次；不同值或时间版本保留 |
| 执行出处 | run、node、attempt、mapping、evidence ref | 追加所有产生该观察的执行来源 |

关系也应按其业务语义去重。普通二元关系可用类型和两端身份；带时间、角色或次数意义的事实不能只按两端节点压成一条。合并后必须保留关系自身的证据，不能只给节点加引用。

名称处理建议新增 `retain_variants` 策略：身份一致时保留节点、关联边、所有 observed_label 与各自引用，并记录名称分歧；展示名优先使用明确配置的主源。没有主源时采用稳定展示规则，标注存在分歧，用户可展开查看各来源。显示一个名字不代表判定其他名字错误，也不要求必须做投票。

如果以后引入投票，应按独立原始来源计算，不能按 supports 数量、行数或 worker 次数投票。两路都包含 A 时，A 不应得到两票。即使 B/C 两张表来自同一上游系统，也未必构成独立证据。主源优先级需要细到逻辑表/字段；当前 `authority_sources` 主要按 source_id 区分，三张表共用数据库 source_id 时不够用。

质量状态要反映实际缺口：保留全部实体但名字有分歧，不必仅因分歧标 coverage=incomplete；真的隔离了 33 个实体及其边，则继续标明覆盖缺口。是否总体覆盖完整还取决于其他目标，不能只消除一个冲突标记就改 completed。

标签策略须贯穿实例验证、workspace 发布、跨草稿隔离、跨单元 merger、报告和授权后的显示。只改 `conflicts.py` 或最终 merger，无法找回上游已经隔离掉的节点。要恢复这些节点，需要读取保留的验证候选，按新策略重建新的发布版本。

**7. Semantica 可以借鉴哪些内容**

建议借鉴属性聚合、来源记录和策略接口，不直接引入整套模糊消歧算法。当前有可靠编码时，先用确定性身份合并；确实没有键或需要候选对齐的场景再单独评估相似度算法。

| 机制 | 源码实际行为 | 对当前项目的建议 |
| --- | --- | --- |
| merge_all | 将冲突 property 值聚合成列表 | 借鉴多值保留；不等于所有顶层名称都会自动保留 |
| 顶层 name | 未配置属性规则时仍使用 base entity 的名称 | 单独设计 observed_label 与 display label |
| keep_most_complete | 根据属性数和关系数挑选实体 | 完整程度不等于权威性，也不等于主数据表优先 |
| PropertySource | 保存一个 value 和 sources；新值不同会覆盖 value | 不能直接用它实现每个值及其来源的无损历史 |
| voting | 对 conflicting_values 使用 Counter | 需要先去除同一原始证据的重复抽取，不能照搬票数 |
| confidence × credibility | 使用传入权重；缺少信息时有回退 | 没有可信权重就不能宣称自动找到了真值 |

源码：[属性合并与名称规则](https://github.com/semantica-agi/semantica/blob/main/semantica/deduplication/merge_strategy.py#L354)、[merge_all](https://github.com/semantica-agi/semantica/blob/main/semantica/deduplication/merge_strategy.py#L484)、[keep_most_complete](https://github.com/semantica-agi/semantica/blob/main/semantica/deduplication/merge_strategy.py#L391)、[SourceTracker 的值更新](https://github.com/semantica-agi/semantica/blob/main/semantica/conflicts/source_tracker.py#L220)、[投票与权重](https://github.com/semantica-agi/semantica/blob/main/semantica/conflicts/conflict_resolver.py#L387)。

前文“只差一个规范标签投票规则”的判断过于简单。当前真正还缺独立观察去重、身份契约统一、历史产物读取、发布状态拆分和节点恢复；这些都不是移植消歧算法就能解决的。

**8. 从合并节点恢复：需要节点边界的检查点**

当前限制有明确代码依据：

| 当前行为 | 后果 |
| --- | --- |
| retry_unit 只接受 workflow.units | merge_nodes 无法重试 |
| 执行器要求 target_units 非空 | 无法实现“零抽取、只跑 merge” |
| 旧 /workflow/merge 拒绝显式 DAG | 无法用该接口补做本次 merge |
| 抽取单元重试重置全部 merge 状态 | 没有按受影响后代进行最小重跑 |
| 只有一份 workflow-state.json | 下一次运行覆盖旧节点状态，历史恢复缺少依据 |
| 确定性 merge 结果主要保留在内存 | 中间 merge 已完成也不能稳定跨进程复用 |

证据：[retry_unit](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/execution.py:901)、[非空抽取要求](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/execution.py:420)、[DAG 拒绝旧合并入口](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/execution.py:984)、[唯一状态文件](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/workflow.py:585)。

建议每个节点提交产物后立即落盘检查点，确定性 merge 也不例外。保存 node_id、attempt_id、实际输入摘要、固定上游 artifact IDs、产物摘要、覆盖信息与提交状态，同时保留当次 DAG、节点有效配置、恢复来源 run、执行器及身份契约版本、来源数据版本（没有则明确 unknown）。每次 run 单独保存状态历史；项目级 workflow-state 仅作为当前运行的快捷视图。

产物与检查点使用同一提交 ID 和可恢复的提交协议。进程在写入二者之间退出时，下次启动能识别已提交产物和未完成登记；完整快照提交后才原子更新项目头。不能把一次普通 JSON 写入视为覆盖全部文件的原子事务。

建议接口：

```text
POST /api/projects/{pid}/runs/{source_run_id}/resume
If-Match: <当前配置版本>
{
  "from_node_id": "merge_1",
  "action": "rerun_node"
}
```

返回新 run ID、复用节点、重跑节点、固定输入版本及依赖不兼容原因。启动前核验所有必需的可复用上游产物，缺失或损坏时指出具体节点与原因，不静默扩大为重新抽取。执行时以目标节点的后代闭包决定重跑范围；其他节点复用已提交产物。旧 run、旧 attempt、旧图不覆盖。前端按钮应叫“从此节点重新运行”，不声称能续接到模型的第 513 次调用。

另提供 `publish_artifact` 动作：请求显式携带固定 `artifact_id`，指定已提交的终端完整图，核验后只做项目提升，不调用模型、不查询源数据。不能依赖不断移动的 current 指针。

复用判定应按节点实际依赖，而不是整个 workflow JSON。当前摘要包括画布位置等字段；把 merge 从 merge_and_extract 改为 deterministic，也可能导致所有上游复用被拒绝。新设计中，改变 merge 策略只使该 merge 及其后代失效；改变 extract 实际来源、身份规则或任务要求，才使对应 extract 和后代失效。[现有总指纹](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/execution.py:428)

预算、运行时限和模型配置应留在执行记录中。提高失败节点的预算通常不应使已完成上游数据失效；需要重跑模型节点时仍要记录新配置，不假装结果与旧执行完全一致。

**9. 本次已有成果如何恢复**

按反馈，优先利用已落盘数据，顺序如下：

1. 登记两路 extract 的已提交快照、merge 的 frozen upstream、已提交 delta 和保留候选，核验其归属与摘要。
2. 先提供 worker 产物只读查看；页面明确是完整组合图、增量还是单草稿，不把各类数量混在一起。
3. 若仅需查看已隔离冲突后的结果，使用本次 frozen upstream 与已提交 delta 按原身份/合并策略重新组合，生成新的项目完整快照，再执行项目提升；保留原始 run 的 partial 记录。
4. 若要恢复那 33 个实体，则从 retained validation candidate 重新应用 retain_variants 策略，保留名称分歧证据，重新做纯数据约束检查，再产生新快照。不能只改 coverage 或 completed 标签。
5. 若当时上游材料不完整，按产物实际范围展示缺口，不凭报告中的计数重造节点。

不能直接把 worker manifest 复制到项目输出：父编排原本要组合 `merged_upstream + m_delta`；有些 delta 的边还引用继承端点。[原组合流程](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/web/execution.py:728)

反馈中的 5811/7961 是增量还是组合后的 business counts 不能从摘录确定。当前 runner 同时记录两组数字，最终恢复应以实际字段与快照为准。[计数区别](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/runner.py:265)

**10. 实施顺序与验收**

| 顺序 | 修改范围 | 完成标准 |
| --- | --- | --- |
| 第一批：结果可见 | 快照登记、历史读取、访问校验拆分、产物可用性与执行状态分离 | 移动目录仍可看授权历史；partial 合格产物可见；损坏或失效产物不提升 |
| 第二批：可恢复 | 每 run/node 检查点、任意节点恢复、独立项目提升 | merge 恢复 0 次 extract；只重跑后代；新运行不覆盖旧状态 |
| 第三批：合并行为 | 默认 deterministic、补充抽取独立、共享身份契约、retain_variants、原始观察去重 | 同身份实体及边保留多源引用；重复抽取不增票；名称分歧不整图阻断 |
| 第四批：验证性能 | 单次验证会话复用、co-projection 评估、mapping 级依赖缓存、重复上下文削减 | 同覆盖条件下减少物理连接和重复投影，并能解释缓存失效 |

前两批共用快照登记，应一起设计。第三批中的默认合并选项可以较早调整，但须配合第二批允许修改 merge 模式后复用上游，否则用户仍会被迫重跑抽取。

至少覆盖以下验收场景：

| 场景 | 期望 |
| --- | --- |
| Windows 到 Mac 路径迁移，源离线 | 授权后的历史快照可读，不自动认定可继续当前源抽取 |
| 历史 A/B 切换，同时有新发布 | 画布、详情、分页始终是选定版本，不混入晚到响应 |
| 当前权限收缩 | 旧缓存失效，不暴露无权 supports、标签与统计 |
| Parquet 损坏或非法路径 | 继续拒绝读取，不因历史模式放宽完整性检查 |
| merge partial，已提交干净子集 | 可查看并按策略发布，明确覆盖缺口 |
| partial 原因为输入变更 | 禁止自动提升为本轮有效结果，旧项目图保留 |
| A+B 与 A+C 都抽到同一 A 观察 | 一个实体、一份原始观察、两条执行出处；不重复投票 |
| 相同编码但身份域不同 | 保持独立，不误合并 |
| 从 merge_1 恢复 | 复用固定上游版本，不运行 extract；仅目标和后代重跑 |
| 只修项目发布 | 0 次 SQL、0 次模型调用，旧 run 不改写 |
| 只移动画布或提高 merge 预算 | 已完成 extract 仍可复用 |
| 改上游真实输入或身份语义 | 对应节点及后代失效，并解释具体差异 |
| 同一快照重复纯合并 | 内容结果和引用去重稳定；发布时间、运行 ID 可不同 |
| 缓存跨数据库读取时点 | 明确使用历史快照或重新验证，不伪称当前一致视图 |

后续性能报告应使用现有 phase_timings，并补齐逐节点关键路径、模型/压缩、连接建立、资格检查、distinct 计数、投影、物化、序列化和发布的边界。按同一数据快照、相同关系目标比较；同时报告隔离数量和新增关系数量，避免把少做任务当成等价加速。

**11. 补充：跨源实体与属性的固定融合规则**

用户补充的重点是：相同实体在不同表中携带不同引用和属性，抽取后仍出现多个节点。对此应建立可重复执行的融合规则，不能每次交给 Agent 临时决定整个节点保留、丢弃还是重建。

本轮直接调用现有 merger 和授权后的显示过滤，运行了四个最小内存样例。它们验证当前实现行为，不代表对实际实验图做了重复率统计：

| 输入差异 | 现有结果 |
| --- | --- |
| 同 type、同编码、同名称，属性不同，未声明共享类型 | 保留两个节点，各自一个 support |
| 上述输入，声明共享类型 Product | 合成一个节点，两个 supports；region=[华东,华南]，owner/category 等不同属性互补保留 |
| 已声明共享，但名称分别为“网关”和“企业网关” | 默认 quarantine 隔离唯一共享实体，输出零节点 |
| 同类型同编码，但 key_names 分别为 code 和 product_code | 抛出 published_definition_conflict |

因此，普通属性值不同本身通常不会阻止当前 merger 合并。它已有属性集合并集操作；当前更可能造成拆分或阻断的是共享身份未声明、实体类型不同、键定义不同、键值类型不同，以及名称冲突。实际实验中各项占比仍需具体节点才能确认。[属性并集](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/merger.py:233)、[节点 ID 生成](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/executor.py:481)

还需区分实体定义和来源映射。物化后的实体定义排除了 mappings，所以两表挂载了不同 properties，并不会仅因此触发定义冲突；key_names、display_mode、identity_origin 等定义不一致则可能触发。当前 shared_identity_domains 在 merger 中展开为共享 type 集合，没有完成跨字段重命名、跨类型统一或编码转换。[定义生成](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/executor.py:615)、[共享类型处理](/Users/sanjiuganmaoling/Desktop/vm/ict/src/lightgraph/construction/merger.py:112)

建议将固定规则实现为五步，每一步保存规则版本和结果诊断：

1. **统一实体与字段定义。** 在并行抽取前固定共享实体契约，例如把 A.product_code 与 B.prod_id 映射到 Product.product_id；把 A.owner_department 与 B.owner_org 映射到同一个已确认的业务属性。物理列名可以不同，语义字段一致。缺乏等价依据的字段保留各自定义，生成对齐候选。已有结果可在融合入口通过明确的版本化映射转换，不必自动重抽全表。
2. **根据身份归组。** 业务实体使用“身份域、实体类型、规范键、必要业务作用域”建立稳定键。引用源、worker ID、名称和普通属性不加入已确认共享实体的身份键。编码规范化采用显式规则，保留原值；不能自动把有前导零的编码、数值编码和文本编码混成一类。只有名称相同但缺少身份依据时，输出候选，不直接合并。
3. **合并原始观察。** 一个实体可有多个字段值，每个值绑定自己的来源、原始字段、证据、有效时间及业务口径。同一原始观察被两路抽到时，保留一份观察并追加执行出处；不同原始值不覆盖。来源表没有稳定行键时，记录“内容等价观察”，不要把内容摘要冒充物理行 ID，也不要据此推断出现频次。
4. **按属性规则生成展示值。** 所有观察作为保留层，展示和查询使用明确的派生结果。属性值分歧只影响相应属性，不否定已经确认的实体身份。涉及实体身份本身的矛盾，进入身份复核，处理范围与普通属性分开。
5. **重写关系端点并去重。** 保存 old_node_id → canonical_node_id 映射，重写所有相关边。按关系的完整事实键合并边及其引用，重新检查无环性、自环、端点与基数约束。对转换后出现的问题按依赖范围处理，保留原始快照和转换记录。

属性处理应有固定分类：

| 情况 | 固定处理方法 | 示例 |
| --- | --- | --- |
| 同属性、同值、不同来源 | 一个规范值关联多个来源观察 | 两表都记录产品类别“设备” |
| 一表有值、另一表缺失 | 保留有值观察；缺失不覆盖 | A 有责任部门，B 无此字段 |
| 多值属性的不同值 | 按集合合并，保留每个值的来源 | 服务地区、经确认的别名 |
| 单值属性在相同时间和业务口径下不同 | 保留冲突观察；按声明主源派生选值，或显示待判定 | 同一时点责任部门分别为甲、乙 |
| 属性随时间变化 | 分有效时间保存，按查询时点选值 | 2025 年归甲部门，2026 年归乙部门 |
| 同名字段但业务口径不同 | 拆成不同属性或带口径的事实 | 实际收入与预算收入、含税金额与不含税金额 |
| 数值单位或格式不同 | 有明确转换规则时转换并记录原值、单位和规则版本 | 元与万元；没有规则时不直接比较或求和 |
| 名称不同但实体已确认相同 | 保留 observed_names 及其来源，按稳定规则选展示名 | 正式名称、旧称或尚未解释的不同名称 |

时间规则使用业务有效时间，不能简单用“哪个 worker 最后执行”决定哪个值最新。求和、平均和投票也不是通用的属性合并方法；特别是两路抽取包含同一张表时，会导致重复计数。

属性值需要带上下文。例如同一产品 P001，在 A 表中是华东区域收入 100，在 B 表中是华南区域收入 200：产品可以合为一个；两项收入应分别保留区域、期间、币种和口径，作为事实节点或带限定条件的关系。不能只保存 region=[华东,华南]、income=[100,200]，否则会丢失“哪个金额对应哪个地区”。现有 supports 能保留部分行内关联，但只读取顶层属性数组的下游仍可能误用，统一查询接口必须使用这些带上下文的观察。

同名的物理字段节点也需单独处理。A 表的“收入”列与 B 表的“收入”列属于两个物理对象，应保留物理来源；若业务含义相同，可共同关联到一个业务概念节点。把两个物理列直接压成一个，会丢掉实际列身份。

建议增加融合诊断表，按候选组解释“为什么没有合并”：未声明共享身份、实体类型未对齐、键字段未对齐、键值类型不同、不同业务作用域、仅名称相同、真实身份矛盾。对已经合并的组说明参与节点、旧新 ID 映射、合并的原始观察数量和属性分歧。这样可以直接核对用户看到的重复节点，避免继续以节点总数猜原因。

验收除数量外还应检查：输入顺序交换结果不变；重复输入同一证据不增票、不增加原始观察；同一有效策略下分批合并与一次合并结果一致；属性分歧不造成实体重复；属性的地区/时间/单位对应关系不丢失；权限过滤后不泄漏其他 scope 的候选值。不同策略版本之间应重新计算派生结果，不复用旧的选值冒充新结果。

**2026-11-18 补充：共享知识、限定事实与局部冲突的实现**

本节记录后续已实现的代码行为。前文的运行耗时仍来自测试机反馈，未在本机复算；本节的验证使用固定本地数据与脚本化测试，不替代真实模型和业务验收。

实体身份、事实适用范围、来源引用分别保存：

| 声明 | 含义 | 行为 |
| --- | --- | --- |
| `identity_scope: global` | 有依据的共享对象身份 | 要求 `identity_domain` 和已签发的 `identity_evidence_refs`；同一身份域、同一类型契约及完整键可自动共享 |
| `identity_scope: scoped` | 项目等范围内的对象 | `identity_context_keys` 必须是完整 `key_names` 的子集；对应列同时进入映射键和关系端点键 |
| `business_context.status: global` | 此条事实已声明通用 | 可以参与指定范围的查询，仍需满足独立的访问授权 |
| `business_context.status: scoped` | 此条事实受明确条件限定 | `columns` 保存业务维度到物理列的映射，`values` 保存有依据的常量声明；逐条保存值、条件和引用 |
| `business_context.status: unknown` | 适用范围未确认 | 全图保留，指定项目或仅通用知识时不纳入确定结果 |
| `context_columns`、`row_context` | 来源行与路径依据 | 不自动参与实体身份，也不自动推断业务项目 |

例如，共享产品可以只有一个实体；M 项目金额 100 与 K 项目金额 200 仍是两条限定观察。项目内编码则必须使用 `(project, code)` 身份。A 表的项目与 C 表的项目角色不同，应使用不同维度名称，不能覆写为同一个属性。

证据引用校验保证引用来自已签发材料，不能自动证明业务身份声明正确。没有资料证明全局唯一时，应保持未知或项目内身份。不能根据同名、同码、表名、项目目录或模型置信度自动提升为通用知识。

合并与发布的默认处理：

- 同名但不兼容的定义生成稳定内部版本，保留 `original_type`、原始事实及各自关系端点；输出 `definition_conflicts`，不再因这一局部分歧终止整个合并。跨草稿发布采用相同处理。
- 已声明全局身份的名称分歧保留双方名称与引用；普通界面不再要求填写底层融合策略。历史显式严格/隔离策略保留兼容，不在升级时偷偷覆盖。
- 定义分裂导致继承关系端点无法确定时，原支持证据保存在 `unresolved_relations`，不作为已确认边参与遍历。非法输入、摘要损坏、不存在的端点仍报错。
- 关系 ID 包含明确业务适用范围。M 的 A→B 与 K 的 B→A 不构成同一范围的环。环检测有状态、工作量和内存上限；未完成会返回诊断，不宣称无环。同上下文真实环仍遵守关系约束与已有冲突策略。
- 工作流报告分别记录发布结果、覆盖状态、待对齐定义和未决关系数量。保留两个定义不等于它们已经对齐，也不等于丢失了原事实。

查询与查看使用同一业务范围合同：

```json
null
{}
{"project": "M"}
{"project": "M", "period": "Q1"}
```

以上依次表示全部内容、仅通用知识、通用知识加明确适用 M 的事实、通用知识加满足 M 与 Q1 的事实。查询选择是条件合取；事实需具有全部选定维度且值一致。没有选定的维度可以继续保留为各条观察的不同条件。业务范围与 `scope_ref` 访问授权独立，二者都必须满足。

画布、详情、完整批次和历史快照都在读取支持证据之后、遍历之前筛选。范围列表只从当前授权内容提取；分页固定快照。检索索引先按权限与业务范围分桶，词法、向量评分和拓扑扩展使用同一组选定切片。每条支持观察仅投影一次，避免项目数乘以全图扫描量。

检索返回逐条 `observations` 和分开的 `path_groups`。路径累积条件，不将 M-Q1 与 M-Q2 拼成同一路径；未知范围明确标记未确认。条件兼容仅表示这些观察可以在所列条件下共同成立，不证明原始资料曾给出整条路径，也不证明因果关系。连通分量只描述邻接并集。路径分析达到内部预算时返回截断说明。

已有索引格式升级为 3，需要重新执行一次向量化；旧图文件不重写。旧产物没有业务范围声明时保持未知，新算法不能从已压平的属性或已删数据恢复原身份粒度。要得到可靠的项目检索，必须基于保留的原始来源和正确映射重新验证、发布相关规则。缺少原始 ID 元数据的历史单位图，也不能承诺与从原贡献重放得到完全相同的 ID。

验收固定案例包含：项目内同码对象不合并；共享产品的 M/K 数值与引用不混用；同一 C 观察重复抽取保留执行来源；定义冲突后双方事实仍可查看；同一上下文真实环被识别、不同上下文假环不被隔离；输入顺序和分阶段合并 ID 稳定；权限撤销后历史范围列表同步缩小；跨上下文批次游标不能复用。真实 Windows 实验、真实模型语义判断及原生 Harness 测试仍需在对应环境验证。

继承图按名称或属性查找端点时，同节点、同权限下的不同观察也分别保留。关系的范围取主行与继承见证的共同条件：全局声明经过 M 限定见证后只能得到 M 限定关系；未知依据不会提升为全局。没有兼容见证且 `on_missing=error` 时，当前草稿仍验证失败并保留诊断，既有发布成果不变；显式 `omit` 继续按原合同计数并跳过。物理表查找仍要求映射明确完整匹配键，不猜测项目字段。

对于明确声明相同来源表、相同 `context_columns` 的观察，不同源行见证不能拼成一条桥接路径；普通事实邻接仍保留。任意跨表或自连接推理不自动视为完整来源路径。路径状态和见证输出均有全查询预算，超限明确返回 `path_budget_reached`；它不等于无路径。数值和适用条件完全相同、仅物理列名不同的事实在详情中归并为一条，引用仍分别保留。

本地验证记录：后端全量回归 3659 项通过、26 项跳过；跳过项需要原生 Harness 或显式环境开关。最后补充的详情引用归并及继承、检索专项也通过。前端 393 项测试通过，TypeScript 检查和生产构建通过。以上计数包含交叉覆盖，不应相加。未在 Windows 测试机重新运行真实模型实验。
