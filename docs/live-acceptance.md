# Carbon-Lens 真实环境验收报告

## 结论

**最终状态：PARTIAL**。真实环境的 MCP、SSH/Beeline 通路、异步常量查询和同步 CSV/JSON 常量导出已获得证据；后台导出暴露了生命周期问题，业务数据性能/正确性仍需在明确授权的测试集上完成。当前没有把未执行或未核对的项目标记为 `PASS`。

本报告对应日期：2026-10-10。验收前代码基线：`38d7479 feat: harden Carbon-Lens query and export workflows`。本报告和 runner 属于当前未提交工作区变更。

## 结果矩阵

| 阶段 | 状态 | 证据/说明 |
| --- | --- | --- |
| G0 本地配置预检 | PASS | `config.json` 可解析；配置了 1 个连接；敏感字段未写入报告。 |
| G1 MCP/SSH/Beeline 可达性 | PASS（范围受限） | `carbon_list_connections` 返回 `carbon-prod`；`SELECT 1 AS check_value` 真实返回 1，`transport=ssh-beeline`，耗时约 9.8s。Kerberos、SFTP 和独立凭据轮换未单独注入验证。 |
| G2 数据正确性 | UNVERIFIED（结构/样本通过） | 已获授权测试 `sdr_nethouse.cloudphone_sales_users_all`；结构返回 6 列，`SELECT * ... LIMIT 100` 返回 100 行且未截断。尚无业务预期值，也未证明全表完整性。 |
| G3 异步持久化/重启 | PASS（范围受限） | 真实 `carbon_query_start` 返回任务，随后状态为 `succeeded`，返回 1 行且未截断；服务重启后的持久化仍需专用窗口或隔离实例。 |
| G4 取消/超时/断连 | NOT_APPLICABLE | 共享真实环境不执行故障注入；需隔离环境或明确窗口。代码级取消/超时回归已在既有测试中覆盖。 |
| G5 CSV/JSON 导出与续传 | FAIL（后台路径） | 同步 CSV/JSON 常量导出各返回 1 行且未截断；后台 CSV 导出长期 `running`，取消后仍为 `cancel_requested` 且未下载文件。已修复本地超时状态机的不可达分支，并补充回归测试；真实后台任务需重启服务后复测。 |
| G6 真实性能 | UNVERIFIED（有限样本） | 授权表的 100 行同步查询耗时约 12.2s，异步查询耗时约 20.5s；没有并发模型、SLA、全表规模和基线，不能给出业务性能 PASS。 |
| G7 问题修复与回归 | PASS（代码基线） | 既有自动化测试 21 项通过；本次未发现可安全复现的真实业务缺陷。 |
| G8 交付物 | PASS | runner、runbook、报告和脱敏证据目录规则已补齐。 |

## 已完成的真实检查

已通过 CarbonLens MCP 连接发现得到 `carbon-prod`，其配置使用 SSH + Beeline。对该连接执行了单条有限、只读的常量 SQL：

```sql
SELECT 1 AS check_value
```

结果为一行 `1`，未截断，返回传输标识 `ssh-beeline`。随后只执行了元数据级发现：`SHOW DATABASES` 成功返回 44 个数据库名；`SHOW TABLES` 对 `test_cs`、`test_ps` 均为空；对一个默认库表仅执行 `DESCRIBE`，未读取业务行。

异步查询也已完成：任务先返回 `running`，后续状态为 `succeeded`，结果为 `acceptance_value=1`。同步 `carbon_export` 的 CSV 和 JSON 常量导出均返回 1 行、未截断；后台 `carbon_export_start` 的 CSV 任务在超过 120 秒后仍为 `running`，发出取消请求后仍为 `cancel_requested`，因此该路径记录为失败而不是通过。

尝试读取默认库中的业务表行数据时被安全策略拒绝，因为该表不是明确授权的测试表。这是本次验收的正确阻断行为，不是通过改写 SQL 绕过的理由。

## 授权测试结果

用户已明确授权表 `sdr_nethouse.cloudphone_sales_users_all`。`DESCRIBE` 返回 6 个字段：`msisdn`、`charge_name`、`sales_date`、`dept_name`、`region_name`、`res_code`。

使用以下单条只读 SQL 做了有限抽样：

```sql
SELECT * FROM sdr_nethouse.cloudphone_sales_users_all LIMIT 100
```

同步查询返回 100 行，`truncated=false`，耗时约 12.2s；异步任务 `cq_de17882a0af14181a5c44b3dfde94e60` 最终为 `succeeded`，返回 100 行，`truncated=false`，耗时约 20.5s。报告不保存或回显业务行内容；`total_row_count` 未知，不能据此声称“全部”数据已读取。

## 复跑命令与证据

```powershell
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode all
```

运行结果写入 `.acceptance-data/live-<run-id>/results.json`，该目录被 `.gitignore` 排除。各阶段单独运行和授权测试集用法见 [`live-acceptance-runbook.md`](live-acceptance-runbook.md)。

本报告中的后续异步和导出状态应以最近一次 runner 的 `results.json` 为准；若 runner 因共享环境、凭据或服务生命周期失败，保留 `BLOCKED_ENV` 并记录失败原因，不改成 `PASS`。本次后台导出异常已形成代码修复：超时判断现在先于 `RUNNING`/`CANCEL_REQUESTED` 快速返回，避免任务永久悬挂；自动化回归由 21 项增加至 22 项并通过。

## 完成 G2/G6 所需输入

1. 一个明确授权的非生产或脱敏测试表，及其保留周期。
2. 只读 SQL、预期列/行/聚合结果和数据质量核对人。
3. 性能数据规模、并发数、超时/SLA、冷热缓存条件和基线。
4. 如需 G3/G4，提供隔离实例或明确的故障注入窗口。
