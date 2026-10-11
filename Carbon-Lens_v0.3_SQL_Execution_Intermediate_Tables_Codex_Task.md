# Carbon-Lens v0.3 — SQL Execution & Managed Intermediate Tables

> **交给 Codex 执行的工程开发任务书**  
> 仓库：<https://github.com/shutu-ai/Carbon-Lens>  
> 目标分支：基于最新 `origin/main` 创建工作分支；先确认工作区状态，不覆盖用户未提交改动。  
> 核心要求：**原始数据表永久只读；只有由 Carbon-Lens 创建、登记、确认所有权的中间表允许写入、覆盖和删除。**  
> 优先级：**P0 数据保护 > P0 执行正确性与恢复 > P1 可用性 > P2 扩展优化**。

---

## 0. Codex 执行指令

你是本仓库的开发工程师。**不要仅出方案；直接检查最新仓库、实施代码、补齐测试、运行回归，并提交证据报告。**

执行前：

1. `git fetch`，报告实际 HEAD SHA、分支、工作区状态，核实已经存在的能力；不得根据本任务书对旧版本的描述盲目修改当前代码。
2. 阅读 `README.md`、`docs/future-write-capability.md`、`docs/live-acceptance-closure.md`，以及 `src/carbon_lens/server.py`、`service.py`、`beeline.py`、`jobs.py`、`exports.py`、`config.py`、相关测试与 Skill。
3. 检查 `origin/main` 当前代码和测试。旧有的 12 个 MCP 工具、查询、描述、CSV/JSON 导出、异步任务、SQLite 持久化和跨进程恢复必须保持兼容。
4. 以本任务书中的 P0 数据保护边界为**硬约束**。发现现有设计与之冲突时，应修改实现，不得降低约束或通过文档提示 Agent“谨慎操作”代替服务端拦截。
5. 默认允许本地实现和非破坏性测试。若缺少明确配置的独立工作区或真实环境条件，标记对应端到端验收为 `BLOCKED_ENV`；绝不通过直接写生产原始数据表来完成测试。
6. 工作过程中修复必要缺陷，避免无关重构；除非明确授权，不自动执行 Git push、发布 Release 或清理其他人的 Git 改动。

### 本期明确不做

- 不开发账号系统、RBAC、多租户、审批、复杂审计平台或安全网关。
- 不要求 C5（与原生 Beeline 对账）和 C6（性能基线/SLA）；这两项永久列为本轮 **`WAIVED / OUT OF SCOPE`**，既不算 PASS，也不阻塞交付。
- 不负责优化底层 CarbonData/Spark 的运行速度、计算吞吐或资源调度。
- 不强行兼容 Carbon 引擎未支持的 SQL 方言。
- 不创建通用 Shell MCP 工具；不以 Shell 命令、SFTP 上传脚本、`SET` 或 UDF 途径提供绕过 SQL 写保护的入口。

---

## 1. 产品定位与硬约束

Carbon-Lens 是可信使用环境下、面向 Codex / AI Agent 的 **CarbonData 数据操作 MCP 工具**。

期望能力：

1. 对已有生产/业务原始表自由执行**读取、联表、过滤、聚合、查询和只读导出**。
2. 在明确配置的独立工作区（workspace schema/database）内创建 Carbon-Lens 管理的中间表，例如 `CREATE TABLE ... AS SELECT ...`。
3. 只对上述**已登记且所有权已确认**的中间表进行 `INSERT INTO`、`INSERT OVERWRITE`、清理和删除。
4. 让大数据 SQL 可以长期异步执行，保持任务状态可追踪、进程重启可恢复、失败可诊断。
5. 让 Agent 通过简单稳定的 MCP 接口使用这些功能，不需要复杂的授权与审批流程。

### P0 不可妥协的规则

**R1. 原始表永久只读。** 所有已经存在的原始业务表、第三方表、其他程序创建的表均禁止 `INSERT/OVERWRITE/UPDATE/DELETE/ALTER/TRUNCATE/DROP/RENAME/REPLACE`。同一限制也适用于工作区内**未登记**的表。

**R2. 仅允许自己创建且确认归属的中间表被写入。** 表名以 `tmp_` 开头、落在工作区 Schema、Agent 声称“这是我创建的”均**不足以**证明归属。必须在持久注册表中有可核验的创建记录和最终确认状态。

**R3. 创建只能创建全新的独立表。** 不允许 `CREATE OR REPLACE`、`CREATE TABLE ... LIKE ...` 写入既有对象、`CREATE IF NOT EXISTS` 来接管别人已有表、重命名接管、建外部表到既有目录或显式指定外部 `LOCATION`。目标表必须是工作区内由工具生成/批准的全新、全限定唯一名称，创建前确认不存在；使用碰撞极低的唯一 ID，并在执行后核验存在性和身份。

**R4. 所有写 SQL 经服务端统一校验。** MCP、Skill、同步、异步、重试、恢复、内部调用路径均不可绕过。不能依赖正则表达式或首关键字作为唯一防线；应采用可处理实际 Carbon/Hive/Spark 方言的 AST/语法分析与**严格允许列表**，解析不明的可变更语句直接拒绝。不能精确识别写入目标就拒绝，不可自动降级为直接执行。

**R5. 无法确定真实写入结果时绝不自动重提。** SQL 超时、SSH 断连、服务崩溃、状态文件丢失时，必须先恢复或核对原远端执行；如果无法判断，标记 `unknown` / `reconciliation_required`，保留证据和原任务 ID，不可重新提交 `INSERT`、`CTAS` 或删除操作。

**R6. 原始表保护适用于间接破坏路径。** 不允许多语句脚本、注释/引号绕过、`USE` 改变解析作用域、`CREATE VIEW`/`REPLACE VIEW` 影响原对象、`MSCK`、`LOAD DATA`、`INSERT OVERWRITE DIRECTORY`、外部路径写入、系统命令、元数据修改、动态 SQL/存储过程或其他未明确审过的副作用语句。只支持本期白名单内的精确语法子集，其余拒绝。

**R7. 连接或平台配置缺失时 fail closed。** 没有 `workspace_schema`、注册表不可访问、任务归属不明确、Schema 解析不明确或存在命名冲突时，写入接口拒绝提交，但老的只读查询/导出不受影响。

> 提醒：应用层 SQL 校验不能在拥有任意数据库写权限的账号下，对所有可能的驱动/外部副作用给出绝对物理隔离保证。可选在数据库环境中将业务 Schema 设为只读、仅给工作 Schema 写权限作为额外边界；**不要求开发账号或 RBAC 体系**。无此环境隔离时要在报告中如实披露剩余边界，不得宣称“绝对物理安全”。

---

## 2. SQL 支持范围（v0.3）

### 2.1 必须支持

| 操作 | 来源表 | 目标表 | v0.3 策略 |
| --- | --- | --- | --- |
| `SELECT` / `WITH ... SELECT` / JOIN / 聚合 | 原始表或中间表 | 无 | 走原有只读查询接口 |
| `DESCRIBE` / `SHOW` | 原始表或中间表 | 无 | 保留原接口与语义 |
| `CREATE TABLE <workspace>.<new_table> AS SELECT ...` | 允许读取原始表 | 新的本工具管理表 | 允许，需经过创建预留、目标不存在和 SQL 校验 |
| `INSERT INTO <workspace>.<owned_table> SELECT ...` | 允许读取原始表 | 已登记、状态为 active 的中间表 | 允许 |
| `INSERT OVERWRITE TABLE <workspace>.<owned_table> SELECT ...` | 允许读取原始表 | 已登记、状态为 active 的中间表 | 允许，必须显式标识 overwrite 类型 |
| `DROP TABLE <workspace>.<owned_table>` | 不适用 | 已登记、可验证归属的中间表 | 允许，必须与注册状态联动 |

可按实际 Carbon 方言调整精确语法，但不能放宽目标表所有权规则。若 `INSERT OVERWRITE` 或其他操作在实际 Carbon 客户端不支持，保留扩展接口并明确返回 `UNSUPPORTED_DIALECT`；不准伪报可用。

### 2.2 不必在 v0.3 实施

- `UPDATE`、`DELETE`、`MERGE`、`ALTER TABLE`、`TRUNCATE`、多语句事务、跨连接工作区共享、自动中间表 TTL 删除。
- 如果实现以上任一功能，也必须套用与 INSERT/DROP 完全相同的目标归属检查、幂等和真实环境验收；否则明确拒绝，不能透传。

### 2.3 仅供说明的 SQL（真实表名必须由系统分配并验证）

```sql
-- 读取原始表，向新的中间表写入结果
CREATE TABLE carbon_lens_workspace.cl_tmp_a1b2c3d4 AS
SELECT msisdn, SUM(traffic) AS traffic_total
FROM business_schema.source_traffic
GROUP BY msisdn;

-- 对已登记中间表追加
INSERT INTO TABLE carbon_lens_workspace.cl_tmp_a1b2c3d4
SELECT msisdn, traffic_total FROM business_schema.daily_summary;

-- 只删除 Carbon-Lens 创建、登记且确认归属的中间表
DROP TABLE carbon_lens_workspace.cl_tmp_a1b2c3d4;
```

不要把样例中的 `business_schema` 或表名当成真实测试目标。

---

## 3. 实现方案（要求最小侵入）

### 3.1 新增独立写操作通道

保留现有 12 个工具及其参数和行为。新增通用执行接口，名称建议：

- `carbon_execute(connection, sql, ..., idempotency_key=None)`：快速语句可同步完成。若处理耗时不确定，可明确返回需要异步的错误/提示，而不是让 MCP 请求长时间卡住。
- `carbon_execute_start(connection, sql, idempotency_key=None, timeout_seconds=None)`：提交异步写任务，立即返回 `execution_id`。
- `carbon_execute_status(execution_id)`：状态和最终结果。
- `carbon_execute_list(status=None, limit=50)`：任务清单。
- `carbon_execute_cancel(execution_id)`：请求取消；不等于“数据库保证回滚”。
- `carbon_workspace_list(connection, ...)`：仅列本工具登记管理的中间表及状态。实际名称可按仓库规范调整。

SQL 接口是给可信 Agent 的便捷入口，**不是原生 SQL 直接透传入口**。服务端强制做分类、Schema 限定、所有权核验、幂等与持久状态管理。建议将 `carbon_execute` 中 CREATE 的目标表名改为服务端生成并返回，并由结构化参数指定源 SQL；若保留完整 CTAS SQL 入参，服务端仍须验证名称在本次操作的预留记录中绑定且不允许自选既有目标。评估两种形式并选择**最简单且不牺牲约束**的实现，记录理由。

所有读操作仍使用原有 `carbon_query` / `carbon_export`；禁止为了支持写 SQL 而放宽它们的只读校验。

### 3.2 独立工作区配置

引入可选配置，如：

```json
{
  "write_workspace": {
    "enabled": false,
    "schema": "carbon_lens_workspace",
    "table_prefix": "cl_tmp_"
  }
}
```

- 配置键名和所在层级以仓库现有 ConfigStore 结构为准；向后兼容，**默认关闭写入**。
- `schema` 必须是显式、全限定解析的独立命名空间；不得默认使用连接的 `default` 数据库。
- 不自动创建、删除或修改 Schema/Database；启用前预检其存在性和可写性。
- 单连接单工作区先实现即可；连接间不得混用注册表记录。
- 关闭写入开关不影响既有查询和异步导出。
- 避免对生产运行环境隐式做破坏性元数据变更。

### 3.3 中间表登记与生命周期

基于现有 SQLite 机制增加 `managed_tables`（或等价结构），最少包括：

`table_id, connection, workspace_schema, qualified_name, create_execution_id, create_sql_fingerprint, state, created_at, activated_at, dropped_at, last_verified_at, error`

推荐状态：

```text
reserved -> creating -> active -> dropping -> dropped
                    \-> create_unknown / create_failed
active -> drop_unknown / drop_failed
```

关键约束：

1. **先持久化 reservation 和幂等键，再发出远程 CREATE**；表名唯一，不得使用自选已有表名做“领养”。
2. CREATE 完成且验证对象身份/归属后才转 `active`。尚在 `creating`、`create_unknown` 等状态的表不可任意 INSERT/DROP。
3. 所有写入目标必须与 `(connection, workspace_schema, qualified_name)` 注册记录完全匹配且 `state=active`，并检查真实对象未被其他系统替换或重建；无法确认时阻断写入，返回可诊断原因。
4. `DROP` 成功后以原子/可恢复方式转入 `dropped`；断网时不要盲目宣称删除成功。若可能用专用表属性或元数据标记绑定 `table_id`，应验证 Carbon 方言支持，否则采用严格“未知即拒绝”的策略。
5. 注册表和任务库迁移必须向后兼容、可重复执行；重启后不会误把未经本工具创建的表归为管理对象。
6. 清理只能清理注册的表；“过期清理”首期可以只给出显式命令，不自动执行 `DROP`。
7. 不对已存在工作区表使用 `CREATE IF NOT EXISTS` 接管，不允许手工修改 SQLite 注册表后就视为创建证据。

### 3.4 SQL 分类及校验组件

新增可测试、与执行器解耦的 `SqlPolicy` / `SqlPlan`（命名自定），输出示例：

```json
{
  "kind": "insert_into",
  "connection": "...",
  "target": "carbon_lens_workspace.cl_tmp_xxxx",
  "source_tables": ["business_schema.source_traffic"],
  "mutates_data": true,
  "requires_owned_target": true,
  "validation_status": "allowed"
}
```

实现原则：

- 支持 SQL 引号、注释、大小写、别名、CTE、换行和字符串中的分号的正确解析；只允许 **恰好一条 SQL**（末尾可有单个终止分号）。
- 识别实际写目标，强制展开或禁止不确定的 Schema 解析；谨慎处理 `INSERT INTO TABLE`、`INSERT OVERWRITE TABLE`、多层 CTE、子查询中的同名表和方言变种。
- 对允许的 CTAS/INSERT/DROP，严格比对操作类型与目标，拒绝混合语句和 SQL hints/动态执行形式中不可理解的副作用。
- 禁用影响执行上下文的多语句 `USE`, `SET`, `ADD JAR`, `CREATE FUNCTION` 等操作。
- 不允许写往 `LOCATION`、目录、视图、临时函数或外部存储；不允许 `CREATE TABLE ... STORED AS ... LOCATION` 这类未经审核的写入变体。
- AST/解析器选择应有真实 Carbon SQL 方言适配验证；不能解析时拒绝并返回 `UNSUPPORTED_SQL`，不得把字符串关键字替换当成可验证解析。
- 不额外触碰旧的读查询路径；读取业务表应继续受支持。

### 3.5 统一任务生命周期和重复执行保护

复用已有远端 detached 任务、SSH/Kerberos/Beeline、状态/PID/日志、SQLite、本地进程重启恢复能力。不要独立重新造一套不兼容的执行机制。

要求：

- 提交时持久记录 `execution_id`、`idempotency_key`、SQL 指纹、工作区表 ID、SQL 类型、远端目录、时间、状态和最终错误。
- 同一幂等键及相同请求返回原任务；同一幂等键但不同 SQL/连接/目标必须拒绝冲突。
- 未收到远程启动确认不代表未执行；重连时核对唯一远端任务路径/状态，不允许另建远端任务重放写 SQL。
- 恢复时只**观察已有**作业，不因进程 A/B 切换再次执行 CTAS/INSERT/DROP。
- 终态判定以可靠的远端状态为准；客户端超时、取消请求和网络错误不应抢先覆盖已成功终态。
- 取消请求不等于写操作回滚；返回明确的 `cancel_requested`、`cancelled`、`succeeded` 或 `unknown` 语义。
- 若实际执行状态无法证明，返回 `reconciliation_required`，不自动补偿，不自动再次 INSERT。
- CTAS 与 DROP 的执行结果应联动 `managed_tables` 注册表；重启后须能继续完成状态结算。
- 不应将 SQL 行集结果结构（`columns` / `rows`）强加于 DDL/DML。

建议命令型结果至少有：

```json
{
  "execution_id": "cx_...",
  "operation": "create_table_as_select",
  "status": "succeeded",
  "connection": "...",
  "target_table": "carbon_lens_workspace.cl_tmp_...",
  "affected_row_count": null,
  "affected_row_count_known": false,
  "elapsed_ms": 12345,
  "error_kind": null,
  "remote_log_reference": "..."
}
```

`affected_row_count` 不可从无证据的输出中猜测。错误中不得回显密码、keytab、SSH 密钥或完整连接配置。

### 3.6 Skill 与使用文档

更新 `skills/carbon-analysis` 和 `README.md`：

- 优先读查询；需要数据加工时，创建专用中间表，然后增量加工，最终可显式删除中间表。
- 强调原始表永久只读，工作区未知表也不可写。
- 给出正常创建、插入、查询中间表、删除中间表的完整调用范例；示例使用虚构的 Schema/表名，不嵌入真实业务敏感信息。
- 说明任务 ID、幂等键、断线后的状态恢复步骤以及 `unknown` 时应停止重试并排查。
- 描述 SQLite 数据迁移、旧配置兼容、工作区启用开关和常见失败码。
- 不要把“Agent 应当小心”写成替代服务端验证的唯一措施。

---

## 4. 验收门禁（G0–G9）

### G0 — 基线、代码审计与实施计划（P0）

- 确认最新 HEAD、已有 12 个工具、已有测试数量、现有任务表结构；识别实际 Carbon SQL 方言及编译器兼容性。
- 输出简短 `Implementation Plan`，再开始改代码；不要为等待用户确认而停止能够安全执行的本地工作。
- 不覆盖现有未提交内容或机密配置。

### G1 — 基本功能与兼容性（P0）

- 12 个旧工具回归正常、入参和响应向后兼容。
- 写入开关缺省关闭；只读查询和导出在缺少工作区配置时仍正常。
- 新 MCP API 能列出、提交、查询状态、取消和列出任务。

### G2 — 原始数据表零修改保护（P0，硬门禁）

必须用**不执行远端破坏性 SQL**的单元/集成测试证明：

| 测试输入/场景 | 预期 |
| --- | --- |
| `INSERT INTO business_schema.original_table ...` | 拒绝 |
| `INSERT OVERWRITE TABLE business_schema.original_table ...` | 拒绝 |
| `UPDATE / DELETE / MERGE` 原始表 | 拒绝 |
| `DROP / ALTER / TRUNCATE / RENAME` 原始表 | 拒绝 |
| `CREATE OR REPLACE TABLE business_schema.original_table ...` | 拒绝 |
| `CREATE TABLE IF NOT EXISTS` 指向既有或不明归属表 | 拒绝 |
| 工作区存在 `tmp_existing`，但 SQLite 无所有权记录 | 拒绝修改/删除 |
| 仅有 `cl_tmp_` 前缀但未登记表 | 拒绝 |
| `SELECT 1; DROP TABLE ...` | 拒绝整段 |
| SQL 注释、多余分号、引号、混淆空白、大小写变体 | 不可绕过 |
| CTE/嵌套查询中的 INSERT/OVERWRITE 变体或 parser 无法理解的语法 | 拒绝 |
| `USE`, `SET`, `LOAD DATA`, `INSERT OVERWRITE DIRECTORY`, 外部路径写入 | 拒绝 |
| 指向其他连接、其他 workspace schema 的写操作 | 拒绝 |
| 注册记录丢失、错误、损坏，或者工作区配置缺失 | 拒绝写操作 |
| 恢复中的未确认 CREATE 表 | 不允许新的写/删操作 |
| 旧 `carbon_query` 收到写 SQL | 仍然拒绝 |

**通过定义：所有上述不允许的操作在调用远端执行前被拦截，且没有被上传并执行；测试要证明远程执行方法没有被调用。**

### G3 — 中间表创建与所有权（P0）

- CTAS 目标由工具确定且唯一，不覆盖任何旧表。
- 创建前数据库对象不存在且 SQLite 存在已提交 reservation；创建后核验才 active。
- 同名冲突、提交断线、创建中断、建表成功但未收到响应等情况不得接管现有表或重复创建。
- 只有 active 的已登记中间表能被 INSERT 或 DROP。

### G4 — 写入和清理功能（P0）

- 已登记中间表支持 `INSERT INTO`；引擎若支持则实现 `INSERT OVERWRITE`，否则明确返回不支持。
- 已登记中间表可显式 DROP，登记状态一致；重复 DROP 不得误删同名重建表。
- 修改原始表或其他未登记表的尝试一律拒绝。
- `UPDATE/DELETE/ALTER/TRUNCATE` 若未实现，必须在 API 层明确拒绝。

### G5 — 异步/恢复与幂等（P0）

- 长时间 CTAS/INSERT 能返回 execution ID，按 ID 查询状态。
- 独立 MCP 进程 A 提交后退出，进程 B 以相同 SQLite DB 恢复观察原任务；不能再次提交 SQL。
- 同一 `idempotency_key` + 相同请求只对应一次远端执行。
- SSH 断连、远端状态缺失、启动回复丢失、客户端超时、取消竞态分别有确定性测试。
- 未知状态不误报成功/失败/已回滚，也不自动重试写操作。
- 创建/删除任务中断时，注册表状态可重整，不进入错误的 active/dropped 状态。

### G6 — 健壮可用（P1）

- SQLite 升级、重复启动、历史任务查询、无工作区配置、状态字段缺失均可诊断。
- 避免读取整个大结果进内存；命令结果返回简洁元信息而不是强制拉取写语句输出。
- 错误码和操作反馈明确：`READ_ONLY_ORIGINAL`, `UNOWNED_TABLE`, `WRITE_DISABLED`, `UNSUPPORTED_SQL`, `IDEMPOTENCY_CONFLICT`, `EXECUTION_UNKNOWN`, `WORKSPACE_NOT_READY` 等（命名可调整）。
- 远端超时与本地 MCP 超时语义区分清楚，资源清理不删除仍在执行的作业所需状态。

### G7 — 测试与工程质量（P0）

至少包括：

- 全部原有自动化测试 PASS。
- SQL 允许/拒绝的参数化测试，包括多语句/绕过输入。
- 注册表状态机测试、SQLite 升级测试、幂等冲突测试、重启恢复测试、连接失败测试。
- Mock 必须断言被拒绝 SQL **没有触发远端 SQL 调用**。
- `python -m pytest`、`python -m compileall -q src tests`、`git diff --check` 均 PASS。
- 因环境缺失无法实际执行的项目不得伪造 PASS。

### G8 — 受控真实环境验收（P0，若具备环境）

**只允许在事先配置并确认属于 Carbon-Lens 的专用测试 workspace 中进行真正的写入验收。**

安全顺序：

1. 连通性与读取 smoke test，不修改任何表。
2. 检查测试工作区存在、配置明确、与原始 Schema 不同，并核实写入被启用。
3. 创建唯一测试中间表（CTAS，读取只读测试源或常量 SELECT）。
4. 查看本工具登记状态和表结构；查询中间表结果。
5. 仅对该新表执行 `INSERT INTO`；如方言兼容，再测试 `INSERT OVERWRITE`。
6. 跨进程重启恢复创建/写入任务，验证没有重复执行。
7. 只删除本次创建且所有权可核验的测试中间表，并记录最终清理情况。
8. 禁止通过向生产原始表真实发送 DROP/INSERT/OVERWRITE 来验证保护；此类输入只在本地 policy/mock 层断言已拦截。

如果没有获准使用的测试工作区、写入开关未开启或引擎方言尚未确认：**`BLOCKED_ENV`**。此时可以完成实现和模拟验证，但不得声称真实写入 PASS，不得擅自创建/删除真实 Schema 或将其它生产表当测试表。

### G9 — 报告与交付（P0）

输出 `docs/v0.3-sql-execution-acceptance.md`，最少包含：

- HEAD/分支/变更文件、架构改动、依赖变化。
- 每个 G0–G9 的 `PASS / FAIL / PARTIAL / BLOCKED_ENV / WAIVED` 及证据路径。
- 明确 C5、C6 为 `WAIVED / OUT OF SCOPE`。
- SQL 操作支持矩阵和禁止矩阵。
- 测试命令、测试数量、结果、重要失败/限制和实际环境测试证据。
- 无写入授权时列出哪些本地能力通过、哪些真实功能未验证。
- 剩余风险，特别是应用层防线不等同于底层数据库授权隔离。
- 真实写测试创建了哪些表、是否全部清理（仅记录本次临时表，不暴露敏感内容）。
- 更新 README、配置示例、Skill 使用说明；必要时新增数据库/架构迁移说明。

---

## 5. 建议的实现顺序

1. **Phase A — 原型与 policy**：梳理现有执行器和 Carbon SQL 方言，先实现 SQL 严格分类与拒绝测试。
2. **Phase B — workspace registry**：SQLite migration、唯一命名、reservation、active/dropping 状态和恢复校验。
3. **Phase C — command execution**：复用 SSH/Beeline、远端作业与 SQLite 生命周期，新增 MCP 执行工具及命令结果。
4. **Phase D — safe operations**：CTAS → INSERT INTO → （支持时）INSERT OVERWRITE → DROP managed table。
5. **Phase E — resilience**：幂等、丢响应、网络中断、重启、取消/超时竞争、未知状态。
6. **Phase F — 验收交付**：本地测试、受控真实工作区测试（如具备）、文档和最终报告。

每阶段应保留小范围、可回归的变更。P0 校验未完成之前，不要暴露任何真正可执行的写入 MCP 接口。

---

## 6. 失败处理与不得绕过的红线

- **不要**为让演示成功而临时关闭 SQL policy。
- **不要**把未经登记的生产表“加入注册表”后执行写入。
- **不要**因为 Agent 请求或参数里出现 `force=true`、`unsafe=true` 就跳过归属验证。
- **不要**把 `DROP DATABASE`、`DROP SCHEMA`、`CREATE OR REPLACE` 当作中间表清理手段。
- **不要**把本地任务超时解释为数据库执行失败且可安全重跑。
- **不要**在未确认的真实环境中执行破坏性测试或自动初始化写工作区。
- **不要**把配置文件、密码、keytab、真实敏感业务数据或未经清理的远程日志提交到 GitHub。
- **不要**把 C5/C6 指标重新加回验收门禁。

本期主要追求 **正确、健壮、可用、可恢复**，不是追求更严格的人员身份管理，也不是追求实验室条件下的高性能分数。

---

## 7. 最终交付格式（Codex 输出要求）

任务完成后，回复必须包含：

1. **RESULT**：`PASS` / `PARTIAL` / `BLOCKED_ENV` / `FAIL`，并解释对应的是本地工程完成还是实际写入环境验收。
2. **IMPLEMENTATION**：完成了哪些 SQL 操作、多少个新 MCP 接口，以及与旧 12 个接口的兼容情况。
3. **ORIGINAL TABLE GUARANTEE**：SQL 写目标拦截、工作区检查、所有权注册与失败关闭机制的代码/测试证据。
4. **LIFECYCLE**：幂等、断网、重启、取消与未知状态如何处理；给出测试名称和结果。
5. **TESTS**：pytest、compileall、diff-check、受控真实环境结果和未验证项。
6. **FILES / DOCS**：列出新增修改的源文件、文档与验收报告。
7. **GIT STATUS**：HEAD SHA、工作区状态、是否提交/推送（未授权不擅自推送）。
8. **LIMITATIONS**：实际 Carbon SQL 方言不支持的操作、剩余风险及后续建议。

### 整体通过定义

只有同时满足以下条件才可以宣称本期 **`PASS`**：

- 原始表零修改规则 G2 全部通过，无可绕过的直接执行路径；
- CTAS、INSERT、受支持的 managed-table 清理操作在实现和测试中能够正确运行；
- 任务持久化、幂等、恢复、防重复提交 P0 测试全部通过；
- 现有 12 个 MCP 工具回归正常；
- 若声称“真实环境写入 PASS”，还必须有经许可的独立 workspace 上的实际成功证据；否则整体应按 `PARTIAL` 或 `BLOCKED_ENV` 准确披露。

**最重要的验收标准：即使 AI Agent 生成了错误 SQL，也不能通过 Carbon-Lens 修改或删除任何原始数据表。**
