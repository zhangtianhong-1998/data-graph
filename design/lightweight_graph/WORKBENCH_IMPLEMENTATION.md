# Vue 工作台与项目服务

更新：2026-10-23。本文记录实际实现，优先于旧工作台设计中尚未开发的接口与页面约定。

## 入口与目录

前端为 Vue 3、TypeScript、Vite，源码在 `src/workbench/`。后端为 FastAPI，位于 `src/lightgraph/web/`。服务复用原有 pipeline、AgentScope、SQL 守卫和结果库，不另设抽取流程。

```bash
.venv/bin/python -m pip install -e '.[test]'
npm --prefix src/workbench ci
npm --prefix src/workbench run build
.venv/bin/lightgraph serve
```

访问 `http://127.0.0.1:8000`。生产构建由同一个后端提供，页面请求同源 `/api`；开发时运行 `npm --prefix src/workbench run dev`，Vite 将 `/api` 代理给 8000。不是 `vite preview` 单独启动抽取引擎。

`serve --port 8000 --workspace <目录> --frontend-dir <dist目录>` 可覆盖默认位置。默认 workspace 是源码仓库根目录下的 `workspace`，不是运行命令的当前目录。源码 checkout 是当前交付方式；没有把 Vue 构建产物塞入 Python wheel。

```text
workspace/
  .service.lock
  <project_id>/
    project.yaml
    config.yaml
    files/<file_id>_<原始文件名>
    metadata/<file_id>.yaml    # 单个上传文件的结构预览
    output/
      metadata.yaml
      metadata-context.json
      metadata/...
      graph.sqlite
      report.json
      graph.json、graph.yaml、viewer.html 等
    runs/<run_id>.json
```

根目录和项目目录自动创建。项目编号只能使用字母、数字、下划线、短横线，创建后不变。文件有独立 ID；同名上传不会覆盖另一份文件。配置路径必须指向本项目已登记文件，拒绝跨项目路径、绝对输入路径、目录穿越与符号链接。`output_dir` 固定为本项目 output，`isolation_domain` 固定为项目编号。

上传的 Excel/CSV/YAML/Markdown 是用户授权保存的输入资产。数据库查询行、DISTINCT 值列表、样本、模型对话和 worker 标准输出不是资产，不写入上述目录。资产文件摘要只用于检测输入变更，不对数据库返回行计算持久化摘要。

## 页面到后端的实际流程

界面按两个阶段组织，配置页面不再与结果图交错排列：

“项目配置”和“运行与结果”使用一级页签，保留各自上次选中的子页；支持左右方向键和 Home/End 切换。主内容区不再重复显示项目大标题及编号，项目身份仍可在侧栏和面包屑中查看。

桌面数据接入页按窗口剩余高度布局。中间数据列表、右侧表结构或 YAML 各自滚动，标题、切换按钮和底部操作保持可见。600px 以下改为纵向排列，各面板仍有独立滚动区，外层内容区允许滚动。弹窗只滚动正文，关闭和提交按钮固定在弹窗内；高度使用 `dvh`，并保留 `vh` 回退。平台兼容样式集中在 `src/workbench/src/compatibility.css`。

图谱默认按原始大小显示，字体不会随节点数量隐式缩小。“适配全图”显式缩放到完整布局，“原始大小”恢复可读比例；缩放以当前视口中心为基准，拖动距离按视口坐标换算。

本轮验证：前端 21 项测试、TypeScript 检查和生产构建通过；macOS Chrome 检查了 1280×800、1280×600、820×700、390×700 和 390×600 布局、弹窗操作区、页签键盘切换、元数据独立滚动、YAML 切换及图谱缩放。Safari 实机检查了桌面数据接入布局和表结构/YAML 切换。Windows/Linux 浏览器及 Firefox 的视觉表现尚未实机验收，不能以自动化测试通过替代。

| 阶段 | 页面 | 内容 |
| --- | --- | --- |
| 配置 | 数据接入 | 本地数据、数据库连接与选表、元数据 YAML 预览 |
| 配置 | 知识文档 | Markdown 上传、用途、全局或指定表范围 |
| 配置 | 抽取目标 | 目的、包含与排除内容、发现提示 |
| 配置 | Agent 与模型 | URL、模型名、会话 API key、流式开关和高级配置 |
| 运行与结果 | 元数据图 | 表、列、声明外键及已核验的列关系 |
| 运行与结果 | 业务图谱 | 当前发布的业务实体和关系 |
| 运行与结果 | 运行记录 | 状态、报告、取消与临时数据清理结果 |

1. 创建项目，服务保存 `project.yaml` 和初始 `config.yaml`。
2. 数据接入分为三列：左侧切换数据库或本地文件，中间显示已选数据库表或上传文件，右侧显示当前项的表列和 YAML。默认选择第一项；切换时清空旧预览，迟到的请求不能覆盖新选择。上传 XLSX/CSV 后登记来源，并单独解析该文件的表结构；无需先连通项目中的其他数据库。CSV 初值为 UTF-8、逗号、首行表头、空串为 NULL、文本解析。结构预览只保存声明，不保存单元格值，也不冒充整个项目已完成元数据读取。
3. 数据库可输入连接串，或地址、端口、数据库、用户名和密码。先测试连接，再读取表清单并筛选勾选；也可手动输入表名或导入 Excel 表清单。后者仅用于选表，不登记为业务数据来源。提交后保存授权表并读取所选表的元数据。

   Gauss 的连接串模式支持 `jdbc:gaussdb://db.example.com:8000/business` 和 `jdbc:postgresql://db.example.com:8000/business`。粘贴 JDBC 地址后显示独立用户名、密码字段，密码中的特殊字符原样提交，无需用户编码。地址末尾的 `business` 是数据库名，Schema 仍单独填写。用户名和密码分开传递的形式参照[华为云 JDBC 连接介绍](https://support.huaweicloud.com/centralized-devg-v10-gaussdb/gaussdb-42-0082.html)。

   当前支持单主机 JDBC URL；可选查询参数为 `user`、`password`、`sslmode`、`connectTimeout` / `connect_timeout`。查询参数中的特殊字符需要 URL 编码；重复参数、与单独输入不一致的凭据、其他 JDBC 驱动属性会报错，不会静默忽略。Schema 使用页面独立字段，不使用 `currentSchema` 参数。连接超时仍固定为 5 秒，远程连接仍要求 `verify-full`；本次没有增加证书配置入口或更换驱动。后端将 JDBC 地址转成现有驱动可用的连接信息，供测试、选表、元数据读取和后续运行共用，仅保留在会话内存。原生 PostgreSQL/MySQL DSN 的行为不变；服务器环境变量仍填写原生驱动 DSN。

   数据库弹窗仅通过关闭按钮退出，点击遮罩、移出鼠标或向外拖选都不丢弃表单。连接状态和错误显示在正文之外的固定反馈区。测试前先校验必填项；测试及读取表清单直接调用只读接口，无需先建立凭据会话，只有保存来源时才建立会话。测试成功自动读取表清单，两个阶段分别显示进度。前端等待上限为：测试 15 秒、表清单 30 秒、会话 10 秒、元数据提交 120 秒。超时不会自动重发请求，也不代表服务端操作已经回滚。

   当前前端 43 项测试及构建通过；数据库接入与会话凭据相关后端测试 108 项通过。浏览器通过 4173 代理实际验证了 JDBC 地址识别、独立账号输入、缺少用户名、本机关闭端口的连接失败。此前验证过固定错误反馈及遮罩点击保留表单；连接成功后选表、凭据向运行任务传递使用模拟接口或驱动验证，客户数据库的真实连通性仍需有效配置验证。
4. 元数据提供 `tables.yaml`、`constraints.yaml`、`foreign_keys.yaml` 预览与下载。主键、唯一键、外键来自连接器声明；未读取的 CHECK/default 等保持 unknown。文件来源没有声明外键时不会凭名称自动补上。导出和打开缓存不连接数据库；全项目“读取元数据”是显式操作。
5. 上传 Markdown 后加入 documents；无范围声明时默认全局，已有 frontmatter 范围保留。可选择数据说明、分析知识、混合或待分类，以及全局或具体表范围。绑定写回实际 frontmatter；表标识为 `source_id.schema.table`。
6. 填写抽取目的、包含/排除内容和探索提示。在 Agent 配置中填写 URL、模型名与流式开关，API key 可直接输入为会话凭据；原有环境变量方式继续支持。
7. 开始抽取时先保存配置，再创建后台 worker。worker 使用固定配置调用真实 pipeline，来源与文档依赖指纹在前后检查。页面每秒查询任务状态，完成后可查看元数据图、业务图与运行记录。
8. 业务图以 GraphStore 当前发布为准。原图非空时后续运行继续使用同一结果库，实现同项目增量。

运行期间不允许从 API 修改项目输入。当前采用锁定编辑，而不是旧设计中的“当前运行快照 + 下一轮可编辑草稿”双版本界面。手工在磁盘修改输入会报告 `partial/input_dependency_changed`；已经发布的完整分区不会因此自动回滚。

## API

基础路径 `/api`。错误体统一为 `{"error":{"code":"..."}}`，不包含原始请求、驱动错误、查询结果或模型响应。

| 请求 | 内容 |
| --- | --- |
| GET /health | 服务健康状态 |
| GET /projects；POST /projects | 列出、新建项目 |
| GET /projects/{pid}；PATCH 同路径 | 项目详情、改名 |
| POST /projects/{pid}/session | 创建仅在内存中的项目会话 |
| GET /projects/{pid}/credentials | 会话凭据存在状态，不回显内容 |
| PUT /projects/{pid}/credentials/model | 保存会话 API key，绑定当前模型配置 |
| DELETE /projects/{pid}/credentials/model | 清除会话 API key |
| POST /projects/{pid}/database/test | 用请求中的连接信息验证只读连接 |
| POST /projects/{pid}/database/tables | 读取指定 schema 的有界表清单 |
| POST /projects/{pid}/database/table-list | 从 Excel 读取表名清单，不登记为数据来源 |
| POST /projects/{pid}/database/connect | 保存来源/授权表、保留会话凭据并读取元数据 |
| POST /projects/{pid}/files | multipart 上传 file |
| GET /projects/{pid}/files/{fid}/metadata | 单个本地数据文件的表结构和 YAML 预览 |
| GET /projects/{pid}/metadata/yaml | 已缓存的元数据 YAML；可用 source_id、schema、table 限定当前选择，交叉筛选无匹配时返回空列表，不回退到全项目；不连接源库 |
| GET /projects/{pid}/files/{fid}/content | Markdown/YAML 原文预览，重新检查 YAML 凭据 |
| PATCH /projects/{pid}/files/{fid}/binding | Markdown purpose 与 scope |
| GET /projects/{pid}/config；PUT 同路径 | 当前完整 RunConfig |
| POST /projects/{pid}/config/import | 将指定上传 YAML 导入为配置 |
| GET /projects/{pid}/config/export | 导出无密钥配置 YAML |
| GET /projects/{pid}/metadata | 已读取的元数据；未读取或配置变更时提示刷新 |
| POST /projects/{pid}/metadata/refresh | 显式刷新授权目录并保存元数据 YAML |
| GET /projects/{pid}/graph?layer=business\|metadata | 当前图；元数据层走同一缓存权限检查 |
| GET /projects/{pid}/runs；POST 同路径 | 列出任务、启动真实抽取 |
| GET /projects/{pid}/runs/{rid} | 任务实际状态和安全报告 |
| POST /projects/{pid}/runs/{rid}/cancel | 请求取消并等待有界清理 |

上传单文件 20 MiB，Markdown/YAML 2 MiB，请求体上限 21 MiB。YAML 禁止 alias、anchor、自定义 tag、重复键；持久化配置禁止明文 API key、密码和 DSN；直接输入走单独的会话接口，环境变量方式仍可使用。API 不提供任意 SQL 执行入口；Agent 工具仍经过内部 SQL 守卫。

元数据缓存绑定来源、允许表列范围和上传输入摘要。变更后不把旧授权目录继续作为当前目录返回。刷新声明时，仅当引擎元数据的来源全集与各 schema 版本都吻合，才合入它的验证关系；否则返回新声明和 `stale_engine_metadata` 提示，等待重新运行。

图接口最多返回 1,000 节点、3,000 边，并标记真实总数与截断状态。前端一次绘制最多 240 个筛选后节点。搜索只覆盖已返回集合；本期不是全图分页或大规模图检索服务。

## 会话凭据

数据库密码、连接串和 API key 不写进项目配置、YAML、运行记录或浏览器存储。页面通过单独接口交给服务端内存，使用与项目绑定的会话令牌访问。刷新页面、服务重启或会话过期后需要重新输入；配置页面只显示是否已提供凭据，不回显密钥。

模型密钥绑定配置中的服务地址与模型名，修改后需重新提供。数据库接入操作更新来源配置和目录身份；旧目录不能仅因来源编号相同而继续视为有效。Worker 所需凭据通过 stdin 管道传递，不使用全局环境变量临时替换，不写临时文件。

连接测试与读取表清单只执行固定的只读目录查询，不接收任意 SQL，也不执行业务表的 COUNT 或样本查询。测试通过不替代实例抽取的权限、超时/取消能力和部署留存验证。手动输入或导入的表清单仍需连接器核验访问权限。

## 后台任务与清理

服务通过内存管道把固定配置交给独立 Python 进程。stdout/stderr 丢弃，不保留模型或驱动日志。任务记录只保存安全报告、时间、状态、依赖摘要。

- `status`：queued、running、completed、partial、failed、cancelled、interrupted。引擎 COMPLETED_WITH_GAPS 映射为 partial，原报告仍保留。
- `stage`：starting、extracting、finished。没有用计时器伪造 Agent 每个子阶段或百分比。
- `cleanup_status`：pending、completed、unknown。worker 关闭来源后才报告 completed；强杀、无有效结果或服务异常恢复时为 unknown，不代表系统内存物理擦除。

取消先写入仅含固定状态的运行专属标记，worker 在内存中请求来源取消并执行清理；超过期限才强制终止。标记在收尾或重启恢复时清除，不保存查询内容。Windows 的 terminate 是强杀，不用于冒充正常清理。

取消不等于“本次没有发布任何结果”。页面仍显示已经发布的完整分区，未完成分区不会被当作完整删除证据。worker 正常退出但没有合法报告时，服务判失败。

一个 workspace 只允许一个服务，服务持有操作系统文件锁，worker 继承同一锁。父进程异常退出时，旧 worker 没退出就不能启动第二个写者；不能用磁盘里记录的旧 PID 去杀进程。待旧 worker 退出后重启服务，未结束记录显示 interrupted。macOS/Linux 使用 flock，Windows 使用独占文件句柄及显式句柄继承，详见 [Windows 兼容说明](WINDOWS_COMPATIBILITY.md)。

## 已实现与后续边界

实际具备项目隔离、文件持久化、目标/文档配置、真实后台执行、增量结果读取、两类图、取消与状态恢复。浏览器刷新后重新从 API 读取，不依赖 localStorage，也不预置合成结果。

尚未实现：多用户登录/RBAC、跨项目继承、运行间图差异视图、文件删除/替换界面、在线 Markdown 正文编辑、物理元数据 YAML 在线改写、全图服务端搜索、细粒度 Agent 事件/SSE。当前是监听本机的单用户工作台；来源权限仍由数据库账号与表列白名单决定。不能将它直接当作公网多租户服务部署。

用户填写模型配置或数据库连接配置，不等于相应环境已验收。真实模型发现质量、真实 MySQL/Gauss 权限与取消、服务端日志和部署级不落盘要求另行验证。当前实测结果汇总在 [实施与验收记录](../../IMPLEMENTATION_STATUS.md)。

实现参考：[Vue 官方入门](https://vuejs.org/guide/quick-start.html)、[FastAPI 上传文件](https://fastapi.tiangolo.com/tutorial/request-files/)、[Vite 开发服务器](https://vite.dev/config/server-options.html)。
