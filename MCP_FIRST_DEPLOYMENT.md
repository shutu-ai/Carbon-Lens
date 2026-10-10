# CarbonLens MCP 首次部署与使用文档

本文用于在一台新的 Windows 机器上首次部署 CarbonLens MCP，并接入 Codex 或其它本地 Agent。

本项目的 MCP 使用 `stdio` 传输：Agent 启动一个本地 Python 进程，通过标准输入输出调用 CarbonLens 工具。MCP 进程本身再通过 SSH 连接 Carbon 主机，调用远端的 `fiber-beeline`。

## 1. 部署前检查

### 本地 Windows 主机

- Python 3.11 或更高版本。
- 能访问 Carbon 主机的 SSH 端口，默认是 TCP 22。
- 有权限创建部署目录、虚拟环境和导出目录。
- 已安装 Codex、Claude Desktop、Cursor、VS Code 或其它 MCP 客户端之一。

### 远端 Carbon 主机

配置中的 SSH 账号需要能够访问以下资源：

- Carbon 客户端环境脚本，例如 `/opt/client/bigdata_env`。
- `fiber-beeline`，例如 `/opt/client/Fiber/bin/fiber-beeline`。
- Kerberos `kinit`、principal 和 keytab。
- 配置中指定的远端临时目录，默认是 `/tmp`。
- 如果配置了 `run_as_user: root`，SSH 账号还需要能够执行对应的 `su - root` 流程。

## 2. 解压 MCP 包

将 [MCP 包](packages/carbon-lens-mcp-0.1.1.zip)复制到目标机器并解压。建议部署目录为：

```text
D:\apps\carbon-lens
```

解压后，`pyproject.toml`、`run_mcp.py` 和 `src` 应直接位于部署目录下：

```text
D:\apps\carbon-lens\
├── pyproject.toml
├── run_mcp.py
├── config.example.json
├── config.json
├── carbon_lens.json
├── keys\known_hosts
└── src\carbon_lens\
```

如果 ZIP 解压后多了一层 `carbon-lens-mcp` 目录，应将该目录作为部署根目录，或者将其内容移动到 `D:\apps\carbon-lens`。

## 3. 创建 Python 虚拟环境

在部署目录打开 PowerShell：

```powershell
Set-Location 'D:\apps\carbon-lens'
py -3.11 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
```

安装 MCP 服务依赖：

```powershell
& '.\.venv\Scripts\python.exe' -m pip install .
```

如果目标机器无法访问 PyPI，需要提前准备 Python 依赖包或内部 wheelhouse；当前 ZIP 是源码包，不包含离线依赖仓库。

## 4. 初始化 CarbonLens 配置

### 推荐方式：使用环境变量保存密码

先备份包中已有的运行配置，再从示例创建目标配置：

```powershell
Set-Location 'D:\apps\carbon-lens'
if (Test-Path -LiteralPath '.\config.json') {
  Copy-Item -LiteralPath '.\config.json' -Destination '.\config.json.backup'
}
Copy-Item -LiteralPath '.\config.example.json' -Destination '.\config.json' -Force
```

编辑 `config.json`，至少确认以下项目：

- `connections` 下的连接名称。
- `ssh_config.hostname`、`port`、`username`。
- `ssh_config.known_hosts` 或 `private_key`。
- `env_script`、`beeline_path`、`beeline_connection`。
- `principal` 和 `keytab`。
- `remote_tmp_dir`、`export_dir`、超时和行数限制。

配置示例使用 `password_env` 和 `root_password_env`，密码不要写入 JSON。可以为当前 Windows 用户设置环境变量：

```powershell
[Environment]::SetEnvironmentVariable('CARBON_LENS_SSH_PASSWORD', '<SSH密码>', 'User')
[Environment]::SetEnvironmentVariable('CARBON_LENS_ROOT_PASSWORD', '<root密码>', 'User')
```

设置后需要重新打开 Codex 或其它 Agent，使其继承新的环境变量。

如果使用 SSH 私钥认证，可以删除 `password_env`，改为在 `ssh_config` 中设置 `private_key`，并确保私钥文件权限受限。

### 配置文件路径规则

在 Agent 配置中设置：

```text
CARBON_LENS_CONFIG=D:\apps\carbon-lens\config.json
```

`CARBON_LENS_CONFIG` 可以是绝对路径。配置中的相对路径，例如 `keys/known_hosts` 和 `exports`，会相对于 `config.json` 所在目录解析，因此不依赖 Agent 的当前工作目录。

### 检查 SSH 主机密钥

包中已有 `keys\known_hosts` 时，CarbonLens 会优先使用该文件校验 SSH 主机密钥。首次连接新主机时，可以：

1. 预先放置经过核验的 `known_hosts`；或
2. 在确认主机指纹可信后使用 `auto_save_host_key: true` 自动保存。

不要在未确认主机身份的情况下长期使用允许未知主机的配置。

## 5. 本地启动检查

先检查文件和依赖：

```powershell
Set-Location 'D:\apps\carbon-lens'
Test-Path '.\.venv\Scripts\python.exe'
Test-Path '.\run_mcp.py'
Test-Path '.\config.json'
& '.\.venv\Scripts\python.exe' -c "import carbon_lens.server; print('CarbonLens MCP import OK')"
```

也可以直接启动服务：

```powershell
$env:CARBON_LENS_CONFIG = 'D:\apps\carbon-lens\config.json'
& 'D:\apps\carbon-lens\.venv\Scripts\python.exe' `
  'D:\apps\carbon-lens\run_mcp.py'
```

启动后进程会等待 MCP 客户端输入，不会显示普通网页或 HTTP 页面。使用 `Ctrl+C` 停止手工启动的进程。

## 6.1 进程生命周期保护

CarbonLens 启动时会根据 `CARBON_LENS_CONFIG` 和父 Agent 进程加单实例锁。同一个 Agent 使用同一个配置文件时，只允许一个 CarbonLens MCP 进程运行；第二个实例会在 stderr 中提示已有实例并退出。不同 Agent 可以同时使用同一个配置文件。

CarbonLens 支持每 2 秒检查启动它的 Agent 进程，但该监控默认关闭。部分 Agent 会通过短生命周期包装进程启动 stdio MCP，直接监控 `os.getppid()` 可能误判包装进程退出并提前断开 MCP。

只有在确认启动器的父进程 PID 始终稳定时，才启用父进程监控：

```powershell
$env:CARBON_LENS_WATCH_PARENT = '1'
```

该保护只负责本机 MCP 进程，不会取消已经通过 `carbon_query_start` 提交到 Carbon 主机的 detached 查询任务。远程任务仍需使用 `carbon_query_cancel` 显式取消。

## 7. 接入 Codex

编辑 `%USERPROFILE%\.codex\config.toml`，加入：

```toml
[mcp_servers.carbon-lens]
command = 'D:\apps\carbon-lens\.venv\Scripts\python.exe'
args = ['D:\apps\carbon-lens\run_mcp.py']

[mcp_servers.carbon-lens.env]
CARBON_LENS_CONFIG = 'D:\apps\carbon-lens\config.json'
```

或者执行：

```powershell
codex mcp add carbon-lens `
  --env CARBON_LENS_CONFIG=D:\apps\carbon-lens\config.json `
  -- D:\apps\carbon-lens\.venv\Scripts\python.exe `
     D:\apps\carbon-lens\run_mcp.py
```

验证：

```powershell
codex mcp list
```

如果是在 Cursor、Claude Desktop 或 VS Code 中使用，请参考 [MCP 配置文档](MCP_CONFIGURATION.md) 中对应的 JSON 格式。部署完成后不需要为了 Agent 注册再次打包。

## 8. 首次功能验证

建议按以下顺序验证，不要一开始就执行大查询：

### 7.1 查看连接

调用：

```text
carbon_list_connections
```

确认返回连接名称、主机和 beeline 连接名，且没有返回密码、私钥或 keytab 内容。

### 7.2 执行最小只读查询

使用实际返回的连接名称执行：

```sql
SHOW DATABASES
```

设置较小的工具 `limit`，确认能够完成 SSH、Kerberos、beeline 执行和结果解析。

### 7.3 验证表结构

已知表名时优先调用：

```text
carbon_describe(connection="carbon-prod", table="database.table")
```

### 7.4 验证 bounded SELECT

始终带有时间条件和 SQL `LIMIT`，例如：

```sql
SELECT * FROM database.table
WHERE event_time >= '2026-01-01'
LIMIT 10
```

查询可能较慢时使用 `carbon_query_start`，再轮询 `carbon_query_status`，不要重复提交同一个长查询。

### 7.5 验证导出（可选）

只有在确实需要文件时调用 `carbon_export`。先用较小的 `limit` 验证 JSON 或 CSV 导出，并确认文件出现在配置的本地 `export_dir` 下。

较大的文件导出使用 `carbon_export_start`，然后轮询 `carbon_export_status`。远程 Carbon 任务完成后，CarbonLens 会通过 SFTP 自动将文件下载到配置的 `export_dir`；只有本地文件下载成功后，任务才会变为 `succeeded`。如果 SFTP 回传失败，继续查询同一个 `export_id` 会重试下载，不会重复执行 SQL。使用 `carbon_export_list` 查看导出历史，使用 `carbon_export_cancel` 取消远程导出。

## 9. 常见问题

| 现象 | 常见原因 | 处理方式 |
| --- | --- | --- |
| 配置文件不存在 | `CARBON_LENS_CONFIG` 路径错误 | 使用绝对路径，并确认文件名是 `config.json` |
| `No module named mcp` | 虚拟环境未安装依赖 | 使用 `.venv\Scripts\python.exe -m pip install .` |
| SSH connection failed | 主机、端口、账号或密码错误 | 先用相同账号从本机单独验证 SSH |
| Host key 错误 | `known_hosts` 不匹配或主机密钥变化 | 核验指纹后更新 `keys\known_hosts`，不要直接关闭校验 |
| `kinit` 或 beeline 失败 | 远端环境脚本、principal、keytab 或权限错误 | 在 Carbon 主机上用相同账号验证 `bigdata_env`、`kinit` 和 beeline |
| `su - root` 相关失败 | `run_as_user` 或 root 密码配置错误 | 确认远端账号具备对应权限；不需要时删除 `run_as_user` |
| 导出目录错误 | 本地 `exports` 不存在或不可写 | 创建目录并确认当前用户有写权限 |
| Agent 看不到 MCP 工具 | Agent 没有重启或路径指向旧目录 | 重启 Agent，检查 MCP 列表和启动日志 |
| 直接双击入口没有界面 | MCP 是 stdio 服务，不是桌面程序 | 通过 Agent 或命令行启动，不要用浏览器访问 |

## 10. 更新和重新部署

更新代码时建议按以下顺序操作：

1. 停止 Codex 或其它 Agent 中的 CarbonLens MCP。
2. 备份 `config.json`、`keys\known_hosts` 和 `exports`。
3. 替换源码和 `pyproject.toml`，保留运行配置。
4. 执行 `\.venv\Scripts\python.exe -m pip install .` 更新依赖。
5. 重新检查 `CARBON_LENS_CONFIG` 和 Agent 中的 Python、入口路径。
6. 重启 Agent，并重新执行 `carbon_list_connections` 和最小查询。

## 11. 安全要求

- `config.json` 可能含明文 SSH/root 密码，不能上传到公共仓库或发送给不可信人员。
- 交付包中的当前配置只适合受控环境；新环境优先从 `config.example.json` 创建配置。
- 不要在对话中粘贴 SSH 密码、私钥、keytab 或原始认证诊断信息。
- 只使用 `carbon_query`、`carbon_describe` 和 `carbon_export` 支持的只读操作，不要尝试绕过服务端限制执行写 SQL 或任意远程命令。
