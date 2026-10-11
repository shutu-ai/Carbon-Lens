# Carbon-Lens — 真实环境端到端验收任务书（Codex 执行提示词）

> **角色**：你是 Carbon-Lens 项目的资深 Python / SSH / Kerberos / CarbonData / MCP 工程师和独立验收负责人。  
> **工作仓库**：`https://github.com/shutu-ai/Carbon-Lens`（在当前本地工作树执行）  
> **前置状态**：上一轮优化已本地交付，`python -m pytest -q` 为 `21 passed`；`docs/optimization-acceptance.md` 结论是 **PARTIAL**，原因是 Carbon/SSH/Kerberos/SFTP 真实环境尚未验证。  
> **本次目标**：补齐真实环境验收证据，验证**数据正确、执行稳定、性能可测、长任务可恢复**，并对可复现的缺陷做最小必要修复。  
> **不包含**：新建账号体系、RBAC、审批链、复杂安全网关、架构重写或当前版本的 DDL/DML 写入能力。

---

## 0. 执行约束与判定原则（必须遵守）

1. **先审代码、配置与环境，再设计和运行测试。** 不要假设任何账号、主机、表名、密钥、测试数据或 Fiber 方言特性已经存在。先读取实际代码、`README.md`、`MCP_FIRST_DEPLOYMENT.md`、`MCP_CONFIGURATION.md`、`docs/optimization-acceptance.md`、`docs/future-write-capability.md`、`config.example.json`、测试文件和相关脚本。
2. **以真实环境为证据**：Mock、离线 pytest 和合成 benchmark 不能替代真实 Carbon/SSH/Kerberos/Beeline/SFTP 验证。每个用例记录 `PASS / FAIL / BLOCKED_ENV / UNVERIFIED / NOT_APPLICABLE`，不能用“预期能通过”代替执行证据。
3. **不凭空填充连接参数**。读取当前开发机已有的环境变量、配置路径和连接别名；本地配置不存在时生成模板及具体所缺字段的清单，不猜测真实凭据。不要在报告、命令行回显或 Git 中暴露密码、keytab、Token、内网敏感信息或样本个人数据。
4. **真实环境验收只用只读 SQL**。未来 `CREATE TABLE AS SELECT`、`INSERT` 等是后续独立迭代；本轮不要借验收之名尝试写入、删除表或更改线上数据。无需扩展账号权限或增加审批机制。
5. **共享生产环境友好**：限定真实查询的表/分区、行数、并发、时长和输出体积；取消和网络故障注入仅作用于本次验收创建的任务或本地进程，不停止共享 Kerberos/SSH/Beeline 服务，不删除其他用户的临时文件。
6. **尽量使用现有 12 个 MCP 工具与底层实现**。可以新增独立 live-acceptance 脚本、测试 fixtures、诊断与最小 Bugfix；不要破坏已有工具名称、参数兼容性、配置环境变量和数据返回结构。
7. **不可假绿**：缺少真实网络或凭据时，不许只运行 Mock 后宣布 `PASS`。应完成所有能在本地执行的准备、离线验证和可复用脚本，最终报告标记为 `BLOCKED_ENV` 或 `PARTIAL`，清晰列出下一次复跑所需输入。
8. **验收不是性能宣传**：没有同环境、同 SQL、同时间窗口可比数据，就只报告绝对测量值，不宣称性能提升百分比；区分查询耗时、连接准备耗时、远程执行耗时、下载耗时与解析耗时。
9. 发现重大缺陷时先形成**可复现失败证据 → 最小修复 → 回归 → 复测结果**。不允许为追求 PASS 删除失败测试、放宽断言、忽略异常或人为修改已测结果。
10. 未经明确要求，不执行 `git push`、发布版本或创建线上资源；保留工作树中不属于本任务的改动。

## 1. 需要核对的仓库现状

重点入口（以当前工作树为准，不要假设文件和接口永久不变）：

- `src/carbon_lens/server.py`：12 个 MCP 工具与 stdio 启动。
- `src/carbon_lens/service.py`：同步查询、`DESCRIBE`、同步 CSV/JSON 导出、结果元数据。
- `src/carbon_lens/jobs.py`：异步查询，SQLite 任务记录，原子结果文件、状态查询、取消、重启恢复。
- `src/carbon_lens/exports.py`：异步导出，状态机、SFTP 下载、下载失败重试。
- `src/carbon_lens/beeline.py`：SQL 包装与 Fiber DSV/CSV 格式解析、远程执行脚本。
- `src/carbon_lens/ssh.py`：SSH/SFTP/跳板机与执行会话。
- `src/carbon_lens/config.py`：配置解析和限制。
- `src/carbon_lens/lifecycle.py`：Windows / Unix MCP 进程生命周期。
- `scripts/benchmark.py`：**仅用于本地解析性能比较**，不是远端基准。
- `docs/optimization-acceptance.md`：上一轮已通过项和真实环境 UNVERIFIED 项。
- `docs/future-write-capability.md`：本轮只验证其架构边界，不启用写入功能。

当前 MCP 工具接口（必须在 G1 中核对，不盲目硬编码数量或参数）：

```text
carbon_list_connections
carbon_query
carbon_query_start
carbon_query_status
carbon_query_list
carbon_query_cancel
carbon_describe
carbon_export
carbon_export_start
carbon_export_status
carbon_export_list
carbon_export_cancel
```

关键已知行为：

- `CARBON_LENS_CONFIG` 指定配置路径；`CARBON_LENS_JOB_DB` 可指定本地 SQLite 路径。
- 支持 `password_env`、`root_password_env`，远端依赖实际 SSH/`bigdata_env`/`kinit`/`fiber-beeline` 组合。
- 默认工具超时约 600 秒，工具最大 `timeout_seconds` 为 7200 秒；`max_rows`、`max_output_bytes` 由连接配置约束。
- 后台查询有 `query_id`，后台导出有 `export_id`；SQLite 保留任务元数据，完整查询结果可存本地结果文件。
- 完成后的 CSV 导出仅在 SFTP 文件下载成功后才应标记为 `succeeded`；失败下载可用原 `export_id` 继续状态查询重试，**不应重跑 SQL**。
- `truncated` / `has_more` 可能为 `true`、`false` 或 `"unknown"`（按实际接口判定）；`total_row_count` 未显式提供时可为 `null`；`type_info_unavailable` 应被如实保留。

## 2. 阶段及验收门禁

| 门禁 | 内容 | 优先级 | 失败处理 |
|---|---|---|---|
| G0 | 环境与测试范围就绪 | P0 | 阻塞则不执行 live destructive/disruptive 步骤 |
| G1 | MCP / SSH / Kerberos / Beeline / SFTP 连通性 | P0 | 失败时输出分层故障定位 |
| G2 | 数据正确性、字段完整性和截断语义 | P0 | 任一静默丢数/错数即 FAIL |
| G3 | 同步与异步查询、任务持久化及重启恢复 | P0 | 任务丢失/错误终态即 FAIL |
| G4 | 取消、超时、网络异常及状态竞争 | P0 | 有条件执行受控故障测试 |
| G5 | CSV / JSON 导出和断线下载重试 | P0 | 文件内容不一致或重复执行 SQL 即 FAIL |
| G6 | 有边界的并发、内存和端到端性能基准 | P1 | 有指标、无证据不宣称达标 |
| G7 | 缺陷修复与回归 | P0（如有） | 修复后重跑受影响门禁 |
| G8 | 报告、证据、版本和可复跑交付 | P0 | 缺少证据则不评为整体 PASS |

**整体结果定义**：

- `PASS`：G0–G5、G8 所有适用强制项实际通过；G6 有有效测量和说明；无未解决 P0 正确性/可靠性缺陷。若受控故障用例明确不适用，需有可审计原因并不虚称已测。
- `PARTIAL`：真实环境已跑一部分，部分关键用例因条件受限未验证，且没有已确认的阻断级失败；列明剩余项。
- `BLOCKED_ENV`：没有真实连接条件，或无法获得允许的真实测试范围；本地预检/测试可通过，但不能算 live PASS。
- `FAIL`：已执行的强制用例暴露未修复的 P0 缺陷，或证据与结果矛盾。

## 3. G0 — 真实环境就绪性预检

**交付**：一张无凭据的 readiness 表和可复跑的预检命令/脚本。

### 3.1 本机侧

核对：

- Python 版本、操作系统、虚拟环境与 `pip install -e '.[dev]'` 可行性。
- `python -m pytest -q`、`python -m compileall -q src tests`、`git diff --check`。
- `CARBON_LENS_CONFIG` 指向存在且可解析的 JSON；如果未设置，按仓库真实默认行为判断配置发现路径。
- 配置中的连接别名、`connection_mode`、SSH 目标、端口、用户、jump host（如有）、`run_as_user`、`env_script`、`beeline_path`、`beeline_connection`、`principal`、`keytab`、`remote_tmp_dir`、`export_dir`、`max_rows`、`max_output_bytes`。
- 本地 `export_dir`、SQLite 所在目录、查询结果存储目录可写，并有合理可用空间。
- SSH 密码或密钥、`CARBON_LENS_ROOT_PASSWORD` 等必要凭据**是否可被进程读取**，仅输出 `configured/missing`，不输出值。

### 3.2 远端侧

在不改变远端持久配置的前提下核对：

- DNS/地址解析、TCP SSH、SSH 登录、jump host（若有），记录失败阶段。
- 远端运行用户及 `su` 流程能否使用；`bigdata_env` 是否存在。
- `kinit`/keytab/principal 是否能成功获得 Kerberos 会话（记录返回码和脱敏错误类型，不打印凭据）。
- `fiber-beeline` 是否存在且能启动；配置的 Carbon 连接别名是否真实可用。
- 临时目录读写权限、SFTP 下载路径和必要磁盘空间。
- 如当前环境禁止在共享主机上运行额外检查，则只执行已授权的最小 smoke test，并明确标记跳过项目。

### 3.3 测试数据清单（禁止自行猜业务表）

尽可能从现有连接中寻找获准使用的只读测试表或视图，建立**本地不入库**的验收配置模板，例如：

```json
{
  "connection": "<existing-connection-alias>",
  "test_table": "<authorized_schema.table>",
  "key_columns": ["<stable-key-or-ordering-columns>"],
  "stable_filter_sql": "<permitted-small-partition-filter>",
  "expected_data_properties": {
    "has_null": true,
    "has_unicode": true,
    "has_duplicate_column_projection": true
  },
  "small_row_limit": 100,
  "medium_row_limit": 1000,
  "large_row_limit": 10000,
  "max_concurrency": 2,
  "allow_fault_injection": false
}
```

以上仅为 **schema 示例，实际值必须通过环境确定**。若没有真实 `NULL`、Unicode 等值，可以通过只读常量投影（经 Fiber 实测支持才用）验证序列化；不要修改线上数据来制造测试样本。将真实数据口径及哪些覆盖通过 SQL 常量构造写进报告。

若 G0 阻塞：立即停止需要真实集群的步骤，继续完成离线准备、脚本、文档，结果标为 `BLOCKED_ENV`，附最小所需信息清单；不追问无关细节。

## 4. G1 — 真实连接与 MCP 烟雾测试

通过真实 MCP stdio 客户端优先测试，与直接调用 Python service 分别保留证据（两者不是同一验收层）：

1. `carbon_list_connections`：返回连接别名，不泄露密码、private key、keytab 内容。
2. `carbon_query(connection, "SELECT 1 AS check_value", limit=1)`：数据值、列名、`row_count`、耗时正确；若方言不支持该语法，使用经原生 Fiber-Beeline 验证的等价语句并记录。
3. `carbon_describe(connection, table)`：获准测试表的字段可与原生 Beeline 元数据对比。
4. 明确捕获 SSH 失败、`su` 失败、Kerberos 失败、Beeline 连接失败、SQL 语法失败以及 SFTP 失败的独立诊断信息。
5. 对相同受控 SQL，分别通过真实 MCP 调用与原生 Beeline 执行，记录返回值与各阶段延迟。
6. 在已授权条件下进行一次正常断开再连接，验证客户端资源正常释放；不要修改全局 Kerberos 缓存或杀死共享进程。

**通过标准**：测试路径完整、成功结果正确、错误来源可定位；不能因为 Python 层 Mock 成功就标记真实 MCP 通过。

## 5. G2 — 数据正确性对比（最高优先级）

### 5.1 样本矩阵

根据实际测试表构造稳定且小规模的 SQL，覆盖：

- 单列、多列、字符串、数值、日期/时间（受 Fiber 返回格式影响需标注）。
- `NULL`、空字符串、字符串文字 `"NULL"`、空列别名（若方言支持）。
- 中文/Unicode、引号、逗号、分隔符 `|`、换行（若有适用样本）。
- 同名投影：`SELECT a.id AS id, a.id AS id ...`（按真实表改写），检查 `columns` 唯一键、`original_columns` 和 `column_name_map` 的语义，所有位置和数值仍可被还原。
- 不一致列宽时应显式报错，不得静默错位或丢行；可用受控模拟/本地 fixture 验证协议异常，不必破坏远端输出。
- `limit` 小于实际结果数、等于实际结果数、结果为 0 行的情况。
- `has_more`、`truncated`、`total_row_count`、`total_count_known`、`type_info_unavailable` 的实际契约。
- 同步 `carbon_query` 与 `carbon_query_start/status` 的相同 SQL 结果一致。

### 5.2 参考基线

使用同一个连接、同一 SQL、同一过滤范围，对比：

`原生 Fiber-Beeline 输出` ↔ `Carbon-Lens MCP 返回` ↔ `Carbon-Lens 导出文件`

注意：

- 没有明确 `ORDER BY` 的 SQL **不能**按行序直接比较。使用唯一稳定排序键，或比较包含重复次数的多重集合（multiset）。
- 参考结果应在同一数据快照/窗口内取得；可能变化的数据记录查询时间和波动范围，不能将原始表数据变化直接归咎于解析器。
- CSV/DSV 中的数值、时间等可能采用文本表示。比较时定义字段级规范化规则，**不得**把 `NULL` 与字面字符串 `NULL`、空串混成相同值。
- 有重复字段名时，比较应基于字段**位置**及映射，而不是把原始两列压成一个字典键。
- 不发起额外全表 `COUNT(*)`。如需要精确总数，只对允许的受控样本/小分区执行显式计数。
- 只对查询结果上限内的数据声明完整性。`total_row_count = null`、`truncated = "unknown"` 属于允许的显式未知，不得写成“总行数已知”。

**硬性 PASS**：受控稳定样本中 **0 字段丢失、0 静默改值、0 行错位、0 虚报完整结果**。保留脱敏 SQL、字段列表、对比散列、差异统计，不提交生产数据原文。

## 6. G3 — 异步查询、持久化与 MCP 重启恢复

### 6.1 基本工作流

1. 执行 `carbon_query_start`，记录新的 `query_id`、提交时间和 SQL 散列。
2. 用 `carbon_query_status` 轮询 `queued → starting → running → succeeded/failed/cancelled`。不要求每个瞬时状态必须可被观测，但不得出现非法状态倒退或多个相互矛盾终态。
3. 调用 `carbon_query_list`，核验 ID 与元数据一致。
4. 成功后核对 `result_available`、`returned_row_count`、`preview_rows`、完整 `result` 以及字段映射。
5. 验证 SQLite 主要保存任务元数据，大结果位于原子写入的独立结果文件；结果文件可以正常读取，无破损 `.part` 被当作成功结果。
6. 对同一个成功 `query_id` 重复查询状态，结果稳定，**不得重复执行远端 SQL**。

### 6.2 真正的重启测试

- **只重启本次验收创建的 Carbon-Lens MCP 进程**，保留 `CARBON_LENS_CONFIG`、`CARBON_LENS_JOB_DB` 和本地结果目录。
- 在远端任务仍运行时（若可可靠复现）停止本次 MCP 客户端进程；重启并使用同一 `query_id` 查询。确认能够发现远端任务、继续追踪并最终得到一致终态。
- 再针对已完成的查询重启一次，确认结果仍可读取，任务不丢失、不重新提交。
- 如远端任务在故障注入前已结束，不能声称“运行中重启恢复已验证”；可单独标记“完成后恢复 PASS、运行中恢复 UNVERIFIED”。
- 通过任务状态文件、远端目录、创建时戳/进程信息或其他可用证据判断未重复运行。避免为检查而重跑有实际成本的 SQL。

**硬性 PASS**：同一 ID 可恢复、终态一致、结果存在且可读、无无意重复提交。

## 7. G4 — 取消、超时、连接丢失与竞争条件

此门禁只能在 `allow_fault_injection=true` 且确认作用对象属于验收专用任务时执行有扰动的部分，否则对这些步骤标注 `BLOCKED_ENV` / `NOT_APPLICABLE`，并补 Mock/进程级测试；**不得无授权向生产网络注入故障**。

必须分别验证：

1. **运行时取消**：只取消自己创建的 `query_id`；轮询至真实终态，确认远端资源释放，状态不长期悬挂。
2. **完成后取消**：对已 `succeeded` 的 ID 调用 `carbon_query_cancel`，结果依旧为 `succeeded`，不能误改 `cancelled`。
3. **取消与成功竞争**：并行/重复触发后以真实远端终态为准，不发生 success → cancelled 错误转换；可用 Mock 精确制造竞态并补充真实小规模验证。
4. **超时**：在受控查询中设置短的 `timeout_seconds`，如果无法可靠制造超时，可明确标记并提供确定性测试；不得通过全表笛卡尔积等高负载 SQL 诱发超时。
5. **短暂 SSH/SFTP 断线**：优先通过仅作用于本次 MCP 进程的故障代理/替身实现；真实网络断连只在允许时进行。断线后不能将未知的远端结果直接误报为失败/成功，更不能因查询状态超时而自动二次提交。
6. **异常退出**：只终止本次创建的客户端/MCP 进程，随后复原，避免杀掉所有同名 Python/Beeline 进程。

记录每一步的 `query_id`、故障注入方法、前后状态、远端作业线索、最终资源状态。如果遇到不可确定的远端执行状态，明确标记 `UNKNOWN/UNVERIFIED`，不要假定已取消。

## 8. G5 — CSV/JSON 导出、SFTP 下载与完整性

### 8.1 同步导出

- `carbon_export(..., format="csv")`：确认 `export_dir` 中确实生成文件，路径、表头、字段位置、文件大小、行数与原生输出相符。
- `carbon_export(..., format="json")`：确认 UTF-8 正确、合法 JSON、所有记录可解析；如数值以字符串表示，按当前 `type_info_unavailable` 语义如实记录，**不得错误宣称保留原生类型**。
- 针对行数恰好等于 `limit` 的情况，检查 `truncated="unknown"` 或实际接口规定的语义；不要将等于上限误判为已确认没有更多行。
- 检查包含逗号、引号、换行、Unicode 的 CSV 是否可被标准 CSV Reader 无损读取。
- 校验临时 `.part` 文件不被误报为最终成功文件。

### 8.2 异步导出和下载恢复

1. `carbon_export_start(..., format="csv")` → 获得 `export_id`。
2. `carbon_export_status` → 跟踪远端完成、`downloading`、`succeeded` 等实际状态；仅**本地文件完成下载并校验后**为成功。
3. `carbon_export_list` 能查到原 ID，MCP 重启后仍可追踪/重新下载。
4. 在受控条件下人为使**首次 SFTP 下载失败**，保留远端成功文件，恢复 SFTP 后再次调用 **同一 `export_id`** 的 `carbon_export_status`；记录 SQL 执行次数不增加，最终内容完整，若实现支持还应验证原子落盘。
5. 验证取消已结束/正在导出任务时终态一致，远端仅清理自己创建的临时文件。
6. 记录同文件名覆盖行为（若存在）和本地可用磁盘不足时的失败方式；磁盘故障可通过本地隔离测试模拟，禁止填满共享盘。

**硬性 PASS**：在稳定样本上文件内容正确、成功标志不提前、重复状态查询不会重新执行 SQL、可恢复的下载失败不会丢失任务。

## 9. G6 — 真实性能与资源占用基准

### 9.1 测试规模与边界

从 G0 的可用样本选择合适规模，建议三档：小规模（约 100 行）、中规模（约 1,000 行）、较大规模（约 10,000 行）；如获准测试表不足则实际规模为准，**不为了凑行数构造高负载笛卡尔积**。

建议每个固定 SQL 预热 1 次、正式重复 5 次；生产环境时间/配额不允许时减少重复并说明。并发只测 `1`、`2` 两档起步；更高并发需在允许的范围内谨慎增加。

### 9.2 必须测量与区分的指标

| 指标 | 说明 |
|---|---|
| End-to-end latency | 从 MCP 客户端调用到取得结果/文件可用 |
| SSH connection/setup | 连接、认证、会话初始化的时间；无法分离则报告未采集 |
| Beeline startup / query runtime | 实际远端执行时间与客户端总耗时的差异 |
| SFTP download | 远端文件传输耗时、总字节数、有效 MiB/s |
| Parse / serialization | DSV/CSV 解析与 JSON 序列化时间 |
| Peak memory (local) | 本地进程峰值内存，明确 `tracemalloc` 与 RSS 的口径区别 |
| Output size / row throughput | 实际记录数、结果字节数、行/秒 |
| Concurrent success | 同时运行的任务数、成功率和错误类型 |
| Resource cleanup | 任务完成后的 `.part`、远端临时目录、进程残留情况 |

可使用当前 `scripts/benchmark.py` 做离线解析基准，**必须单独**创建真实环境基准记录。不能把合成结果当成端到端 Beeline 结果。

对 G2/G5 等正确性失败的场景，不允许以“吞吐量更高”掩盖失败。对相同 SQL/过滤器/列投影/环境做至少中位数对比；只有具有同口径旧版结果时才计算“优化前后增益”。

如发现全量查询在 MCP 端截断后才返回，请量化网络字节数、耗时及 `max_output_bytes` 影响，给出数据级别与复现 SQL；**先报告，不要擅自大改 SQL 生成逻辑或破坏旧接口**。

## 10. G7 — 缺陷定位、最小修复与回归

对 G1–G6 中每个失败项，建立条目：

```text
Issue ID:
Severity: P0 | P1 | P2
Environment/commit:
Connection/test dataset (redacted):
Reproduction steps:
Expected:
Actual:
Evidence paths:
Root cause / confidence:
Fix files:
Regression test:
Live retest result:
Residual risk:
```

- 优先修复：字段/行数错误、成功/取消竞态、断线结果丢失、下载重复执行、SQLite/文件持久化不一致。
- 其次处理：同步查询浪费输出带宽、过大结果内存占用、明显连接开销。
- 不在此阶段实现 `carbon_execute`、中间表写入、账号权限管理或完整 SQL 编译器。
- 每个修复后至少执行 `python -m pytest -q`、`python -m compileall -q src tests`、`git diff --check`；受影响的 live 用例必须重新实际运行（可运行时）。
- 发生真实环境不可复现异常时，保留明确的失败/未验证证据，而不是修改检查条件让其通过。

## 11. G8 — 交付物与报告格式（必须生成）

优先新增以下文件；若当前仓库已有同类文件，复用并保持一致：

1. `docs/live-acceptance.md`：**最终完整验收报告**，包括概要、测试日期、版本 SHA、真实环境前置条件、每门禁结果、故障与剩余风险、结论。
2. `docs/live-acceptance-runbook.md`：可照做的环境准备、运行、重跑、重启恢复、下载失败恢复、清理步骤（Linux/Windows 差异需要说明）。
3. `scripts/live_acceptance.py` 或等价小型验收 runner：区分 `preflight` / `smoke` / `correctness` / `jobs` / `exports` / `benchmark` / `all`；可选择只读连接别名、样本表和测试范围。若已有工具足够，可只增加薄封装，禁止为测试重建一套产品系统。
4. `tests/` 下必要的回归与本地故障注入测试；live 测试应显式 opt-in（例如 `--live` 或单独命令），普通 `pytest` 不能未经许可访问真实集群。
5. `.acceptance-data/live-<run-id>/`（应加入 `.gitignore`，敏感原始证据仅本地保存）：结构化 JSON、性能 CSV/JSON、脱敏日志、查询/任务 ID 映射、数据差异摘要、环境诊断和必要哈希。
6. 若修复了源码，附 `git diff --stat`、修改原因和受影响测试；不修改无关代码。

**验收报告建议模板：**

```markdown
# Carbon-Lens Live Environment Acceptance

- Date / run_id:
- Branch / commit SHA / dirty state:
- Environment: OS, Python, Carbon/Fiber versions (if detectable)
- Connection: <alias only, redact host if needed>
- Test dataset: <authorized identifier, no sensitive rows>
- Final Result: PASS | PARTIAL | BLOCKED_ENV | FAIL

## Gate Results
| Gate | Result | Case Count | Evidence | Notes |
|------|--------|------------|----------|-------|
| G0 | ... | ... | ... | ... |
...

## Correctness
- Native Beeline vs MCP mismatched cells: ...
- Missing columns / rows: ...
- Truncation and unknown-total behavior: ...

## Reliability
- Async IDs / states / restart recovery: ...
- Cancel/race / timeout / disconnect: ...
- Export resume without SQL rerun: ...

## Performance
- workload / concurrency / sample count:
- p50 / p95 / peak RSS / bytes / throughput:
- comparability caveats:

## Defects & Fixes
- ...

## Unverified / Blocked
- Item, reason, prerequisites, exact rerun command:

## Conclusion
- Current readiness and next smallest action:
```

每条门禁证据必须能关联到具体运行 ID、提交 SHA、执行命令/工具调用、时间戳和测量结果。日志可脱敏，但不能只保留人为口头描述。

## 12. 可执行步骤（直接开始，不只输出规划）

按顺序完成：

```text
Step 1  审阅仓库、建立验收 case matrix、记录 git branch/SHA/status。
Step 2  执行本地基础回归并保存完整结果。
Step 3  G0 preflight：检测配置/网络/凭据是否就绪（不显示凭据内容）。
Step 4  若 G0 PASS：调用真实 MCP + 原生 Beeline 完成 G1–G2。
Step 5  执行 G3–G5；有扰动的步骤先验证允许范围和隔离性。
Step 6  用同一批获准 SQL 执行 G6，保存测量原始记录。
Step 7  对可复现缺陷最小修复、追加回归、复跑相关 live 用例。
Step 8  输出 runbook、完整报告、结构化证据与 Git 状态。
```

**必要命令示例（按实际平台调整）：**

```bash
python -m pytest -q
python -m compileall -q src tests
git diff --check
python scripts/benchmark.py --rows 100,1000,10000 --repeat 5
```

说明：`scripts/live_acceptance.py` 是本任务的**拟新增交付物**，不是现有仓库已确认存在的脚本。只有实际实现后才可在报告中列出和使用其 CLI 命令。

若当前运行环境无法接入实际 Carbon 集群，**不准停止在空泛的建议阶段**：照样完成 preflight/runner/runbook/本地回归，产出详细的 `BLOCKED_ENV` 报告，并提供以后在有环境机器上一条命令复跑的明确说明。

## 13. 最终向我回复的格式（中文）

```text
Carbon-Lens 真实环境验收结论：PASS / PARTIAL / BLOCKED_ENV / FAIL

1. 实际连接到的层级：MCP、SSH、Kerberos、Beeline、SFTP（逐项说明）
2. G0–G8：每项结果、用例数、关键证据路径
3. 数据正确性：原生 Beeline 对比、字段/行数差异、截断契约
4. 稳定性：长任务、重启恢复、取消竞态、下载失败重试
5. 性能：各档耗时、峰值内存、传输量及可比性
6. 修复：改动文件、回归用例、新增测试及通过数量
7. 未验证项：具体阻塞原因和准确的复跑命令
8. Git：当前分支、HEAD SHA、git status --short、git diff --stat
9. 交付文件：docs/live-acceptance.md 等
10. 是否可结束真实环境验收，以及下一步仅需做什么
```

**最终要求：工程证据优先，正确性与稳定性优先，不增加不必要的安全框架。若真实环境不可用，如实保持未验证状态；绝不能为了交付而制造真实连接成功、吞吐量提升或故障恢复的虚假证据。**
