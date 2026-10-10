# Carbon-Lens 真实环境验收运行手册

## 目标与安全边界

本手册用于在已授权的 CarbonData 环境中复跑 `docs/live-acceptance.md` 中的证据。默认模式只执行常量 SQL 的只读检查：

```sql
SELECT 1 AS acceptance_value
```

不要把生产业务表当作默认测试集，也不要为了“通过”而绕过访问控制。验收不执行 DDL、DML、删除、写入或故障注入。`config.json`、密码、keytab 和 SSH 私钥不得提交或打印。

## 环境准备

在仓库根目录运行。推荐使用项目虚拟环境；Windows PowerShell 示例：

```powershell
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode preflight
```

Linux 示例：

```bash
.venv/bin/python scripts/live_acceptance.py --mode preflight
```

配置文件默认是仓库根目录的 `config.json`。可用 `--config` 指向经过授权的本地配置。只报告连接别名和传输方式，不回显敏感值。

## 分阶段运行

先运行安全烟测和异步/导出检查：

```powershell
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode smoke
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode jobs
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode exports
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode benchmark --repetitions 5
```

也可以一次运行上述安全阶段：

```powershell
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode all
```

每次运行都会在 `.acceptance-data/live-<run-id>/results.json` 写入脱敏证据；该目录已加入 `.gitignore`。`benchmark` 只代表常量 SQL 的端到端烟测耗时，不能替代业务数据性能结论。

## 业务数据验收

只有在业务负责人明确提供测试表、SQL、预期结果和授权范围后，才可使用：

```powershell
.\.venv\Scripts\python.exe scripts\live_acceptance.py `
  --mode correctness `
  --sql-file .\path\to\authorized-readonly-test.sql `
  --allow-data `
  --connection carbon-prod
```

SQL 文件必须是只读、有限范围、面向授权测试集的单条语句；不要把真实生产业务载荷复制到报告。若没有这些输入，结果应保持 `BLOCKED_ENV`，不能改写成 `PASS`。

## 失败处理与重试

`BLOCKED_ENV` 表示环境、授权、依赖或测试数据不足；`UNVERIFIED` 表示拿到了运行结果但尚未完成业务核对；`FAIL` 才表示已有明确预期且实际结果不符。先保存对应 `results.json`，再按连接、Beeline、权限、超时和远端临时目录逐项排查。不要重复执行可能产生高负载的 SQL。

## 交付清单

- `docs/live-acceptance.md`：本次运行的真实证据与结论。
- `docs/live-acceptance-runbook.md`：本手册。
- `scripts/live_acceptance.py`：可复跑 runner。
- `.acceptance-data/live-<run-id>/results.json`：本地脱敏证据，不提交。
