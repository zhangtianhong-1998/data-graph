# 记录运行基线

在数据环境的新实验开始前执行。工具只读取本地配置、代码和产物，计算摘要，不连接数据库，不调用模型，也不复制原始行、文档正文、密钥或 DSN。

```bash
.venv/bin/python scripts/capture_run_baseline.py --config /absolute/project/config.yaml --output /absolute/experiment/before.json --run-id experiment-name
```

Windows 将 `.venv/bin/python` 换为 `.venv\Scripts\python.exe`。独立 CLI 项目将 `--config` 指向实际运行 YAML。工作台项目必须指向项目内的 `config.yaml`；工具会按工作台规则读取项目登记、独立 Agent 配置和当前输出代际。`project.yaml` 是项目登记文件，不是运行配置。

工具拒绝覆盖已有清单。实验后用不同输出路径再记录一份，比较输入、代码、检查点和发布版本。`--repo` 可以显式指定源码目录，`--frontend` 可以指定实际前端产物目录。数据文件、配置和产物在读取过程中改变时，本次记录失败，应重新采集。

清单记录：

- 源码工作树的逐文件摘要、Git 提交、Python 依赖版本、Node 版本和前端产物摘要。
- 有效配置及其来源文件摘要、模型配置来源、环境数据库连接的非明文绑定摘要，以及权限和当前读取范围摘要。
- 本地输入文件摘要、检查点文件摘要、当前发布指针及人工裁决账本摘要。
- 采集进程的 Python 路径、模块导入路径，以及模块当时是否已经加载。`repository_import_match=false` 表示导入路径不在指定源码目录，需要先核对部署。

单独运行本命令只能核对采集进程。`remote_service_verified=false` 明确表示没有检查另一常驻服务进程；模块文件摘要反映磁盘内容，不能证明旧服务已经重新导入修改后的代码。实验程序可在实际执行进程中调用 `lightgraph.runtime.run_baseline.capture_baseline(config_path, config=resolved_config, run_id=run_id)`，记录其实际配置和导入路径。新实验应使用新启动的进程。

数据库来源的 `snapshot_verified=false` 表示工具没有核实数据库内容快照。数据环境需要另行固定数据库快照或提供可核验的读窗口；不能仅凭配置和代码摘要声称两轮使用了相同数据库数据。清单中的业务源名称、文件路径及产物数量也属于内部运行信息，按项目权限保管。
