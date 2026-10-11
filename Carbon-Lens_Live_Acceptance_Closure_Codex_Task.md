# Carbon-Lens — Live Acceptance Closure（真实环境验收闭环）Codex 任务书

> 任务类型：直接实施代码修复 + 自动化回归 + 真实环境复验 + 证据交付  
> 仓库：`https://github.com/shutu-ai/Carbon-Lens`  
> 参考报告：`docs/live-acceptance.md`  
> 参考运行手册：`docs/live-acceptance-runbook.md`  
> 目标：在不做大规模重构和账号权限体系的前提下，关闭后台导出、异步任务终态/恢复、数据正确性对账、验收 runner 误判及性能证据缺口。  
> 建议工作分支：`fix/live-acceptance-closure`（如果仓库流程另有要求，以仓库为准）  
> 本任务的成功定义：**真实执行成功、数据正确、任务稳定、证据可复现**，不是“测试脚本显示绿色”。

---

## 0. 给 Codex 的执行指令（必须遵守）

你现在是 Carbon-Lens 项目的高级 Python/MCP/SSH/Beeline 工程师兼测试负责人。请直接在当前工作区进行代码审计、必要修复、自动化测试及有条件的真实环境验收，形成可检查的交付物。**不要只输出建议或方案**。

### 0.1 用户定义的产品边界

- Carbon-Lens 面向可信使用者和 AI Agent，优先保证 **功能高效可用、查询/导出正确性、长任务稳定性、易维护**。
- **本轮不引入**多用户账号管理、RBAC、ACL 审批系统、SQL 审批流、微服务化、复杂安全网关或与目标无关的过度安全设计。
- 当前真实环境验收使用只读 SQL；**只读是本轮验收范围，不是未来产品的永久架构约束**。
- 未来可能支持 `CREATE TABLE AS SELECT`、`INSERT INTO`、`INSERT OVERWRITE` 等中间表加工。现阶段保持执行层可扩展，不实现/启用写操作。
- 不破坏现有 12 个 MCP 工具、既有参数、响应结构或已有 Job ID 的兼容性，除非发现必须修改的错误；变更需写明兼容策略。
- **绝对禁止**对共享真实 Carbon 集群执行未经明确许可的故障注入、破坏性 SQL、写入/DDL、无边界并发或大范围全表扫描。此条是测试环境边界，不是要求实现通用权限管理。

### 0.2 开始前先核对 HEAD，避免重复修复

1. `git status --short`、`git branch --show-current`、`git rev-parse HEAD`、`git log -5 --oneline`。
2. 阅读 `README.md`、`docs/live-acceptance.md`、`docs/live-acceptance-runbook.md`、`docs/optimization-acceptance.md`（如存在）、`docs/future-write-capability.md`（如存在）。
3. 检查当前实际文件与接口：`src/carbon_lens/exports.py`、`jobs.py`、`service.py`、`beeline.py`、`ssh.py`、`server.py`、`scripts/live_acceptance.py`、`tests/`；如路径或结构已变化，按实际代码调整。
4. 确认现有修复是否已在 HEAD，包括后台导出超时状态分支、SQLite 结果文件化、下载失败可重试、终态竞争修复。**已正确实现的能力只补证据，不重复重写**。
5. 在 `docs/live-acceptance-closure.md` 记录 baseline SHA、发现的缺口、计划修改文件、原有测试数量。对报告与代码不一致之处，以可复现代码行为为准，并解释差异。
6. 保留工作区用户已有的未提交更改；不要 `git reset --hard`、清空工作树、修改真实凭证或擅自 push/tag/release。若需提交，按仓库工作流程操作并报告 SHA；仅在授权或明确约定时推送。

### 0.3 已知基线（只作为待核实线索，不要当作最新事实）

既有 `docs/live-acceptance.md` 报告 `PARTIAL`：真实 `SELECT 1`、100 行业务表样本查询、异步查询和同步 CSV/JSON 常量导出有成功证据；**后台 CSV 导出超过 120 秒仍 `running`，取消后长期 `cancel_requested`，没有最终下载结果**。代码做过一次本地超时路径修复，但未完成真实回归。G2 数据正确性对账、G3 跨 MCP 重启恢复、G6 基准性能仍未充分验证。

`live_acceptance.py` 既有版本中：`jobs` 模式仅启动并轮询一次异步查询；`main()` 末尾可能无条件 `return 0`；异常可能被统一折叠成 `BLOCKED_ENV`；`correctness` 模式可能只输出 `UNVERIFIED`；需逐一确认并修复仍存在的问题。

---

## 1. 范围、分级与验收规则

| 门禁 | 优先级 | 必须完成的结果 | 是否要求真实环境 |
|---|---|---|---|
| C0 基线与环境诊断 | P0 | HEAD/配置/可用连接/测试边界可追踪 | 有真实环境则执行 |
| C1 后台导出闭环 | **P0** | CSV/JSON 异步导出到本地，内容校验通过 | **是** |
| C2 任务终态与下载重试 | **P0** | 不挂起、不误报，失败后可重新下载且 SQL 不重跑 | 故障路径先本地注入，真实无损路径复验 |
| C3 跨 MCP 进程重启恢复 | **P0** | 原 Job ID、原持久化 DB、重启后的查询/导出可追踪 | 尽量真实；无隔离窗口时分别标记 |
| C4 runner 正确性 | **P0** | 退出码/阶段状态/证据真实反映 FAIL/PARTIAL/BLOCKED | 本地可完整测 |
| C5 业务数据一致性 | P1 | Carbon-Lens 与原生 Beeline 对账 | 需要被允许的测试 SQL |
| C6 性能基准 | P1 | 多次实测、耗时分布与资源使用；不臆测提升 | 需要真实环境/合适样本 |
| C7 全量回归与报告 | **P0** | 单测通过、结果可复跑、汇总状态符合证据 | 混合 |

**项目级状态定义：**

- `PASS`：所有 P0 本地代码门禁通过、需要的真实端到端关键路径已确证；G2/G6 等列出的必测数据项也已完成且通过，除明确标为可选的隔离故障演练外无未闭环必测项。
- `PARTIAL`：有可用的真实成功证据，但至少一个必测项仍 `UNVERIFIED`、`NOT_RUN` 或 `BLOCKED_ENV`；没有冒充 PASS。
- `FAIL`：实际执行的关键路径明确失败，包含后台导出失败、错误数据、终态不一致或错误地重复运行任务。**不能把产品缺陷改记为环境阻塞**。
- `BLOCKED_ENV`：没有足够环境条件开展关键验证，且没有实际产品失败证据。必须具体说明阻塞条件和复跑命令。

阶段状态允许 `PASS / FAIL / BLOCKED_ENV / UNVERIFIED / NOT_RUN / NOT_APPLICABLE`。所有状态需携带证据、执行时间、测试方法、预期与实际。

---

## 2. C0 — 环境预检与基线固定

### 实施

1. 确认 Python 解释器、包版本、MCP stdio 入口、服务配置位置、`CARBON_LENS_CONFIG`、`CARBON_LENS_JOB_DB`、`export_dir`、SSH 与 SFTP 配置、Beeline 可执行路径、Kerberos 运行前提。
2. 使用现有连接名进行最小化真实查询：`SELECT 1 AS acceptance_value`；验证 `row_count=1`、值为 1、返回无错误；保存真实运行时耗时。
3. 如需核验已有测试表，沿用报告中的 `sdr_nethouse.cloudphone_sales_users_all`，**先核实当前环境仍允许读取**；仅执行有界查询/聚合，不回显或提交 `msisdn` 等个人字段值。
4. 判断执行进程处于 Windows 本地还是 Linux/WSL，SSH 跳板及 `su`/Kerberos 的实际执行方式，区分故障发生在 SSH、Beeline、远端作业、SFTP、MCP 客户端还是 SQLite。
5. 报告中不要写入密码、密钥、keytab 内容、真实业务行、手机号、IP/主机凭证；可以记录脱敏配置状态、组件版本、错误类别和证据文件路径。

### 验收

- 本地预检失败时立即标记相应 `BLOCKED_ENV`，但继续执行不依赖真实环境的代码测试。
- 不因为 `SELECT 1` 成功就声称 Kerberos 续期、SFTP 可靠性或所有数据库均通过。

---

## 3. C1 — P0 后台 CSV/JSON 导出闭环

### 重点调查问题

报告中的现象是 `carbon_export_start` 返回 Job ID 后，远端长时间 `running`，发出取消后进入 `cancel_requested` 且迟迟无终态；没有完成下载。请沿整条路径追踪：

`carbon_export_start` → 远端 SQL/脚本提交 → Beeline 进程与状态文件 → 远端输出文件 → `carbon_export_status` → SFTP 下载 → 本地文件原子发布 → SQLite 状态与结果 → 远端清理。

请逐一验证：

- SQL/Beeline 进程是否真正启动或已退出；PID、退出码、完成标识、stderr、输出文件是否存在且稳定，状态读取是否滞后。
- `su - root`/不同执行身份下，任务控制文件、目录、SFTP 读取权限和清理身份是否一致。
- 后台超时、取消、远端已完成、SFTP 下载中、下载失败的先后顺序是否有不可达分支。
- 是否有远端任务已成功但客户端因较晚轮询而误判超时；是否存在结果尚未完成就下载、下载完未校验就删源文件的问题。
- CSV/JSON 异步路径是否具有不同实现，若 JSON 本来采用受限内存路径，应按当前接口准确验证，不为“统一”而增加无必要的流式重写。

### 最小修复原则

- 保留现有 API、参数和响应语义；只修复证明存在的根因，避免更换底层技术或整体重构。
- 状态迁移必须有确定条件：`queued/running/cancel_requested/download_pending/downloading`（以实际实现为准）→ `succeeded/failed/cancelled`；状态名可以沿用已有，不强制新增中间公开状态。
- 当真实远端终态已知时，先以真实终态和结果证据决定，不能仅依据本地 wall-clock 超时反向覆盖成功。
- 如果超时但远端终态未知，保留足够证据/恢复信息；绝不未经确认就声称 `succeeded`、删除仍在写入的输出目录或重复启动 SQL。
- SFTP 下载建议以临时文件落盘、校验后原子重命名；失败不应出现伪装为完整导出文件的残留。是否引入分块/断点续传，由实测需求决定，**至少保证重试而不重跑 SQL**。
- 远端清理在可安全清理时进行；清理失败可记录待处理，不得因为清理失败覆盖已经验证的数据导出成功。

### 真实验收矩阵

| 场景 | 预期 |
|---|---|
| CSV：有界常量查询异步导出 | `succeeded`，本地 CSV 可打开，表头和值正确 |
| JSON：有界常量查询异步导出 | `succeeded`，JSON 可解析，键和值正确 |
| CSV：批准的 100 行测试数据 | 行数、列名、字段位置正确；不保存敏感样本到 Git |
| 下载失败后恢复 | 相同 `export_id` 重试成功，SQL 提交计数不增加；在隔离 Mock/测试实例注入 |
| 远端成功、较晚轮询 | 不误判为超时失败；无需第二次执行 SQL |
| 下载过程中 MCP 退出后恢复 | 可用原 `export_id` 恢复，不出现损坏的最终文件 |

**C1 PASS 的最低条件**：真实集群上异步 CSV 和 JSON 各有一次完整成功运行记录，包括提交、终态、文件存在、文件解析、行列验证、必要的任务清理证据。若某格式实际不支持异步，应明确接口契约并调整相应设计，而不是伪造 PASS。

---

## 4. C2 — P0 长任务状态机、取消竞态、超时与重试

### 必测状态竞争

至少加入确定性测试（使用可控 clock/fake transport/fake filesystem；禁止用长时间 `sleep` 碰运气）：

1. 远端已 `SUCCEEDED`，本地等待时间已超过软/硬超时阈值。
2. 先请求 `cancel`，远端同时自然执行成功；最终报告应根据可验证远端终态处理，不应一律 `cancelled`。
3. 已发送取消，但终止命令失败或远端仍运行；持续观察后可正确进入终态或明确保持未决。
4. 远端 `FAILED` 与本地超时并发；保留实际错误证据而不是泛化超时。
5. MCP 客户端调用超时/网络短断后重复查询 `status`，不重新执行 SQL。
6. SFTP 第一次下载失败、第二次成功；远端结果仍在，且只发生一次 SQL 提交。
7. 远端文件缺失、损坏、零字节（零行结果除外）及下载前后尺寸变化，给出正确错误状态与可诊断信息。
8. 连续重复 `status/list/cancel` 调用具有幂等性；已终态的任务不会被更晚的状态请求改写为 `running`。
9. 导出成功与远端清理失败同时发生时，数据成功状态与清理警告明确分离。

### 必要的运行指标

至少记录并脱敏输出 `job_id/export_id`、提交时间、远端执行耗时、发现终态时间、SFTP 下载耗时、总耗时、状态转换轨迹、远端 SQL 启动次数、重试次数及失败类型。不能把观测指标实现成频繁远程轮询造成明显额外负载。

---

## 5. C3 — P0 真正的跨 MCP 进程重启恢复

**注意：仅调用 `start` 与 `status` 并不能证明“重启恢复”。必须是新的 MCP 服务进程。**

### 必做测试（两个独立进程）

1. 由测试 runner 启动 MCP 进程 A，并指定固定的隔离测试 DB，例如 `.acceptance-data/<run-id>/jobs.sqlite3`。
2. 用 A 调用 `carbon_query_start` / `carbon_export_start`；保存真实返回的 `query_id` / `export_id`，持久化 DB 路径与已提交任务的远端标识。
3. 关闭进程 A（优先优雅退出；需要 crash 测试时仅在隔离环境执行），**不要**删除 DB、远端控制文件或人为重新提交 SQL。
4. 启动全新 MCP 进程 B，指向相同配置与 DB，调用原 ID 的 `carbon_query_status` / `carbon_export_status`。
5. 验证 B 能找到并继续跟踪，最后拿到相同任务的终态和结果；使用远端日志/作业标识验证 SQL 没有再次提交。
6. 继续覆盖“重启前已完成但未下载”与“下载中进程终止”的场景（本地模拟或隔离环境）。
7. 检查任务列表、保留期清理与 SQLite 结果文件路径在进程重启后仍然有效，不能因相对路径或临时目录被清理而丢失。

### 测试可靠性要求

- 若常量 SQL 在关闭进程 A 前立即完成，也可以验证“已完成任务的跨重启恢复”，**但不能代替运行中任务恢复**；两种场景须分别记状态。
- 长任务测试只能使用可控、有限资源的测试场景；共享集群无法获得适当运行窗口时将运行中重启演练标记为 `BLOCKED_ENV/UNVERIFIED`，并确保本地确定性集成测试覆盖。
- 不允许以当前进程关闭客户端连接但未重启服务器来冒充重启验收。

---

## 6. C4 — P0 修复 live acceptance runner 的可信度

### 目标文件

`scripts/live_acceptance.py` 与必要的测试文件、`docs/live-acceptance-runbook.md`。

### 必须修复的逻辑

1. `--mode jobs` 必须能选择执行真实“两进程重启恢复”，例如独立模式 `restart` 或明确 `--with-restart`；默认行为和耗时变化须在帮助及 runbook 中说明。
2. 改正 `main()` 最后无条件返回 0 的问题。**建议退出码约定**（如仓库已有约定则遵循现有、但要可机器判定）：
   - `0`：请求执行的全部适用检查 `PASS`；
   - `1`：存在实际执行失败 `FAIL`；
   - `2`：存在阻塞或未完成 `BLOCKED_ENV/UNVERIFIED/NOT_RUN`，且不存在 `FAIL`；
   - `3`：runner 本身输入/内部错误（可选，若使用须说明）。
   - `NOT_APPLICABLE` 只适用于运行模式明确排除的项目，不允许借此吞掉未测必测项。
3. 区分“环境不可用”和“应用实际失败”。例如 Kerberos 凭证不存在可 `BLOCKED_ENV`；凭证正常且真实异步导出超时或内容错误，必须 `FAIL`，不能一律捕获 `Exception` 记成 `BLOCKED_ENV`。
4. `poll()` 到轮询截止时应保留最后一次可观察状态、任务 ID、时间、错误类别，并明确是 `UNVERIFIED` 还是实际功能 `FAIL`；不能仅以异常字符串替代关键证据。
5. 完成结果不仅检查 `status=succeeded`，还必须检查有效的文件路径、存在性、可读取性、格式解析、期望字段与行数/值。对 `cancel_requested` 超时不允许标 `PASS`。
6. 对正确性测试不再只输出 `UNVERIFIED`：具备原生 Beeline 对照输出/批准的预期值时自动判断；缺少基线时明确记 `UNVERIFIED`。
7. `--mode all` 应只执行当前环境允许的无损轻量用例；任何较重数据量、故障注入或生产影响操作必须明确 opt-in，不能因为“all”自动触发。
8. 默认结果保留在 `.acceptance-data/live-<run-id>/results.json`，按阶段记录可复跑命令、运行时间、基线 SHA、配置摘要、输入 SQL 摘要（必要时哈希）、结果哈希/行数、失败类型和证据路径。避免把原始业务数据、密钥、敏感日志复制到 Git。
9. `results.json` 中机器可读地提供项目级 `overall_status`、`exit_code`、`gates`、`environment`、`baseline_sha` 和 `summary`；在 stdout 打印简短人类可读汇总与证据位置。
10. 对所有退出码与混合状态编写单元测试：全通过、一个 FAIL、一个 BLOCKED_ENV、一个 UNVERIFIED、preflight 失败、runner 参数错误、某步骤异常但其他独立步骤仍可运行。

### 通过标准

- 用人为构造的 `FAIL` 验收记录运行 runner，shell 中确实出现非零退出码；真实缺陷绝不输出整体 PASS。
- `results.json` 的机器状态与 `docs/live-acceptance-closure.md` 人类总结一致。
- 可独立运行 `smoke/jobs/restart/exports/correctness/benchmark`（名称以实际实现为准），并生成互不覆盖的证据目录。

---

## 7. C5 — P1 数据正确性对账：Carbon-Lens vs 原生 Beeline

### 基本原则

同一个连接、同一份 SQL、尽可能相同的时点及会话设置，对比 Carbon-Lens 和原生 Fiber-Beeline。

### 先准备可重复、低成本的 SQL 用例

- `SELECT 1 AS acceptance_value`：基础数值与列名。
- 只在有授权的表上进行 `DESCRIBE`、明确限制行数的 `SELECT`、按日期/区域等低基数字段的聚合计数。
- 测试 `NULL`、空字符串、Unicode、换行/引号、重复列名（可用常量 SQL 构造，避免依赖业务表）。
- 不使用不带排序的 `LIMIT 100` 做逐行跨运行对账，因为行集可能随执行计划或表更新变化；改用有确定排序的测试 SQL，或针对非排序聚合/集合做规范化比较。
- 对无法确定稳定键或可读范围的业务数据，只做元数据/聚合核验，并记录缺失的前置条件。

### 核对维度

1. 原始列数、唯一化后的字段名映射、列顺序与字段类型（如可用）。
2. 行数及 `returned_row_count/truncated/has_more/total_row_count` 的语义；未知总行数不能伪造为已知。
3. `NULL`、空值、特殊字符、长文本、日期/浮点格式的保真度。
4. CSV/JSON 格式的行列一致性、编码、表头、数据类型声明；明确 JSON 原始字符串与类型转换规则。
5. 行级稳定对账优先比较规范化排序后的哈希，并记录哈希算法、比较规则、数据版本/运行窗口；仅用 count 相等不能证明内容一致。
6. 对于截断用例，验证“已返回多少”和“是否确定存在更多数据”不被误写为总行数；无法确认则显式 unknown。

### 隐私与证据

业务样本只允许保存在本地忽略目录，优先保留聚合数、字段/行数、匿名化哈希和差异统计；不要将 `msisdn`、用户记录及连接凭证上传 GitHub。

---

## 8. C6 — P1 真实性能基准与瓶颈归因

### 基准范围

优先测量实际可用的轻量、可控路径，不追求大规模压力测试：

- `SELECT 1` 同步查询：建议重复 5 次，记录每次 wall time、中位数、P95（标明样本量小，仅观察参考）、最大值。
- 授权测试表有界 100 行同步查询与异步查询：每类在不增加集群负担的前提下多次执行。
- CSV/JSON 同步/异步导出：至少一次完整成功，记录提交、执行、SFTP/下载、落盘和总耗时。
- 本地可控生成数据的解析、序列化和文件存储基准：例如 1 MB、10 MB、100 MB（按机器资源适当缩减），记录吞吐、峰值 RSS/内存与磁盘使用。
- 并发测试仅在取得明确非生产环境/测试窗口后执行；默认不做生产压测。

### 输出

生成 `docs/live-acceptance-benchmark.md` 和本地结构化数据。记录样本数、冷热缓存说明、数据规模、环境、时序、是否复用连接、失败/超时次数。不要凭单次耗时宣称“性能提升 xx%”；无优化前基线时只报告当前性能画像及热点。

需要时单独识别：SSH 握手、Kerberos/环境初始化、Beeline 启动、远端 SQL 执行、解析、SFTP 传输、本地写盘的耗时占比；仅在有准确打点能力时提供拆分结论，禁止猜测。

---

## 9. C7 — 自动化测试、回归与交付门禁

### 建议测试层次

**L1：纯单元测试**（不接真实集群）

- Job 状态迁移覆盖所有竞争条件。
- 远端已成功 vs 本地超时；取消和成功并发。
- SFTP 失败/重试、临时文件原子落盘、CSV/JSON 有效性。
- runner 状态聚合、退出码、失败分类与 evidence 完整性。
- NULL/空字段/重复列名及截断元数据回归。

**L2：受控集成测试**（fake SSH/Beeline/SFTP + SQLite + 真正多进程 MCP）

- 进程 A 产生 ID → 进程 B 通过同一 DB 恢复。
- 重试不重新执行 SQL，提交计数可断言。
- 客户端短断、重复 status、下载过程中进程退出。
- Windows/Linux 环境差异：pid 处理、路径与编码；无法直接运行的平台标记 `UNVERIFIED` 而非假设通过。

**L3：真实 Carbon 验收**（只读、可控、明确授权）

- C0 连通、C1 异步 CSV/JSON 完整导出、C3 重启恢复、C5 对账、C6 性能；限制在可用的环境条件内。
- 不允许用 L1/L2 的 PASS 代替 L3 的 PASS。

### 最低命令集（按项目真实环境调整）

```powershell
# Windows PowerShell：优先使用仓库已有的 .venv Python
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests scripts

git diff --check

# 首先只做无损预检/烟测，再分别执行真实任务
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode preflight
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode smoke
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode exports
# 重启恢复与数据对账的参数须依据最终实现及 runbook 填写
```

CI/本地自动化必须运行非真实环境测试；真实测试可独立手动/受控触发，不能默认在 CI 对生产连接执行。

### 必须交付的文件

1. 必要的修复代码与新增测试（沿用当前项目组织方式）。
2. `scripts/live_acceptance.py` 的真实退出码、两进程恢复及文件校验能力。
3. `docs/live-acceptance.md`：更新为本轮最新事实，保留历史结果但清楚区分本轮与上轮，注明基线/修复后 SHA 和确切状态。
4. `docs/live-acceptance-closure.md`：本轮专项结论、根因、修复点、G/C 门禁表、失败/阻塞原因、复跑命令、后续行动。
5. `docs/live-acceptance-runbook.md`：更新可复制的运行命令、环境前提、数据范围、退出码与故障解释。
6. `docs/live-acceptance-benchmark.md`：在有数据时提供真实性能；若无法执行，标明 `UNVERIFIED` 和原因，不创造测量结果。
7. `.acceptance-data/live-<run-id>/results.json` 及必要的脱敏状态/哈希/计时证据（**本地保留，不推送**）。

### 人工终审矩阵（必须填写，不允许空白）

| 门禁 | 状态 | 代码证据 | 真实执行证据 | 缺口或原因 |
|---|---|---|---|---|
| C0 环境/基线 | 待执行 | | | |
| C1 异步 CSV/JSON | 待执行 | | | |
| C2 状态机/重试 | 待执行 | | | |
| C3 跨进程恢复 | 待执行 | | | |
| C4 runner 可信度 | 待执行 | | | |
| C5 数据对账 | 待执行 | | | |
| C6 性能基准 | 待执行 | | | |
| C7 全量回归/交付 | 待执行 | | | |

---

## 10. 执行流程与停止条件

1. **Audit**：先复现/核实上轮报告里的失败、确定当前 HEAD 真实情况；写 baseline。
2. **Fix P0**：优先修后台导出、终态和幂等下载；每修一类问题就添加确定性测试。
3. **Fix Runner**：使退出码、分类、两进程恢复、导出内容验证可信，避免自动化误判。
4. **Verify Local**：单测/集成测试/静态编译/diff check；保留完整日志及测试数。
5. **Verify Live**：在可使用的真实环境分别复验 CSV、JSON、Query Job、重启恢复；对不适合真实执行的故障注入只在受控环境模拟，并分别标明验证层级。
6. **Correctness & Benchmark**：使用允许的测试集完成对账与基准；缺乏可靠基线就清楚标明差距。
7. **Close**：更新文档、状态矩阵、证据索引；若仍 `PARTIAL/FAIL/BLOCKED_ENV`，写明剩余操作步骤，不通过修改状态定义来掩盖问题。

### 立即停止或跳过真实测试的条件

- 发现当前连接不是预期的 Carbon 集群、Kerberos/SSH 认证异常或测试 SQL 不符合已允许范围。
- 需要故意杀死真实共享集群上的 Beeline/服务进程、破坏网络或大量并发才能复现；此类测试仅在隔离环境进行。
- 测试正在明显影响共享集群稳定性、出现在控制范围之外的性能负担；停止相应真实步骤，保留证据，改为本地可控回归。
- 真实业务记录将进入 stdout、Git diff、issue 或提交文件；先删除原始值、改保留脱敏统计。

### 如遇到环境阻塞

不要反复进行高风险试探，更不要回退到 Mock 后将其标成 Live PASS。保留已经完成的代码修复与 L1/L2 证据，把无法验证的部分标为 `BLOCKED_ENV/UNVERIFIED`，写明**精确缺失条件和一次可复制的复跑命令**。能完成的独立任务继续完成。

---

## 11. 最终给用户的答复格式（Codex 必须输出）

最终回复使用以下结构，不得仅报“已完成”：

```text
# Carbon-Lens Live Acceptance Closure

Baseline SHA: <sha>
Final SHA: <sha or uncommitted>
Overall: PASS | PARTIAL | FAIL | BLOCKED_ENV

## 修复摘要
- <根因 → 修复点 → 测试证据>

## 门禁结果
C0 ...
C1 ...
C2 ...
C3 ...
C4 ...
C5 ...
C6 ...
C7 ...

## 实际执行的验证
- pytest: <x passed / failures>
- compileall: <pass/fail>
- git diff --check: <pass/fail>
- Live SSH/Beeline: <pass/blocked + 证据>
- Async CSV: <pass/fail/unverified + export_id + 文件校验>
- Async JSON: <pass/fail/unverified + export_id + 文件校验>
- MCP restart: <pass/fail/unverified + 前后进程 ID/Job ID 证据>
- Data reconciliation: <pass/fail/unverified + 方法>
- Performance: <数据或未验证原因>

## 证据与文档
- docs/live-acceptance-closure.md
- docs/live-acceptance.md
- docs/live-acceptance-runbook.md
- docs/live-acceptance-benchmark.md
- .acceptance-data/live-<run-id>/results.json (本地，不提交)

## 未关闭问题
- <编号 / 影响 / 实际状态 / 下一步 / 阻塞条件>

## 提交状态
- <本地提交 SHA、是否已 push，严格陈述实际事实>
```

**验收信条：** 不为 PASS 而改测试，不把超时改成成功，不把错误降级为环境阻塞，不重跑未知状态的写入操作，不因单测成功就声称真实集群成功。工程目标始终是：**数据正确、功能好用、执行高效、长任务稳定**。
