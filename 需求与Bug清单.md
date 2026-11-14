# ICT LightGraph 需求与问题（Bug / 瓶颈）清单

> **整理基准**：仓库基线 `f69403d`（纯净源码备份 `dist/ict-pure-source-20261112-f69403d.zip`，32 MiB；内含 **9,770 个受控源码文件**，与该提交 `git ls-files` 一致，包含目录总计 11,455 个归档条目；CRC 检查通过，仅 3 个 Windows `.cmd` 存在换行符差异，零运行缓存与凭据）。  
> **协同说明**：当前开发机无真实客户数据，真实运行计数与耗时轨迹均来自另一台测试机的 `test4` / `test5` 运行记录，并已逐条与基线 `f69403d` 源码交叉核验。

---

## 一、 真实运行现象与待验证的容量假设

在测试机上从单表（`test4`）扩展到双表（`test5`：保留 `income` 表，新增约 **740 万行 × 104 列** 的 `dma_fin_measure_track_ebg_order_adv_f` 宽表）后，**业务产出质量达到了可用基线**（例如成功产出 **3,120 条“指标采用维度”关系**，共 4,854 节点 / 10,759 边，且带有完整证据链）；但整体执行时间显著拉长：

| 对比指标 | `test4`（单 Gauss 表基线） | `test5`（双 Gauss 表） | 准确口径与核验结论 |
| :--- | :--- | :--- | :--- |
| 运行总时长 | ~131 分钟（~2.2 小时） | **34,436 秒（≈ 574 分钟 / 9.57 小时）** | 膨胀约 **4.4 倍**；**3,120 条指标-维度边**是后续优化必须守住的业务基线 |
| `db_queries` | — | **954 次** | 依据 `construction/runner.py:293`，该计数器口径为**数据操作尝试次数（`data_operation_attempts`）**，不等于 954 次全表扫描；另有独立计数器 `db_query_admissions` 含目录与事务控制 SQL |
| `model_calls` | — | **467 次** | 平均每轮间隔 ~74 秒 |
| 输入 Token 消耗 | — | **1,239 万 tokens** | 宽表 104 列元数据分页读取与长材料翻页推高消耗 |
| 产出规模 | 2,999 节点 / 2,627 边 | **4,854 节点 / 10,759 边** | 指标采用维度 **3,120 条**；发布规则 9 份，工作区字符达 573k |
| 收尾状态 | — | `run_completed_with_gaps` | 存在未补全目标缺口；`dropped_events = 737` |
| 模型调用间空窗 | — | 尾段 98 次调用 11,751s 中模型推理约 900s，**空窗 ~10,850s** | 空窗并非纯 SQL 执行，而是混合了**连接建立与预检、SQL 扫描、结果流传输、Python 构图物化与 YAML 写盘** |
| 超时与异常信号 | — | 12 段 >60s 大空窗（单段最长 29 分钟） | `worker.jsonl` 记录 **10 次 `query_cancel_unverified`** + **1 次 `sqlstate 29003`**（注：取消探针失败与业务 SQL 超时 `query_timeout` 触发路径不同，见 Defect 4） |

### 扩展到 24 张表的容量推算（待验证假设）
如果继续沿用现有单 Run 串行架构扩展到 **24 张表**：
- **耗时与成本外推（待验证假设）**：按 `test4 → test5` 的单表边际增量（+443 分钟/表）线性推算约 175 小时（7 天+），叠加共享维度重复探查和工作区文本膨胀，存在严重超线性风险；该数值属于基于双表样本的**待验证容量假设**，需先对各子阶段做精细计时后再行实测校准。
- **预算与配置约束**：
  - `max_run_seconds` 默认值为 `60000` 秒（约 16.7 小时，`src/lightgraph/runtime/config.py:176`），虽可通过配置调大，但单 Run 串行拉得过长在工程运维和断连恢复上极其脆弱；
  - `model.max_calls` 默认 `2000`（测试中配至 `3000`，`config.py:64`），24 张表大概率突破数千次调用；
  - *(注：核查源码确认 `max_reconciliation_entities=5000`（`config.py:173`）仅保留在 `RunConfig` 及 Schema 定义中，当前 Harness `construction/` 构建主路径并未消费该字段，不构成当前构建阶段的实体截断限制。)*

---

## 二、 源码核验出的 Bug 与系统级缺陷

### Defect 1：单项目内强制串行，缺少并行工作流与单单元容错
- **源码位置**：`src/lightgraph/web/execution.py:270-275`（`RunManager.start`）。
- **缺陷表现**：目前只有“不同项目之间”能并行，同一项目内通过 `if self.active(pid): raise LightGraphError("project_run_active")` 强制单进程串行。无法在同一个项目内配置多个并行工作节点联合输入（如按表/星型单元拆分 6 路或 24 路并发），也无法实现某一张表失败后仅重跑该分支而不影响其他表已完结成果。

### Defect 2：缺少跨运行/跨单元图谱融合引擎，且存在多处耦合阻塞
- **源码位置**：`src/lightgraph/presentation/importer.py:10-44`、`src/lightgraph/construction/workspace.py:185-188, 482-578`（`issue_evidence` / `_merge_graphs`）。
- **缺陷表现**：
  1. `import_graph()` 仅支持把旧式单份 JSON 快照写入空 SQLite 库，来源强制标为 `user_input`，不具备多快照融合能力。
  2. `_merge_graphs()` 仅用于同一单工作区内草稿合并，扩展至多单元/跨项目融合时存在四处硬阻塞与隐患：
     - **权限指纹强一致校验**：`workspace.py:497` 要求所有图 `access_fingerprint` 完全相等，否则直接抛 `published_graph_access_conflict`。
     - **证据编号全局冲突**：`workspace.py:185-188` 每个工作区独立从 `e1, e2, ...` 顺序签发证据编号；多单元/多项目融合时不同工作区的 `e1` 会发生碰撞，必须限定到源工作区命名空间才能保真追溯。
     - **错误合并/错误阻断风险**：`workspace.py:544-552` 在严格模式下遇到同码轻微命名差异直接抛 `published_graph_identity_conflict` 阻断全图；反之，若在缺少共享身份域（`identity_domain`）与口径验证的情况下仅凭“同码或同名”自动硬合并不同表的实体或列定义（如两张表的“年累计”字段），则会把不同口径的业务概念错误连到一起。
     - **大 YAML 单文件解析瓶颈**：`workspace.py:159` 读取限制 128 MiB，前端 `web/graph_batches.py:22-24` 与 `web/construction_graph.py:76` 每次冷加载仍全量解析整份发布 YAML 并进行权限重建。

### Defect 3：无法基于“已有项目知识图谱”继承扩展
- **源码位置**：`src/lightgraph/web/workspace.py:314, 494`（强制覆写 `isolation_domain = project_id`）、`src/lightgraph/construction/models.py:189-214`（`GraphDraft.coherent_definitions`）、`src/lightgraph/construction/executor.py:570-577`。
- **缺陷表现**：
  1. **项目隔离域强制锁死**：`web/workspace.py:494` 保存项目配置时强制将 `isolation_domain` 设为当前 `project_id`，因此任何依赖“同 `isolation_domain`”的跨项目直接读取方案都会失效；必须改用显式的跨项目发布授权与版本绑定。
  2. **草稿类型闭环检查**：`models.py:191-194` 强制要求关系端点的 `source_type` 和 `target_type` 必须都定义在当前草稿 `entities` 中，不允许增量草稿直接连向已继承基础图谱的实体类型。
  3. **执行器端点强制存在检查**：`executor.py:570-577` 校验边时强制要求两端点都在本次提取的 `nodes` 中（否则报 `unresolved_relation_endpoint`），且 `EndpointLookup` 只能查询物理数据源，导致新项目不重连、不重扫旧表就无法建出连接旧图谱实体的边。

### Defect 4：Gauss 每次重连重复全检、缺乏阶段耗时剖析及重验开销大
> **不可退让的安全与防断连前提**：测试机 Gauss 存在真实的网络掉线与半开连接问题，`gauss.py:506-547` 与 `reconnect.py:166-233` 的“思考期释放物理连接 + 每次投影独立短只读事务（防 `25P02` 异常事务连坐）+ 最多 3 次退避重建”是现场摸索出的保命底线，任何提速都不能倒退回长连接或跨查询共享事务，也不得违反 [US-04 数据不落盘约束](design/lightweight_graph/US.md#L48)。

在此前提下，核查源码定位到以下真实效率问题：
1. **缺少子阶段耗时剖析指标**：现有运行报告仅给出总 `elapsed_seconds` 与 `db_queries` 次数，无法精确区分一次构建中**建连预检、目录拉取、前置统计、数据投影、流传输、Python 构图、YAML 写盘**各占多少秒。
2. **常规重连重复全表目录查询与 `pg_sleep` 探针**：
   - 位置：`src/lightgraph/sources/reconnect.py:189` 与 `src/lightgraph/sources/gauss.py:242-275, 277-339, 341-421`。
   - 表现：每次快照重开都要重建 `GaussSource`，不仅按表循环重新查 `information_schema` 与 `pg_catalog`（每张表 3~4 条 SQL），而且当 `actual_database_verified=True` 时，每次重连都会重复执行 `_verify_runtime_controls_current()`——包含 **2 次 `pg_catalog.pg_sleep(0.25)` + 2 次 `SELECT 1`**。
3. **两个不同超时/取消路径容易混淆**：
   - `query_timeout`：业务 SQL 超过 `limits.timeout_ms`（默认 60s）被定时器取消；
   - `query_cancel_unverified`：新建连接预检阶段跑 `_probe_runtime_control(cancel=True)`（0.05s 后调用 `cancel()` 等待 0.25s `pg_sleep` 返回 `57014`）失败或超时。调大业务查询超时并不能自动消灭重连时 `query_cancel_unverified` 探针自身的失败路径。
4. **每个投影先计数再拉取 + 修稿全量重验**：
   - 位置：`src/lightgraph/construction/executor.py:293-325`。
   - 表现：每个未命中单次调用内缓存的投影都要执行一次 `profile_column_group` 全表聚合 + 一次 `SELECT DISTINCT`；并且单次校验结束即清空内存缓存，Agent 修改草稿局部内容再次调用 `validate_graph_draft` 时会重新验证全草稿所有大表映射。**注意：按 US-04 约束，绝不可通过把原始投影行落盘做活库缓存来“偷懒”，否则既违规又会在源库新增数据时静默漏边。**

### Defect 5：节点数据模型标准化不足，前端未直观按物理表组织属性与来源
- **源码位置**：`src/lightgraph/construction/models.py:30-44`、`src/lightgraph/web/construction_graph.py:207-304`、`src/lightgraph/web/graph_payload.py:4-38`、`src/workbench/src/graphLabels.ts:111-116`、`src/workbench/src/GraphCanvas.vue:498-544`、`src/workbench/src/TechnicalDetails.vue:10`。
- **缺陷表现**：
  1. **缺少标准中英文名与节点描述角色**：`EntityMapping` 仅提供 `label_column`、`alias_columns`、`display_columns` 和 `properties`，没有独立的一等字段 `english_name_column` 和 `description_column`；英文名被混在 `alias_columns` 里，无法在历史或新数据中严格区分“已知英文名”与“未提供/未知”。
  2. **目录注释与节点业务定义混淆风险**：表注释、列注释（物理元数据）与实体/指标自身的业务说明混杂，缺少明确的来源归属标识。
  3. **前端详情页无法一目了然回答“来自哪张表哪一列”**：虽然底部的 `<TechnicalDetails>` 原始 JSON 本身已经是 `<details>` 默认折叠，但主展示区仍通过 `nodeProperties()` 反向收集散落字段，且把同一节点跨多张物理表贡献的编码、中文名、英文名、动态属性埋在逐条展开的 `facts -> supports` 嵌套列表中，缺乏按物理表归并的标准化条目视图。（同时必须注意：不能将沉重的明细支持直接塞回 `graph/batches` 画布全图载荷，以免拖垮大图首屏渲染。）

---

## 三、 修订后的核心业务与架构需求清单

| 编号 | 需求模块 | 具体要求与验收边界 |
| :--- | :--- | :--- |
| **REQ-01** | **跨项目图谱继承与增量并行扩展融合** | 1. 新项目可通过**显式跨项目授权引用**继承已有项目的已发布图谱 $G_{\text{base}}$（固定其发布版本摘要与可继承证据范围，**不重连、不重扫**旧图物理表，不受项目各自 `isolation_domain` 不同的阻断）。<br>2. 在 $G_{\text{base}}$ 上支持项目内并发运行多个扩展分支（先以 2 单元打通闭环，再扩至 6 路 / 24 表），并支持基于确定性融合引擎产出终态图谱 $G_{\text{final}}$。<br>3. 融合必须保留作用域完整的证据溯源（如 `unit_id:e1`），**严禁仅凭同码/同名自动硬合并实体或自动连通同名度量列**。 |
| **REQ-02** | **项目级父运行编排与独立单元重试** | 在同一项目内支持“项目级父运行 + 独立子单元（Unit）”架构：各单元独立预算与结果目录；支持单单元失败后独立重跑，并在融合时精确替换该单元上一次的贡献，不破坏其余已完成单元的边与证据。 |
| **REQ-03** | **可观测计时 + 兼容防断连模型的 Gauss 提速** | 1. 保持 Gauss 短快照、独立只读事务（防 `25P02`）与有界重建自愈；严禁把原始投影行数据落盘缓存（严守 US-04）。<br>2. 优先落地**分阶段精细计时埋点**，实测 `test5` 各阶段真实耗时后再逐项开启优化（目录 SQL 批量合并精确比对、分草稿局部验证/合规复用、带自动回退的宽投影合并等），且**每轮优化必须用 `test5` 的 3,120 条指标-维度边及来源证据做精确对账**。 |
| **REQ-04** | **本地向量库 + 本地关系库子图召回（权限与路径优先）** | 1. 第一版即以**本地 SQLite 邻接表 + 本地向量库**搭建简单可控的召回链路，不引入多模型重排打分。<br>2. 向量索引严格仅从已授权名称、别名与邻域构建；支持长 Query 多实体并行召回、轻量融合排序、受限 1~2 跳子图提取及公共节点展示；**严格返回实际边、方向和证据，绝不伪造“收入”与“订单”的直接业务关系，允许无连接多分量输出**。 |
| **REQ-05** | **标准化节点契约与条目化详情视图** | 1. 画布全图传输继续保持轻量摘要不拖慢首屏；点击节点加载 `/graph/details` 时输出标准化结构（中文名、独立英文名/未知标记、明确来源的节点描述、关联物理列元数据类型的动态属性清单、按物理表分组的来源证据条目）。<br>2. 严格区分物理目录注释与业务定义说明。 |
| **REQ-06** | **纯净源码备份留档** | 已完成并核验 `dist/ict-pure-source-20261112-f69403d.zip`（9,770 个受控源码文件，CRC 无误）。 |
