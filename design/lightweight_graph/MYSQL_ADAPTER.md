# MySQL 与 Gauss 并存接入

更新：2026-10-21。MySQL 接入沿用已有 SourceAdapter 契约，不替换 Gauss。一个项目的 sources 可以同时包含 mysql_database、gauss_database、excel_workbook 和 csv_file。

## 配置与首次连接

示例见 [mysql.yaml](../../examples/mysql.yaml)。数据库连接串只通过环境变量传入，格式为 `mysql://user:password@host:3306/database`；用户名、密码中有特殊字符时须做 URL 编码。不要把实际密码写进 YAML、页面或 Git。

```yaml
sources:
  - id: finance_mysql
    kind: mysql_database
    dsn_env: LIGHTGRAPH_MYSQL_DSN
    allowed_tables: [finance.report_item]
    allowed_columns:
      finance.report_item: [tenant_id, item_code, item_name]
    capability_manifest: mysql-capabilities.yaml
  - id: finance_gauss
    kind: gauss_database
    dsn_env: LIGHTGRAPH_GAUSS_DSN
    allowed_tables: [finance.report_item]
    capability_manifest: gauss-capabilities.yaml
```

MySQL 使用 `database.table`，Gauss 使用 `schema.table`。来源编号参与完整表身份，两个库里的同名表不会合并。Web 工作台的“添加数据库”可选这两种数据库；上传能力文件后选择对应文件，配置只引用本项目资产。

```bash
.venv/bin/lightgraph connect-check examples/mysql.yaml
.venv/bin/lightgraph connect-check examples/mysql.yaml --verify-runtime --output mysql-capabilities.yaml
```

第一步只读取授权目录并检查基础连接能力，不执行实例分析。第二步在客户批准的环境执行短时间超时/取消探针，能力清单绑定产品、版本和驱动。真正抽取前，还需通过部署留存检查并设置 `deployment_retention_verified: true`。配置运行 YAML 和工作台不会自动批准这项检查。

能力清单可用 JSON 或 YAML，按 source id 保存。Web 上传时使用 `.yaml`/`.yml`；`connect-check` 输出的 JSON 同时是合法 YAML。读取时拒绝超大文件、YAML 别名和自定义 tag。产品、服务端构建版本或驱动变化后要重新核验，不直接沿用另一环境的清单。

## 适配器行为

- 使用 PyMySQL 1.2.3 及以上的 1.x 版本，启动时核验实际安装版本。SSCursor 逐批消费结果，未读完时中断该连接，避免关闭游标时隐式读取全部剩余行。数据和取消控制各使用一个连接，并核验它们的 server_uuid 与 CURRENT_USER 一致，避免代理把取消指令发到其他服务器。
- 每个一致性快照启用 REPEATABLE READ、READ ONLY；当前只接受 MySQL 8.0/8.4 的 InnoDB 基础表。视图和其他引擎不会被冒充为已验证的一致快照。
- 读取表、列、注释、主键、唯一键和组合外键。外键两端必须都在授权表列范围内；没有读取的约束仍为 unknown。
- 编译器使用 MySQL 反引号、CHAR/CHAR_LENGTH 等方言，工具层仍使用 `:name` 参数，连接器转换为 PyMySQL 的绑定形式。Agent 工具说明与元数据结果会注明当前 SQL 方言。
- 固定并核验数据连接的 SQL mode，避免服务器的 ANSI_QUOTES/NO_BACKSLASH_ESCAPES 改变解析器已验证的引号语义。
- 查询端只接受受控 SELECT/CTE。拒绝多语句、写入、INTO OUTFILE/DUMPFILE、用户变量赋值、锁、危险函数、系统表、执行注释和优化器权限绕行。合法语法仍须通过表列授权和连接形状检查。
- `max_execution_time` 控制服务端 SELECT 时间；本地共享预算进一步限制查询次数、返回行/字节和总时长。取消仅通过私有控制连接 `KILL QUERY` 当前数据连接的固定 ID，Agent 不能指定取消目标。超时或取消后废弃数据连接，防止延迟到达的取消请求影响后续查询。
- 内部 SET/START TRANSACTION/ROLLBACK/KILL 是连接器固定控制语句，不作为 Agent 可调用 SQL 能力。应用只读不能代替数据库账号的 SELECT 权限配置。

MySQL 的只读事务语法与一致快照约束参考 [官方事务文档](https://dev.mysql.com/doc/refman/8.4/en/commit.html)。取消与权限参考 [KILL](https://dev.mysql.com/doc/refman/8.4/en/kill.html)，超时参考 [max_execution_time](https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html#sysvar_max_execution_time)，游标行为参考 [PyMySQL 游标文档](https://pymysql.readthedocs.io/en/latest/modules/cursors.html)。

## 保留规则与边界

与 Gauss 相同：查询返回的行、参数和原始驱动错误不保存；内存批次释放，报告仅保留允许的计数、状态和错误码。元数据、已验证规则和合格图事实可保存。工作台允许保存用户上传文件，不表示允许保存数据库抽样。

跨 MySQL/Gauss/文件的关系核验复用现有有界跨源比较：各端独立快照和 DISTINCT 投影，在内存中核验。它不是联邦 SQL，也不保证跨库同一时点的一致性。

当前不承诺 MariaDB、Percona、TiDB、Aurora 等兼容产品；未识别为 Oracle MySQL 的产品版本明确拒绝。BINARY/BLOB 的 bytes 和 TIME 的 timedelta 等非现有标量合同值在实例投影时拒绝，不静默转成错误实体。JSON 属性结构也不因数据库接入而自动扩展抽取规则。

本轮通过替身驱动、SQL 方言及原有 PostgreSQL/SQLite 回归测试，未连接真实 MySQL 或 Gauss；不能据此声称已通过客户环境验证。项目本机的 Docker 引擎未启动，没有运行临时数据库。真实环境要补充目录权限、TLS、只读账号、服务端取消、完整抽取及留存验收。
