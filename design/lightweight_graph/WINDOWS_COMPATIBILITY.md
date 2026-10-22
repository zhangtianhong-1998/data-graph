# Windows 运行与文件锁

更新：2026-10-22。

## 本次修复范围

原实现直接导入 `fcntl`，且 Web 服务在非 POSIX 系统主动拒绝启动。这会阻断 Windows 上的实际抽取与工作台运行，不只是测试无法运行。跨平台适配必须保留单写者约束，不能以空锁、线程锁或跳过测试替代。

`runtime/locking.py` 统一提供输出目录和工作台服务使用的进程锁：

- Linux/macOS 使用非阻塞 `flock`。
- Windows 使用 `CreateFileW` 的独占共享模式；后续进程打开同一锁文件时被操作系统拒绝。锁文件可以留在目录中，是否被持有由句柄决定，不根据文件是否存在或历史 PID 判断。
- 启动 worker 时只传入指定的锁 FD/HANDLE。父服务关闭或崩溃后，只要 worker 仍持有继承句柄，新的服务就无法取得同一 workspace；最后一个持有者退出后自动释放。
- 关闭锁对象只关闭当前句柄，不主动解除其他进程仍持有的锁。锁路径拒绝符号链接和 Windows 重解析点。

Windows 的 `Popen.terminate()` 会强制终止进程，不能当作发送 POSIX SIGTERM。工作台先写入固定取消标记，监视线程请求 `source.cancel()`，主线程在处理阶段检查取消状态；信号处理器只记录状态，不异步抛出异常打断清理。若驱动、模型或文档处理不能及时退出，父服务在期限后强杀。没有合法清理报告或被强杀时必须显示 `cleanup_status=unknown`。

参考：[Microsoft 文件共享与继承说明](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[Python 子进程句柄白名单](https://docs.python.org/3/library/subprocess.html#subprocess.STARTUPINFO)。

## CSV 保真

测试用例写入包含换行的单元格时，明确指定 UTF-8 和 `newline=""`，避免 Windows 文本写入把 LF 自动变成 CRLF。CSV 生产解析继续保留单元格中真实存在的 LF、CRLF 或 CR；不能通过统一替换换行来使测试通过，否则会修改输入值。

## Windows 开发命令

在项目根目录的 PowerShell 中执行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
npm --prefix src/workbench ci
npm --prefix src/workbench run build
.\.venv\Scripts\lightgraph.exe serve
```

Python 的 UTF-8 模式用于跨语言开发和测试夹具；项目持久化接口自身显式使用 UTF-8。若手工启动多个服务，须使用不同 workspace；不能删除一个仍被持有的锁文件来强行并发运行。

## 验证范围

GitHub Actions 对 Windows Python 3.11/3.13、Linux Python 3.13、macOS Python 3.13 执行完整后端测试，另执行前端测试及构建。测试只使用合成数据和替身服务，不提供客户数据库或模型凭据。

核心回归包括同目录互斥、独立进程争抢、进程崩溃释放、父进程退出后子进程继续持锁、任务取消、结果恢复和 CSV 换行保真。实际执行结果及对应提交记录在 [实施与验收记录](../../IMPLEMENTATION_STATUS.md)；配置 CI 不等于 CI 已通过。

本轮不包含客户的华为 IAM 集成分支，也不证明真实数据库或模型服务已验收。网络文件系统、共享盘和第三方进程管理器还需按部署环境核验。
