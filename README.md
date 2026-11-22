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

## 并行工作流和按需向量化

在「项目配置 → 并行工作流」启用工作流。把两个抽取节点拖到画布，分别选择 A+C、B+C 及各自的知识文档。保存后运行，两个节点按并发数独立抽取。只有抽取节点时，各分支直接输出。新建合并节点默认只归并已提交的上游图，不再调用模型或读取源表。需要发现上游尚未抽出的关系时，再选择「合并后用模型补充跨分支关系」，说明要补哪类关系。已有工作流保留原来的执行方式。

编排画布支持缩放按钮、Ctrl/⌘ + 滚轮缩放、拖动空白处平移和“适应画布”。侧栏按可用宽度调整位置。工作流单元只加载能绑定到当前表目录的知识文档；项目级运行仍严格检查文档绑定。上游未完成或发布记录无法读取时，下游合并跳过，并保留具体错误码。

**重复节点如何合并。** A+C 和 B+C 都抽到 C 表的一条指标记录时，系统核对来源表、物理键列、完整键值和适用范围。类型名不同还要核对它们在关系里承担的角色。证据一致的节点会自动合并，关系连到合并后的节点。节点详情保留各来源观察到的名称、属性值和引用。工作台的「已归并」统计当前可见范围内有重复来源投影归并证据的节点数。

如果 M 项目和 K 项目分别定义了同名指标，项目范围会参与判断。两条记录只有名称或编码尾号相同时，系统保留各自的实体和引用。查某个项目时，在检索页选中项目范围，通用知识和该项目的观察可以一同召回。

在「运行与结果 → 运行记录」可切换报告，刷新后仍可查看所选记录。删除已结束的运行会移除该记录和直属日志。并行节点的执行文件、检查点和已发布图谱单独保留。

**查看历史。** 打开「运行与结果 → 业务图谱 → 版本与范围 → 查看版本」。可以在当前项目输出、以往的完整图和节点增量间切换；“合并基础图”是补充抽取开始前已经归并的上游结果。选择版本不会改掉当前输出。搬迁旧项目后，如旧图提示权限指纹不符，可选中该版本，输入搬迁前存放 `project.yaml` 的目录绝对路径进行核验。原权限无法核实、文件损坏或已丢失时，系统不能仅凭运行报告中的节点数重建图谱。

**恢复合并。** 在「运行与结果」选择原运行，在目标节点点「从此节点重新运行」。系统新建一条运行，核验未重跑节点的输入、授权与产物，重跑目标和下游。若只需把已提交的唯一终端完整图发布到项目，点「仅发布此结果」。节点增量不能直接覆盖项目图。已经按隔离策略发布的干净子集可以出现在项目图中，覆盖缺口仍记为 `partial`；源数据变化或产物校验失败会保留旧项目图。旧运行若没有检查点，只能在仍有对应状态、原输入指纹和已提交抽取结果可核验时恢复。

合并节点已经算出节点数和关系数时，检查点仍可能在写入。提交失败后，可在原运行报告的「工作流节点执行与合并结果」中查看失败阶段、尝试次数及可用的系统错误号。若两路抽取的检查点均可核验，在原运行选择合并节点重跑即可复用两路输入。

确定性合并记录和发布摘要 `construction/workflow-merge-summary.yaml` 会同时给出身份候选组数、涉及的不同节点数（`candidate_identity_nodes`），以及出现在多组中的节点数（`candidate_identity_nodes_in_multiple_groups`）。一个对象可以同时出现在同名组和同码组。实际能归并多少个节点，取决于各组的身份依据。

运行记录中，合并节点的「说明」显示上游输入、输出和同源去重数量。有跨表节点按字典成员合并时，另列这一步消除的副本数。按字典合并要求对象类型和角色相容，各来源引用同一个完整的字典唯一键，并在适用范围一致的观察中找到对应成员。

「同源层级归并」显示不同抽取单元对同一张层级表重复建模时消除的副本数。系统核对每个成员的完整键、来源名称列、适用范围及有方向的邻接关系。归并后，每条关系保留方向和来源，节点详情可查原节点 ID、类型、键和身份域。要用旧检查点验证本次改动，运行下方审计命令，比较 `committed` 与 `with_source_replay`，查看 `hierarchy_replay_collapsed_nodes`、`hierarchy_replay_diagnostics` 和 `integrity`。只读重放不会发布新图。

查看 `hierarchy_replay_diagnostics.physical_set_outcomes` 可以区分全部归并、部分归并和全部保留。例如，`input_nodes=5, output_nodes=2, sets=3` 表示有三组各从五份归为两份。这项统计只覆盖有跨单元物理键证据的集合。同一成员出现多个来源名称时，保留该组的副本。相邻成员仍可用各自的键、方向、路径和范围独立核验。

一个集合可能有五份投影，其中两份的节点观测一致，其余三份有差异。`sets_by_observation_difference` 按集合统计字段差异。`equal_node_pairs_by_result` 只统计通过集合身份核查、节点本身观测一致的跨单元成员对，记录这些成员对在关系、方向或祖先路径核查中的去向。`passed_before_endpoint_remap` 表示已通过这些核查，最终归并数量以 `physical_set_outcomes` 为准。

层级成员在两份投影中都没有非自环边时，系统还会核查生成它的草稿版本、映射和物理键列。如果每个位置都能在各自的同一映射、同一适用条件下找到另一对成员，并且几份投影共同记录了这对成员之间的同向关系，就可以用这些观测确认位置的角色。候选成员仍须通过自身身份、属性、范围和已记录祖先路径的核查。其他成员的关系只用来核对角色，不会添加到候选成员上。

`isolated_members_by_role_result` 按原节点 ID 统计实际进入这一步的成员，每个节点只计一次。可以据此区分缺少草稿版本、物理层级依据不足、某个位置缺少映射样本，以及几份映射没有共同样本。`passed_role_check` 表示已有角色依据，实际消除数还取决于祖先路径和关系端点检查。

审计中的 `collapsed_nodes_by_role_evidence` 给出按直接邻居和按上述映射证据实际消除的副本数。`different_neighborhood_pairs_by_first_difference` 将邻居不同的跨单元成员对按首个差异分列，便于区分邻居编码、方向、端点列或边的适用条件。需要逐个比较两版归并结果时，在下方命令末尾加 `--include-hierarchy-membership` 并保存输出。`with_source_replay.hierarchy_membership.groups_by_output_id` 列出每个归并节点及其在关闭来源归并时对应的原节点 ID。

**源表没有声明主键或外键时怎么核查。** 先取原运行的抽取检查点，找两个单元分别从同一张表或工作表投影的对象，核对完整键列、带类型的键值、角色和适用范围。这条同源去重路径也适用于没有数据库约束的表。对于不同事实表之间的同码对象，再查两侧可见的字段文档，看能否指到同一套编码、同一层级及其适用范围。核查时记下具体表列和文档段落。若只有相同列名、相同编码或取值重合，保留各自的节点，记录待确认的口径。旧检查点只能重放当时保留下来的证据，后来找到的文档依据要与历史合并结果分别记录。

在下面的审计命令末尾加上 `--observations-only`，可以只读取已提交的合并图。输出中的 `typed_row_identity` 用来核对各单元同类型、同完整身份键的对象。`physical_key_overlap` 按来源表、物理键列顺序和带类型的原值查找重合，即使两个单元用了不同的类型名也会统计。`shared_keys_on_separate_nodes` 统计同一物理键出现在多个单元且仍挂在不同节点上的组数。一个节点可以出现在多个组中，项目、层级或对象角色也可能不同。判断能消除几个副本时，需要逐组核实这些条件。

对于直接合并抽取单元的确定性节点，可用 `python scripts/audit_checkpoint_merge.py "<项目目录>/runs/<原运行ID>.json" --node merge_1` 核查。`committed` 来自原检查点。`without_source_replay` 和 `with_source_replay` 使用同一批检查点、原运行的策略和当前 Python 环境的代码，分别关闭和开启基于来源证据的自动合并。`source_replay_collapsed_nodes` 计入同源重复投影消除的副本，`observed_dictionary_collapsed_nodes` 计入随后按字典成员额外消除的副本。旧检查点可使用其中已保存的证据。脚本还会分别统计行实体、物理列节点、未决关系和缺少端点的已发布边。核查期间只读项目文件，无需数据库和模型。

对于旧版「两个抽取 → 一个合并抽取」运行，如果已经隔离名称冲突并提交增量，却没生成项目输出，可在原运行的合并节点点「从原合并产物恢复发布」。系统核对原抽取结果、上游图、冻结记录、节点报告和已提交增量，再按当时的策略重新组合发布，不读源库也不调用模型。此操作限直接连接抽取节点、无项目级继承的唯一终端合并节点。已隔离的实体不会凭空恢复；如要改变隔离结论，需用保留的证据重新验证。

实例图谱的验证预览、发布、合并和继承快照使用不可变目录，内含 `nodes.parquet`、`edges.parquet` 和 `metadata.yaml`。Parquet 按批次压缩写入，保留身份、属性、来源、范围和证据；全部写完后才更新发布指针。旧 YAML 和证据分片仍可读取，内部旧 YAML 上限为 512 MiB；Agent 提交的映射 YAML 仍限制为 8 MiB。升级时安装新的 PyArrow 依赖并重启服务。历史文件不会自动改写。

Parquet 的 `id`、`type`、`source`、`target`、`label` 可直接用 DuckDB 查询，`payload` 保留完整行 JSON。例如 `SELECT type, count(*) FROM read_parquet('路径/nodes.parquet') GROUP BY type`。这次改动解决实例记录受 YAML 大小上限阻断的问题；合并和授权过滤仍会在内存中构造完整图谱，并未改成按需数据库查询，也未改变维度成员的建模规则。可运行 `.venv/bin/python scripts/benchmark_graph_storage.py --size-mib 140` 检查超旧上限的合成图谱发布与合并；该检查不调用模型或真实业务数据库。

新建工作台项目默认使用本地 Qwen 向量服务。在安装项目的 Python 环境中启动服务：

```bash
python -m pip install -e '.[embedding]'
lightgraph embed-server --model Qwen/Qwen3-Embedding-0.6B --port 8002
```

已有离线权重时，`--model` 可填本地模型目录，再加 `--served-model-name Qwen/Qwen3-Embedding-0.6B` 保持相同的 API 模型名。在「模型配置 → 向量嵌入服务」可测试连接，或更换兼容服务的地址和模型名。

图谱发布后可直接浏览。需要语义检索时，在「运行与结果 → 业务图谱」点击「立即向量化」。任务在后台执行。图谱页和子图召回页显示准备数据、生成向量和写入索引的进度；生成阶段按实际完成的去重文本数计数。完成后在「子图召回」搜索。图谱、可见范围或向量模型变化后，需再次手动向量化。

工作台和查询接口都可以直接输入问题或一段文字，单次最多 4,000 个字符。例如，可以问两部手稿如何通过其他作品发生联系。

```json
{"query":"从山行札记到天象录有哪些关联路径？"}
```

系统先识别原文提到的对象和限制，再到用户有权查看的图中核对。问句没有限定项目、版本或其他范围时，系统沿用当前授权视图。节点和关系都可以用 `applicability` 声明适用范围，例如按版本区分同一部作品的引用。已有映射的 `business_context` 写法仍可使用。解析问句时，对话模型只收到问题和待核对的原文片段。没有可用的对话模型时，仍可按名称、别名、编码和简短的查询指令检索，复杂条件可能需要确认。

自然语言检索结果包含 `agent_context`。展开「查看 Agent 上下文」可核对本次使用的节点、关系方向、适用范围、来源表列、待确认条件和截断标记。关联路径与共同事实记录分别列出，方便下游 Agent 判断每段证据能支持什么。

在「子图召回」输入问题，点击「召回并试写 SQL」，工作台会把同一次召回的上下文交给独立的 DSH 会话。接口为 `POST /api/projects/{pid}/retrieval/query?sql_preview=true`，请求体使用原有的 `{"query":"原始问题"}`。DSH 使用项目抽取模型，工具列表为空。工作台只在本地检查 SQL 语法和召回到的 Gauss/MySQL 表列，返回草稿，不连接业务数据库。缺少物理表列或存在影响 SQL 的未决口径时，可返回缺失依据。普通检索不会触发 SQL 试写。

“这两部作品怎样联系”会查找有逐条来源依据的多跳路径，一条路径可以经过多条记录。问“哪些作品在同一份目录中同时出现”则需要一条共同记录或可核验的关联键。问“沿哪些层级找到子项”使用关系定义声明的父子方向。这三类问题所需的证据不同。HTTP 接口也接受 `mode: "traverse"` 显式查询关联路径，可指定起点、终点、方向或关系类型。

例如用户问“空气压缩机”，图上写的是“空压机”。系统用向量在可见节点的名称和描述中查找相关项。如果模型仅凭问题提出的同义名称能唯一对应到同一节点，而且原词的向量检索也把它排在首位，该节点可以参与联合查询。单有相似度时，界面将它列为待确认的候选。如果同一条检修记录也支持问题里的车间、故障和这台候选设备，界面可以单列一条候选路径，注明“空气压缩机指空压机”这一假设。自动下钻读取关系定义中声明的父子方向。

有些内容只出现在资料中。例如“晶罅”没有单独建成故障节点，但某条现场记录写了“外壳出现晶罅”。检索可以返回这段原文及其来源。只有一部分条件能核实时，结果会另列已定位的节点、共同记录和仍待核实的条件。原文检索覆盖已同步的节点描述和文本属性，并按访问权限和项目范围筛选。升级现有索引后点击「立即向量化」即可纳入文本属性和新声明的层级方向，无需重新抽取。

全图概览使用离线计算的社区和中心度。工作台在项目图发布后排队计算异步标签传播、有向 PageRank 及度中心度。每个结果绑定图谱版本和可见范围。切换到 M 项目时，如果还没有该范围的结果，可在业务图的离线分析面板计算。CLI 构建结束后也会计算，已有图可用 `lightgraph analyze-graph config.yaml` 补算。关系类型和逐条来源证据仍可在原图查看。

名称和描述可以各自按关键词、向量或两者混合检索。描述来自已映射的来源列。若只有部分节点有描述，按描述搜索只会命中这部分节点。升级旧索引后需重新向量化一次。索引就绪后，按关键词或节点 ID 查询时无需访问嵌入服务。

```bash
lightgraph retrieve-subgraph config.yaml '销售收入' --field description --method hybrid --business-context '{"project":"M"}' --hops 0
lightgraph retrieve-subgraph config.yaml --node-id NODE_ID --hops 1
lightgraph retrieve-subgraph config.yaml --mode drilldown --seed-id NODE_ID --relation-type HAS_CHILD --business-context '{"project":"M"}'
```

将 `NODE_ID` 和 `HAS_CHILD` 换成图中实际的节点 ID 和关系类型。第二条命令展开一跳邻域，省略 `--hops` 时只取指定节点。第三条沿指定关系下钻，返回一条路径和停止原因。M 项目的检索范围包括允许访问的 M 项目事实和通用知识。HTTP 接口为 `POST /api/projects/{pid}/retrieval/query`，例如 `{"query":"销售收入","field":"description","method":"hybrid","business_context":{"project":"M"},"hops":0}`。

## 输入与配置

数据库接入窗口支持按 YAML 精确选表。在「用 YAML 替换已选表」中上传以下格式的清单。

```yaml
tables:
  - public.metrics
  - public.metrics_archive
```

直接连接时，系统在当前 Schema 内逐个核对完整名称，全部匹配后才替换选择。环境变量连接请填写 `schema.table`，保存后读取元数据时核实。

支持 Excel、CSV、Gauss/MySQL 适配器与 Markdown 文档。数据库实例读取仍受授权范围、只读查询和部署配置约束；连接器存在不代表客户数据库已验收。保留既有来源连接、表列权限、`scopes` 和 `document_scopes`。行支持的可见范围由程序配置 `scope_bindings` 绑定到物理列；无绑定时贡献标记为 `public`，不会根据对象键或列名猜测权限。前端先按范围过滤支持，再重建名称、属性和关系；业务 namespace 本身不授予访问权。`metadata_only` 只允许元数据和知识材料，不发布实例图谱。关闭模型时仅读取目录，不自动猜测对象或关系。

数据库配置页直接提供“允许读取事实数据”：新连接默认不勾选，仅允许读取元数据；勾选后，Agent 可只读查询所选表和列。运行时自动检查当前连接的只读事务、超时和取消能力，无需手工生成或上传能力 YAML。检查未通过时禁止数据查询。保存连接仍只读取元数据，选项从下一次运行生效；全局模型选择“仅元数据”时仍禁止事实读取。查询结果和运行轨迹沿用项目留存设置，关闭该选项不删除既有产物。

已保存的连接配置和元数据预览不随运行目录切换而清空。刷新页面只读取本地声明缓存；显式刷新数据库失败时仍显示上次成功的表结构和 YAML，并提示尚未刷新。来源授权、凭据修订或输入文件变化后，失效声明不会自动恢复。浏览器凭据会话暂时不可用也不会隐藏已加载的项目配置。

查询次数支持在模型配置页选择“不限次数”，对应 `max_db_queries: null`；正整数设置总次数上限。次数包含所有来源的元数据、事务控制及数据查询，报告分别记录预算准入次数与数据操作次数。次数不限不取消只读、授权、单次超时、运行时限、返回量和内存限制。额度耗尽后停止源查询，允许有限收尾，已经验证的成果保留。

模型配置页还可直接设置累计返回行数、累计返回数据量（MiB）和构图工作内存（MiB）。新建工作台项目默认使用环境内模型，可使用受限样本。累计返回行数和数据量均不限，构图工作内存为 2048 MiB。填写 1024 MiB 表示 1 GiB。累计量包含整轮所有来源的探查和验证，分批执行或释放内存不会清零；报告显示已用量和生效限额。设置保存后在下一次运行生效，详见[返回量与验证边界](design/lightweight_graph/SOURCE_RETURN_BUDGET_20261110.md)。

构建结果可选“生成实例图谱”或“仅生成结构与映射”（`construction_target: structure`）。结构检查不查询事实数据，也不证明实例关系或业务语义成立。结构方案可用 `lightgraph export-structure CONFIG OUTPUT.yaml` 单独导出。实例模式按映射的必要列和明确筛选范围生成节点及关系；跨表、跨来源和同表其他行的端点支持等值查找，必须实际找到目标记录。

业务图谱可按当前版本和业务范围下载。Semantica JSON 用 `entities` 表示实体（`id`、`name`、`type`），用 `relationships` 表示关系（`id`、`source`、`target`、`type`）。两者都有 `properties`，对应的逐条来源、属性值和适用范围保存在 `metadata.supports`。 `metadata.type_family` 给出类型的原名，`metadata.applicability` 汇总当前权限下可见的适用范围。实体名称会去除首尾空白；身份编码保留原值，原始名称可在对应的来源记录中查到。GraphML 使用有向边及独立边 ID，复合属性和元数据保存在 `properties`、`metadata` 两个 JSON 字符串中。可用 NetworkX 的 `read_graphml(path, force_multigraph=True, edge_key_type=str)` 保留同一对节点之间的多条关系。带空格等特殊字符的 ID 在 GraphML 中使用安全编码，原值见 `original_id` 的 JSON 字符串。

```bash
lightgraph export-graph CONFIG graph.json --format semantica-json --business-context '{"project":"M"}'
lightgraph export-graph CONFIG graph.graphml --format graphml
```

下载 API 为 `GET /api/projects/{pid}/graph/export`。`format` 可选 `semantica-json` 或 `graphml`，`version` 可指定历史版本，`business_context` 接收 JSON 对象。省略范围时导出当前权限可见的全部业务事实。

每次验证保存独立编号与读取时间窗口，发布固定的已验证产物，不再重查整个活库。失败重验不会覆盖成功快照；后续数据检查发现矛盾时，选择历史成功版本必须显式指定。主名称、别名和补充展示分别映射，名称遗漏会返回可操作提示。完整规则和边界见[架构修订](design/lightweight_graph/RELATION_MAPPING_AND_PUBLICATION_20261104.md)。

业务图页的“查看结构方案”可打开当前或历史修订的类型图与字段绑定，不要求先生成实例。数据库执行失败与映射数据冲突分别诊断；独立来源可继续生成候选，含失败依赖的草稿仍需修复后验证。修改方案时会提示删除的来源、名称角色和路径上下文，见[混合来源故障与能力边界](design/lightweight_graph/MIXED_SOURCE_FAILURES_20261105.md)。

事实来源断连时，Agent 可从已保存的混合草稿另存度量、维度等列定义草稿，单独验证并发布。已有列定义使用本次授权目录，无需读取经营数值；事实成员及其关联仍留在主草稿中等待验证。目录变化或权限失效不会被当作可离线复用的网络故障，见[快照恢复与独立列定义发布](design/lightweight_graph/RECOVERY_AND_DEFINITIONS_20261110.md)。

已授权来源换新会话时，参数绑定、DISTINCT、只读、超时或取消能力的预检失败，可丢弃会话并在三次恢复额度内重新检查。启动时打开 Gauss/MySQL 来源也按相同分类处理瞬态连接与预检失败，最多尝试三次完整连接，两次等待分别为 0.25 秒和 0.5 秒，共用本轮预算。认证、权限、配置和目录错误直接按原有错误语义处理。Gauss/MySQL 取消探针使用 1 秒等待和 5 秒服务端超时保护，必须观察到实际中断且回滚后仍可查询才算通过。进程诊断保留原始安全错误码、检查项目、恢复决定及次数。同次验证中相同的完整事实投影仍可复用，恢复后需验证并发布主草稿才能补上关联边。详见[抽取期恢复与诊断修订](design/lightweight_graph/RECOVERY_DIAGNOSTICS_20261111.md)及[关联取数复用](design/lightweight_graph/PREFLIGHT_AND_RELATION_REUSE_20261110.md)。

字段本身也可按目标建为定义对象，例如从元数据或文档中的字段说明建立度量定义。`metadata_column` 使用来源、Schema、表、列作为身份，不查询金额值；Agent 必须引用已读取且适用的材料。它可与实际行中的指标关联，分别记录“声明适用”或“观察到非空值”的依据，不自动连接全图指标。空维度编码可用显式 `is_not_null` 筛选；列定义、谓词和跨源内存边界见[本轮设计](design/lightweight_graph/METADATA_OBJECTS_AND_RELATIONS_20261105.md)。

圆形画布分批加载全部已授权节点和关系，显示加载进度并支持暂停、继续；分批传输不再把跨页边丢掉。数据库确认断链后可在新只读快照前有界重建并重新检查目录和能力，不重放已读取的部分结果。运行轨迹下的“查看进程诊断”提供安全的驱动状态与退出记录，可独立导出。探查顺序、恢复及性能边界见[后续设计修订](design/lightweight_graph/DISCOVERY_RECOVERY_AND_FULL_GRAPH_20261105.md)。

画布批次同时按条数和约 1 MiB 的节点/边内容分页；超大属性先给明确标记的概览，选择节点后读取详情。临时网络失败在同一游标重试，仍失败则保留已加载部分，可点击“重试加载”续传；发布版本或权限改变时要求重新加载。初次读取仍需还原已发布图谱并完成授权过滤，分批传输并不代表后端已支持任意规模图谱。

实例验证中，同一来源的多个投影可复用已校验的物理连接。每个投影单独开启并结束只读事务，验证退出即释放连接；重新建连仍需通过资格检查。恢复失败会锁定该来源，其他来源的独立工作可以继续。反复调用已锁定来源会触发有界收尾，避免几十次无效校验；已有草稿和候选保留。具体边界见[连接生命周期设计](design/lightweight_graph/SNAPSHOT_CONNECTION_LIFECYCLE_20261105.md)。

同一 worker 修订草稿时，CSV 和 Excel 可以复用文件摘要、字段、过滤条件和读取上限均未变化的完整投影。再次验证同一修订默认重新读取，显式指定 `force_refresh` 也会重读。数据库投影不会跨验证复用。比较两次运行时，可看报告的 `physical_connections_opened`（MySQL 含控制连接）、`immutable_projection_hits`（文件投影命中次数）、`db_query_admissions` 和 `phase_timings`，同时核对发布的节点、边和覆盖缺口。

构图启动时，数据库的连接或可隔离预检故障也会保留为失败来源，健康来源可继续工作；依赖失败来源的草稿不能发布，整轮报告保留缺口。凭据、授权配置及全局预算错误仍按原约束退出。跨源引用检查与实例匹配保留驱动值类型，不将 Decimal、日期等值与同形文本自动合并；新类型语义下的发布需要重新验证旧草稿，历史产物保留。修复及兼容边界见[2026-11-06 修订](design/lightweight_graph/P1_CORRECTNESS_REPAIRS_20261106.md)。

抽取目标与知识材料均支持 Markdown。目标在页面直接编辑，随项目配置修订保存，运行时写入 `construction/goal.md`。知识文档按需读取，初始上下文只包含目标与材料目录。

在「项目配置 → 知识文档」上传普通 Markdown，默认用途为「数据关联」。例如，上传 `orders.md` 时，项目里只有一张名为 `orders` 的表就会自动绑定。多个来源或 Schema 下有同名表、或项目中尚无匹配表时，文档显示「待分配」，暂不参与抽取。可直接在卡片上改用途、选择一张或多张表；通用于整个项目的材料可以显式设为全局。

批量配置时选择「导入 YAML」。`path` 可用项目中已上传文档的唯一文件名或登记路径。新文档可放在一个目录里，按清单写相对路径，在浏览器中选择该根目录。下面将一份说明同时关联到两张表，另一份分析知识供整个项目使用。将示例的 `来源.Schema.表名` 替换为卡片上显示的完整表名。

```yaml
documents:
  - path: docs/orders.md
    type: data_description
    tables:
      - warehouse.public.orders
      - warehouse.public.order_lines
  - path: docs/methods.md
    type: analysis_knowledge
```

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

主要产物位于运行目录：

- `physical_catalog.yaml`：本次授权物理目录。
- `construction/drafts/`、`evidence/`：草稿修订、验证结果和证据记录。
- `construction/published/` 与 `manifest.yaml`：已发布图谱及当前版本、历史版本索引。
- 工作流输出下的 `checkpoints/` 和 `workflow-runs/`：按运行和节点保存的图、证据及恢复记录。
- `construction/config.yaml`、`construction/runtime-result.yaml` 与 `report.yaml`：有效目标、内核运行结果和构建报告。

Harness 原生会话与调用轨迹使用原生 JSONL，单独保留，不冒充图谱事实。草稿校验失败可继续修改；已保存内容不会因单次模型失败自动删除。发布要求当前修订通过数据检查且来源仍有效，语义判断标记为 `agent_authored`。发布成功不等于客户语义已全部证明。

Agent 用 `read_workspace(summary_only=true)` 可分页查看草稿修订和验证状态，无需重复加载项目图。大草稿和完整验证诊断可按修订分页重读，模型无需靠会话搜索重建已保存的 YAML。实例验证增量累积图谱及来源支持，并记录工作集估计；单次 SQL、模型请求与整轮工具等待分别计时。图谱工具通信故障或连续摘要截断会触发有界停止，保留成果供下次继续。设计和验收边界见[实例验证与运行恢复](design/lightweight_graph/MATERIALIZATION_RECOVERY_20261109.md)。

草稿的 `unresolved` 只记录本次目标尚未完成的事项，可以为空；`decisions` 记录已采用的判断，`suggestions` 记录后续建议。建议不会把成功运行改为有缺口。旧草稿中的未决原文仍保留，需要明确修订后才改变状态。压缩摘要不能改写原始目标；目标和 skill 保留在不参与历史压缩的系统上下文中。

没有名称列的节点可使用 `display_columns`，或从已关联对象生成显示名称；显示名称不参与身份计算。页面分别展示业务身份、访问范围和来源支持，搜索支持编码和别名。运行报告按草稿包、实体类型、关系类型和映射统计；工具结果的历史压缩单独标记。用量包含摘要调用与缓存输入，缺失的服务商用量标为未知。

## 已验证与未验证

用 `fb0bbd8` 的代码、三张小型 CSV 和脚本化模型做过一次升级验证：两路抽取完成，旧合并节点隔离一组名称冲突，父运行为 `partial`，项目输出为空。在同一目录升级后，从原合并产物恢复出 4 个节点、1 条边，未启动 worker，原运行记录和覆盖缺口都保留。另有[跨源合并 HTTP 回归](src/lightgraph/tests/test_web_parallel_fusion.py)：只改合并节点的身份规则，两份指标合为一个，两种名称均可追溯，历史版本仍保留原来的两个节点。这些结果未测量客户数据库的墙钟耗时。

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

本轮连接与发布行为见[短连接和冲突隔离设计](design/lightweight_graph/CONNECTIONS_AND_CONFLICTS_20261109.md)：每组完整投影都有独立事务；连接恢复最多三次。名称分歧默认保留各自的来源，也可配置为隔离或阻止发布。
