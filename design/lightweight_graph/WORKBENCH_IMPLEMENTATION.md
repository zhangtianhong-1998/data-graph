# Vue 工作台与项目服务

更新：2026-10-22。本文记录实际实现，优先于旧工作台设计中尚未开发的接口与页面约定。

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

1. 创建项目，服务保存 `project.yaml` 和初始 `config.yaml`。
2. 上传 XLSX/CSV 后自动登记来源；CSV 初值为 UTF-8、逗号、首行表头、空串为 NULL、文本解析，可在完整配置中修改。上传 Markdown 后加入 documents；无范围声明时为全局，已有 frontmatter 范围保留。
3. 编辑抽取目的、包含/排除内容和探索提示。模型设置支持 URL、模型名、密钥环境变量及流式开关。Gauss/MySQL 可通过数据库来源表单并存；高级列权限保留在完整配置中。
4. 显式“读取元数据”调用连接器的目录路径。打开项目、切换页面或读取缓存不会自动连接数据库。上传 Excel 的解析可以读取用户文件内容，但不执行数据库实例查询。
5. Markdown 可以选择数据说明、分析知识、混合或待分类，以及全局或具体表范围；绑定写回实际 frontmatter，后续引擎读取同一份文件。表标识为 `source_id.schema.table`。
6. 开始抽取时先保存配置，再创建后台 worker。worker 使用固定配置调用真实 pipeline，来源与文档依赖指纹在前后检查。页面每秒查询任务状态。
7. 元数据图和业务图分别读后端结果。业务图以 GraphStore 当前发布为准，不用可能过期的 graph.json。原图非空时后续运行继续使用同一结果库，实现同项目增量。

运行期间不允许从 API 修改项目输入。当前采用锁定编辑，而不是旧设计中的“当前运行快照 + 下一轮可编辑草稿”双版本界面。手工在磁盘修改输入会报告 `partial/input_dependency_changed`；已经发布的完整分区不会因此自动回滚。

## API

基础路径 `/api`。错误体统一为 `{"error":{"code":"..."}}`，不包含原始请求、驱动错误、查询结果或模型响应。

| 请求 | 内容 |
| --- | --- |
| GET /health | 服务健康状态 |
| GET /projects；POST /projects | 列出、新建项目 |
| GET /projects/{pid}；PATCH 同路径 | 项目详情、改名 |
| POST /projects/{pid}/files | multipart 上传 file |
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

上传单文件 20 MiB，Markdown/YAML 2 MiB，请求体上限 21 MiB。YAML 禁止 alias、anchor、自定义 tag、重复键；配置禁止明文 API key、密码和 DSN，使用环境变量。API 不提供任意 SQL 执行入口；Agent 工具仍经过内部 SQL 守卫。

元数据缓存绑定来源、允许表列范围和上传输入摘要。变更后不把旧授权目录继续作为当前目录返回。刷新声明时，仅当引擎元数据的来源全集与各 schema 版本都吻合，才合入它的验证关系；否则返回新声明和 `stale_engine_metadata` 提示，等待重新运行。

图接口最多返回 1,000 节点、3,000 边，并标记真实总数与截断状态。前端一次绘制最多 240 个筛选后节点。搜索只覆盖已返回集合；本期不是全图分页或大规模图检索服务。

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

尚未实现：多用户登录/RBAC、跨项目继承、运行间图差异视图、文件删除/替换界面、在线 Markdown 正文编辑、全图服务端搜索、细粒度 Agent 事件/SSE。当前是监听本机的单用户工作台；来源权限仍由数据库账号与表列白名单决定。不能将它直接当作公网多租户服务部署。

用户填写模型配置或数据库连接配置，不等于相应环境已验收。真实模型发现质量、真实 MySQL/Gauss 权限与取消、服务端日志和部署级不落盘要求另行验证。当前实测结果汇总在 [实施与验收记录](../../IMPLEMENTATION_STATUS.md)。

实现参考：[Vue 官方入门](https://vuejs.org/guide/quick-start.html)、[FastAPI 上传文件](https://fastapi.tiangolo.com/tutorial/request-files/)、[Vite 开发服务器](https://vite.dev/config/server-options.html)。
