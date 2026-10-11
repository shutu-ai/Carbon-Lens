# Carbon-Lens v0.4 — Streamable HTTP MCP 服务化与多连接模式兼容

> **Codex 可执行工程开发任务书**  
> 仓库：<https://github.com/shutu-ai/Carbon-Lens>  
> 版本目标：v0.4.0（版本命名按仓库实际发布策略处理）  
> 工作基线：执行前以 `origin/main` 的最新提交为准，不假定提交号或现有接口不变。  
> 目标：**在不重写既有查询、异步任务和导出引擎的前提下，增加独立运行的 Streamable HTTP MCP Server；保留 stdio；让前端 MCP Transport 与后端 CarbonData 连接方式正交组合。**

---

## 0. 执行指令（给 Codex）

你是本仓库的资深 Python/MCP 工程师。本任务要求**实际修改代码、编写测试、运行验证、更新文档并形成验收证据**，不是单纯提交设计建议。请先检查仓库当前状态、已有代码和最新 MCP SDK API，再按门禁逐项实施。采用最小增量改造，避免与 v0.3 可靠性成果冲突。

**优先级：稳定可用 > 协议互通 > 兼容现有能力 > 实现简单 > 性能优化。**

强制约束：

1. 原有 `stdio` MCP 客户端继续可用，原有 22 个 MCP 工具的**名称、参数、返回字段、错误语义**维持兼容（若 main 已变化，以实际工具清单建立基线）。
2. 新增 `Streamable HTTP`，不是旧式 SSE-only transport，也不是手写非标准 HTTP-JSON RPC 网关；必须使用官方 MCP Python SDK 中标准的传输能力。
3. MCP Transport（stdio / Streamable HTTP）与 CarbonData Access（本地 Beeline / SSH / SSH 跳板）相互独立。不得为了新增 HTTP 模式强迫 Server 部署到 CarbonData 主机或删去现有 SSH/Kerberos/SFTP 能力。
4. v0.3 的**原始数据表不可修改/删除**保护和受管中间表归属验证不得削弱；`write_workspace.enabled=false` 仍为默认。真实环境不执行任何原始表破坏性 SQL。
5. 不引入用户账号、RBAC、多租户、审批、微服务、分布式调度平台。HTTP 远程访问采取足够简单的最小连接保护：默认仅监听 loopback；显式暴露到其他网卡时使用可配置的共享 token、合理的 Host/Origin 检查。勿在代码、日志或 Git 中暴露凭据。
6. C5 原生 Beeline 全量数据对账和 C6 性能 SLA/性能排名均保持 `WAIVED / OUT OF SCOPE`。只要求功能健壮、异常可恢复、不会因大输出导致服务不可用。
7. 不要重复提交 `INSERT`、`OVERWRITE` 等非幂等写入以“修复超时”；遇不确定执行状态时沿用 v0.3 的 `unknown` / `reconciliation_required` 机制。
8. 所有环境缺失导致的验收不可执行必须明示 `BLOCKED_ENV/UNVERIFIED`，不得把 Mock 测试写成真实集群 PASS。

### 预期最小使用方式（命令示意，最终 CLI 可根据项目风格微调）

本地 stdio：

```bash
carbon-lens
# 或 python run_mcp.py
```

HTTP 常驻服务：

```bash
carbon-lens serve --transport streamable-http --host 127.0.0.1 --port 8765 --path /mcp
# 客户端 URL: http://127.0.0.1:8765/mcp
```

HTTP 内网访问（**显式启用**，并配共享 Token）：

```bash
carbon-lens serve --transport streamable-http --host 0.0.0.0 --port 8765 --path /mcp
# 客户端 URL: http://<server-lan-ip>:8765/mcp
```

任何示例命令都须由实际实现支持；不允许 README 中出现尚未实现的 CLI 而不标注示意。

---

## 1. 实施前的代码与协议核查（G0）

### 1.1 检查项目基线

执行以下或等价命令并记录输出：

```bash
git status --short
git branch --show-current
git log -1 --oneline
python --version
python -m pytest -q
python -m compileall -q src
```

重点检查以下当前代码（以仓库实际情况为准）：

- `src/carbon_lens/server.py`：`FastMCP` 工具注册、`main()`、`app.run(transport="stdio")`、惰性单例。
- `src/carbon_lens/lifecycle.py`：`InstanceLock.for_config(parent_pid=...)` 和 `ParentProcessMonitor` 是否只适用 stdio。
- `src/carbon_lens/config.py`、`config.example.json`：多连接 SSH、跳板机、Kerberos、`write_workspace` 等配置。
- `src/carbon_lens/ssh.py`、`service.py`、`jobs.py`、`exports.py`、`executions.py`：数据库访问和异步任务持久化的边界。
- `pyproject.toml`、`run_mcp.py`、`README.md`、`MCP_CONFIGURATION.md`、`MCP_FIRST_DEPLOYMENT.md`、`skills/carbon-analysis/`。
- 现有测试、v0.3 验收报告及 `CARBON_LENS_JOB_DB`/`export_dir` 的生命周期规则。

**关键提醒：** 当前仓库已有按父 PID 计算的实例锁及可选父进程监控；这是典型本地 stdio 部署语义。HTTP 是常驻服务，必须把两者分开。HTTP 不得在任一客户端断开或父启动 Shell 退出后自动停止。多客户端不得触发“already running for config”的误报。

### 1.2 复核 MCP SDK，不做未经评估的升级

当前公开 `pyproject.toml` 使用 `mcp>=1.13,<2`，现有 `server.py` 使用 `mcp.server.fastmcp.FastMCP`。先验证当前安装版是否完整支持标准 Streamable HTTP。优先在最小改动下使用既有 SDK；只有出现必要的协议兼容性缺口且测试能够覆盖时才考虑升级。若升级到 SDK v2，需特别注意 `FastMCP` → `MCPServer` 等 API/Transport 配置的破坏性变化，严禁未经回归直接升级整个依赖链。

必须确认：

- `initialize`、`tools/list`、`tools/call` 在真实 MCP HTTP 客户端中互通。
- MCP 标准的 HTTP 请求方法、必要请求/响应头、Content-Type、协议版本协商、可选会话 ID、JSON/SSE 响应行为由 SDK 处理，不要自行猜测实现。
- 选择 stateful/stateless 时评估长任务 ID 是否与 HTTP 会话绑定。**作业状态必须基于持久化 task ID，而不是易失 HTTP Session。**
- 不应要求 HTTP 持续连接维持 10 分钟至数小时查询；长任务必须继续使用 `start`/`status`/`cancel`。

参考官方资料（以开发时最新版为准）：

- <https://py.sdk.modelcontextprotocol.io/run/>
- <https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md>
- <https://modelcontextprotocol.io/specification>

**G0 产物：** 在验收文档记录当前提交、SDK 版本、协议兼容方案、工具清单和必要修改点。

---

## 2. 架构约束与连接矩阵（P0）

### 2.1 严格划分两个独立层

```text
                          MCP CLIENT LAYER
       Codex / Claude / Other MCP-capable Agents
                   |                     |
                 stdio             Streamable HTTP
                   \                   /
                    \                 /
                  Carbon-Lens MCP Tools
                   (same tool registry)
                            |
                Shared application services
             Query / Export / Jobs / Workspace
                            |
                  Carbon Access Adapter
                /            |            \
        Local Beeline    Direct SSH      SSH Jump Host
                \            |            /
                 CarbonData / Fiber Beeline
```

`stdio`/HTTP 只改变 MCP 传输；CarbonData 的连接由 `connection` 配置决定。不得在 HTTP handler 内复制 SQL、SSH、Kerberos 或导出逻辑。

### 2.2 验收组合矩阵

| MCP Transport | Carbon 访问路径 | 要求 | 环境不足的处理 |
|---|---|---|---|
| stdio | 已有 SSH | P0 保持功能 | 原已验证路径需回归 |
| stdio | SSH + Jump Host | P0 保持功能 | 具环境时现场测；无环境做适配器/模拟回归 |
| HTTP | 已有 SSH | P0 验证 | 环境可用则真实测试 |
| HTTP | SSH + Jump Host | P0 保持并验证接线正确性 | 无跳板环境可标注 UNVERIFIED，禁止伪造 PASS |
| stdio | 本地 Beeline | P1 作为可选适配方式 | 有真实本地环境时测试 |
| HTTP | 本地 Beeline | P1 作为可选适配方式 | 同上 |

**本地 Beeline 的边界：** 如果当前项目没有本地执行适配器，先实现轻量 `local` 连接后端，或提出最小可行接口。它须复用现有 SQL 构造、Kerberos、任务状态与清理语义；若真实环境缺少本地 Carbon 客户端，可用受控 fake executable 做端到端进程测试，并将真实本地执行标记 `UNVERIFIED`。不得为了实现 local 一次性重写成熟的 SSH+SFTP 路径。若不能在保证健壮性前提下实现，明确记录为 v0.4 P1 deferred，而不能声称所有连接模式均已实际支持。

**必须确保：** HTTP Server 所在机器不需要与 CarbonData 同机，SSH 跳板方案不因 HTTP 传输而退化。

---

## 3. G1 — Streamable HTTP Transport（P0）

实现下面能力：

1. 在现有 MCP Tool 注册的基础上增加标准 HTTP Transport，默认路径 `/mcp`，Host/Port/Path 可配置。
2. 本地 `carbon-lens`/`python run_mcp.py` 的默认行为保持 `stdio`；启动参数不能破坏原有 Codex 配置。
3. HTTP 服务器为独立常驻进程；支持 Windows/Linux，优雅处理 `SIGTERM`/`Ctrl+C`（按平台能力实现）。
4. 监听端口占用、无效配置、绑定失败时快速失败并有清晰诊断；不应无声回退到 stdio。
5. MCP stdout 规则：stdio 模式 stdout 仅作 MCP 数据通道；HTTP 模式日志按 SDK/日志规范输出，凭据不得泄漏。
6. 当客户端关闭连接、HTTP 请求超时或取消请求时，**不默认取消已有远端异步 SQL**；只有 `carbon_query_cancel`/`carbon_export_cancel` 等显式业务操作才取消对应任务。
7. 如果 SDK 的 HTTP 请求可能并发触发懒加载单例，确保初始化是线程/协程安全的，不生成冲突的 SQLite 管理器或重复清理远端任务。
8. HTTP 不需要单独注册一套 tools；两个 Transport 暴露同一工具注册表，工具参数与返回 schema 一致。

### 最低验证

- stdio/HTTP `tools/list` 获取的工具名称集合相同，预期保留当前约 22 个工具。
- 真实 HTTP MCP Client 完成 `initialize` → `tools/list` → 调用 `carbon_list_connections`。
- 两个独立 HTTP Client 并发调用同一服务，不发生 session 混乱、锁冲突或 HTTP server 退出。
- 关闭客户端 A，客户端 B 与服务进程持续可用。
- 调用不存在的工具与协议不合法请求得到符合 SDK 行为的错误，不致进程崩溃。

---

## 4. G2 — 生命周期、实例锁和多客户端共享（P0）

### 必须修复的已知设计冲突

当前 `InstanceLock.for_config()` 的 key 与父 PID 有关，`ParentProcessMonitor` 是为本地代理生命周期设计的。这些机制在 HTTP 常驻服务中不能照搬。

设计为两套明确语义：

- **stdio：** 保留既有父进程/实例锁规则，除确有错误外不要改变默认行为。
- **HTTP：** 独立生命周期，不绑定客户端 PID；根据监听端口及持久化数据库资源选择合适的单实例控制。最小实现可以限定同一数据库/导出目录由一个 HTTP 服务进程拥有，不在 v0.4 引入多实例集群。

具体要求：

1. HTTP 多客户端共享**同一 QueryJobManager、ExportJobManager、WriteExecutionManager 和 SQLite**（如当前代码用惰性单例，保证并发正确）。
2. A 启动查询，B 仅凭 task ID 能够查询状态、取消任务、取得结果（均在已授权同一可信服务内）。
3. 停止/重启整个 HTTP 服务后，B 仍能继续查询此前持久化的远端任务；不得重新提交 SQL。
4. 多个并发 `status()` 请求不会重复下载、删除同一远端文件，也不会产生不一致终态；必要时给 job-finalize / export-download 加细粒度互斥与原子文件替换，不进行全局粗锁串行化。
5. SQLite 同时访问遵守原有事务边界；进程重启后不破坏数据文件和 v0.3 注册表。
6. HTTP 优雅关闭不可删除还在远端执行的任务；仅清理本地短生命资源。不得将 Client Session 清理与远端 Job 清理混在一起。
7. 如果有并发冲突、SQLite locked、重复完成请求，提供可恢复且可诊断的结果，不悄悄吞掉异常。

### 故障用例

- 客户端断开后异步任务继续；客户端重新连接按原 ID 查询。
- 服务重启前 `running` → 重启后 `running/succeeded/failed`，终态与远端一致。
- 两客户端同时请求同一 `export_status`，最终只生成一个完整可下载文件。
- 服务在生成导出文件/SQLite 状态提交边界异常退出，重启后可继续收敛。
- 写入任务处于 `unknown` 时不得因 HTTP 重连或重试执行第二次。

---

## 5. G3 — 后端 CarbonData 连接路径兼容（P0/P1）

**HTTP 模式不能更改现有 SSH 配置的含义。**

### 保留和回归

- 单跳 SSH：密码或密钥（依据配置实际支持情况），远端 `fiber-beeline`、Kerberos、`bigdata_env`、`run_as_user`。
- SSH Jump Host / Bastion：原有 ProxyJump 类连接方式、Host Key、登录链路及二跳执行。
- SFTP：同步/后台 CSV 下载、断线后的下载重试、远端临时文件清理。
- 多个 `connection`：不同连接独立选路；HTTP 客户端不需要持有目标 SSH/Kerberos 凭据。
- HTTP 服务器在 Windows 或 Linux 上部署时，均不要求修改目标 CarbonData 主机服务配置。

### 可选 local backend（G3-L，P1）

如确需支持直接本地 Beeline：

1. `connection` 配置明确 `transport/backend = local`（具体字段按兼容原则确定），不与 MCP 的 `transport` 混用命名。
2. 封装 `LocalBeelineExecutor` 或等价适配层，复用 SQL/结果解析/任务模型，避免复制 MCP 工具。
3. 进程启动、超时、信号终止、工作目录、Kerberos 环境、临时文件、后台 detached job 和异常恢复与现有语义一致。
4. Local backend 无 SFTP 也要返回同样的导出契约，用受控本地文件复制/原子落盘代替远程下载。
5. 无本地 CarbonData 环境时通过真实本地进程 fake Beeline 覆盖最小完整路径，现场项明确未验证。

如果实现风险超过 v0.4 范围，允许 G3-L 明确推迟，但需要说明后续最小实现方案；**SSH 与跳板路径为 P0，绝对不能因 local backend 延后而被阻断**。

---

## 6. G4 — 远程导出文件交付（P0）

当前导出完成后保存的是 **MCP Server 本机文件**。远程 HTTP 客户端不能直接访问该服务器文件系统，因此需要提供一种受控且可重试的远程获取方式。

### 推荐最小设计

- 继续使用现有 `carbon_export_start` → `carbon_export_status` 生成文件并持久化状态。
- 在 HTTP 模式增加**按 export ID 定位文件的流式下载路径**（例如 `GET /exports/{export_id}/download`；实际路径可调整），或者选用等价的标准 MCP Resource 下载机制，但必须证明真实客户端能取得大文件且无需在 MCP JSON 中嵌入整文件。
- `export_status` 可以保留已有本地路径字段（兼容旧客户端），同时在 HTTP 场景增加显式 `download_url` / `download_available` 等向后兼容信息。若 URL 依赖 Host/反向代理，正确处理配置的公开 base URL，不要从不可信请求头拼接任意下载地址。
- 下载 URL 由可信服务器配置生成。只允许下载 `export_id` 对应的已完成导出，验证文件归属和文件存在；**不能接收任意本地文件路径**。
- 文件传输逐块流式发送，不把 100 MiB～GiB 文件完整读入内存或 base64 塞入 MCP 返回值；尽可能设置 `Content-Length`、`Content-Type`、`Content-Disposition`。
- 若网络中断，可再次请求同一文件；不得为下载重新执行 SQL。支持断点续传（HTTP Range）作为 P1 选项，若未实现须保证全量重试可用。
- 下载接口与 HTTP MCP 使用同一个简单 bearer token 保护（当服务暴露至内网时），不需要独立账户系统。URL 中不携带长期凭据。
- 导出文件清理继续遵守现有 TTL/数据库状态；并发下载时不能中途误删正在读取的文件。
- 明确区分：`export succeeded but file missing/expired`、`not ready`、`unknown export_id`、`download failed but retryable`。
- stdio 模式仍可按原有方式返回文件路径，不能为了 HTTP 改造强迫本地用户经 HTTP 下载。

**P0 演示闭环：** HTTP Client A 发起后台 CSV 导出 → Client B 查状态直到成功 → Client B 在自己的设备保存 CSV → 校验内容/大小/哈希 → 同一个 URL 再次下载 → 不重复执行 SQL。

---

## 7. G5 — 配置、部署与最小连接保护（P0/P1）

### 配置与命令

- 为 HTTP 引入直观可配置项：`transport`、`host`、`port`、`path`、`public_base_url`（若需要）、`access_token`（环境变量优先）和合理日志等级。
- 默认本地 `stdio` 保持零配置兼容；HTTP 默认监听 `127.0.0.1`。
- HTTP 远程监听如 `0.0.0.0` 必须显式配置共享 Token；部署在可信内网/VPN，文档提醒不要直接裸露公网。支持正确的 Origin/Host 限定或复用 SDK 的 DNS rebinding 保护。
- 不实现 OAuth、RBAC、用户表、租户隔离和审批。
- 配置错误必须在 startup 阶段返回可行动的异常信息，日志不得打印密码、keytab、token、数据库凭据。
- 允许在服务器端统一配置连接；客户端只需 MCP URL（和远程访问 Token），不再配置 SSH 密钥或 Kerberos。
- 示例应覆盖 Windows PowerShell、Linux shell、服务常驻方式（Linux systemd 可选示例；Windows 可用脚本/任务计划示意，避免另装繁重守护工具）。
- 提供最小健康检查（如 `/healthz`）用于判断 HTTP 服务可用；健康检查不应触发 SSH、Kerberos 或重 SQL，也不输出敏感信息。

### Codex 配置示例

在文档中提供经当前 Codex 版本/CLI 核查后的**可用配置示例**，例如 TOML：

```toml
[mcp_servers.carbon_lens]
url = "http://127.0.0.1:8765/mcp"
```

如涉及认证 Headers，优先使用客户端支持的环境变量映射/配置方式，不在可提交的 TOML/截图中写明文 Token。不同客户端需标明支持/未测试，不得硬编未知字段。

---

## 8. G6 — 22 个工具双 Transport 兼容与业务回归（P0）

针对**现有全部工具**制作自动化 tool parity 快照，核对：

- `tools/list` 工具名称集合相同；字段定义、可选参数、默认值和返回结构无破坏性改变。
- 查询：`carbon_query`、`carbon_query_start/status/list/cancel`。
- 表结构：`carbon_describe`、`carbon_list_connections`。
- 导出：`carbon_export`、`carbon_export_start/status/list/cancel`。
- v0.3 SQL / 工作区管理工具完整保留，并且写入默认禁用。
- 原始表、跨 workspace、未登记中间表、所有权令牌错误都在提交远端写操作前拒绝。
- 异步状态、取消竞争、文件下载重试、`unknown/reconciliation_required` 维持不变。
- HTTP 传输层断连不应抹掉已有任务、覆盖 SQLite 或删除导出文件。

业务回归优先依赖已有测试 fixture / fake SSH/Beeline，不为了 HTTP 运输层重复跑海量真实 SQL。

---

## 9. G7 — 自动化测试与故障注入（P0）

建议添加或扩展下列测试，命名以仓库风格为准：

- `tests/test_mcp_streamable_http.py`
- `tests/test_mcp_transport_parity.py`
- `tests/test_http_lifecycle.py`
- `tests/test_http_multi_client_jobs.py`
- `tests/test_http_export_download.py`
- `tests/test_connection_modes.py`

覆盖场景（每项记录真实测试名称）：

1. stdio 启动和 MCP 工具列表保持不变。
2. HTTP initialize/tools/list/tools/call 与真实 MCP SDK Client 互通；不能只用裸 `requests.post()` 构造非标准 RPC 测试。
3. 两客户端并发查询及相互按 ID 查询状态。
4. HTTP 客户端退出不关闭服务；重连与服务重启后任务可恢复。
5. 端口冲突、错误 HTTP 请求、非法参数、无 token 的非本地访问有可诊断错误。
6. 异步导出完整下载、断线重试、同时下载、文件过期、错误 export ID、防止 `../` 路径穿越。
7. HTTP 服务异常停止时不触发远程 SQL 重执行，不误置任务终态。
8. SSH 直连、跳板配置与 HTTP/stdin 两种方式的接线回归。
9. 如实现 local backend，使用可控 fake executable 验证创建进程、捕获 stdout/stderr、取消、超时和导出。
10. 旧 82 项（或最新基线全部）测试继续通过；不能删测试或放松断言获得绿色结果。
11. 针对 v0.3 SQL 策略执行负向测试，确保原始表始终只读。

结果证据至少包括：`pytest -q`、`compileall`、`git diff --check`，以及经过真实 MCP Client 的 HTTP 调用日志/JSON（脱敏）。

---

## 10. G8 — 真实环境最小验收（P0）

如果存在前面已验证的真实 CarbonData 连接，使用同一服务端配置，**只做低风险、轻量查询/导出**。

### 真实 HTTP 端到端最小清单

1. HTTP 服务独立运行，外部 Codex/等价 MCP Client 成功连接 `/mcp`。
2. 工具发现完整，`carbon_list_connections` 成功（不返回凭据）。
3. `SELECT 1` 或已批准测试表的有限样本查询成功，结果准确可读。
4. 真实 `carbon_query_start` → 客户端断开 → 重新连接 → 原 task ID 查询成功。
5. 真实 `carbon_export_start` → `status` → HTTP 下载 CSV 文件成功；断开/重试不重跑 SQL。
6. 重启整个 HTTP MCP Server，已有任务仍能用原 ID 查询，符合真实状态。
7. 如果可使用 SSH Jump 环境，在 HTTP 模式上跑一次受控只读 SQL；若无环境，标 `UNVERIFIED`，不能用 mock 代替现场结果。
8. `write_workspace.enabled=false` 维持默认；不执行真实 CTAS/INSERT/OVERWRITE/DROP，仅用 Mock 负向测试确认阻止对原始表写入。

### 不做的验收

- **不**以 Beeline 原生结果全量逐条对账作为交付门禁（C5 WAIVED）。
- **不**要求固定 QPS、延迟 P95 或吞吐 SLA（C6 WAIVED）。
- **不**强制做真实写入 G8（v0.3 既有未验证状态不因本次 v0.4 自动变为 PASS）。
- **不**使用超大生产表做压力测试，不占用共享集群过多资源。

如环境不可用，标记 `BLOCKED_ENV`，保留可重复执行脚本和准确的未验证清单；**本地工程 PASS 与真实现场 PASS 必须分开**。

---

## 11. G9 — 文档与交付（P0）

### 修改与新增文档

1. `README.md`：新增 stdio / Streamable HTTP 双运行模式、清晰的架构图、示例 CLI/URL。
2. `MCP_CONFIGURATION.md` / `MCP_FIRST_DEPLOYMENT.md`：补齐本地/内网客户端接入、Windows/Linux 步骤、常见问题。
3. `config.example.json`：增加 HTTP 配置占位示例；不放真实地址、用户名、密码、token。
4. `skills/carbon-analysis/`：更新远程模式与导出下载操作说明，**不加入智能数据发现或 Agent 分析新业务逻辑**。
5. `docs/v0.4-streamable-http-design.md`：架构、Transport 选择、状态/任务共享边界、配置兼容、local backend 是否实现及理由。
6. `docs/v0.4-streamable-http-acceptance.md`：G0–G9 逐项结果、实际命令、工具列表校验、故障注入、真实环境验证、遗留问题。

### 验收报告约定

每个门禁输出：

| 字段 | 要求 |
|---|---|
| Gate | G0–G9 |
| Verdict | PASS / PARTIAL / FAIL / BLOCKED_ENV / WAIVED |
| Evidence | 命令、测试结果、路径及脱敏记录 |
| Remaining | 阻碍、未验证项及风险 |

区分 `MOCK PASS` / `REAL PASS`；不能以 HTTP 端口监听成功代替 MCP 协议互通；不能以 `export_status=succeeded` 代替客户端下载验证。

### 最终交付要求

- 代码最小增量实现，原有服务与工具保持兼容。
- 单元测试和集成测试通过；若因外部环境阻断，明确列出而非声称全量通过。
- 使用真实 MCP Client 实际验证 HTTP transport。
- 输出最终 commit SHA、测试数量、变更文件列表和运行/连接示例。
- 保持工作区清洁，不提交 `.env`、config.json、私钥、keytab、SQLite 任务文件、真实导出数据或生产日志。
- 确认无敏感信息泄漏后，按仓库既有 Git 工作流提交并推送；若存在未通过的 P0 本地门禁，应先修复，不得以“已推送”代替验收。

---

## 12. 明确不在 v0.4 范围内

- 不增加表/字段智能检索、SQL Agent 自规划、RAG、知识图谱、Data Copilot 功能。
- 不扩展新的 CarbonData SQL 写入能力；只保持 v0.3 现有受管中间表功能，默认关闭。
- 不重构 CarbonData SSH/Kerberos/Jump Host 全链路，仅做必要的适配和兼容修复。
- 不建立账号体系、用户管理、RBAC、审批、复杂审计平台。
- 不上 Redis、Kafka、Kubernetes、多副本状态同步、负载均衡、分布式事务。
- 不实现旧版 SSE-only Transport，不自造 MCP 协议。
- 不把 CarbonData 执行时间作为 MCP 服务性能 SLA。

---

## 13. 最终成功定义（必须逐条回答）

- [ ] `stdio` 完全兼容，原有工具清单与 API 无破坏性变化。
- [ ] `Streamable HTTP /mcp` 被真实 MCP Client 成功使用。
- [ ] HTTP Server 可独立常驻运行，客户端断开不关闭服务。
- [ ] HTTP 双客户端能共享异步任务，服务重启后 task ID 可继续使用。
- [ ] HTTP 远程客户端能**实际下载** CSV，而不是仅拿到服务器本地路径。
- [ ] 原有 SSH 直连、SSH Jump Host、Kerberos、SFTP 仍可用，且与 MCP Transport 解耦。
- [ ] 本地 Beeline 适配如实现，有专门测试；若暂缓，明确原因且不影响 P0。
- [ ] 默认写入关闭，原始表只读保护与 v0.3 幂等/未知态机制保持。
- [ ] 测试/静态检查 PASS，现实环境限制与 Mock 证据严格区分。
- [ ] C5/C6 持续 `WAIVED`；不为性能对比或额外业务功能拖延交付。
- [ ] 文档与示例命令可以从一台新电脑直接复现。

**最终目标一句话：** Carbon-Lens v0.4 从“主要通过本地 stdio 启动的 MCP 工具”升级为“既可 stdio 本地运行、也可 Streamable HTTP 独立服务化、多人/多 Agent 共享、后端继续支持本地 Beeline/SSH/SSH 跳板的稳定 CarbonData MCP Server”。
