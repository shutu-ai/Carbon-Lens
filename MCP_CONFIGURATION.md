# CarbonLens MCP 配置说明

本文说明如何在 Codex、Claude Desktop、Cursor、VS Code 等 Agent 中使用本地
stdio 或独立 Streamable HTTP CarbonLens MCP 服务。

## 1. MCP 服务信息

CarbonLens 支持两种 MCP Transport。stdio 适合 Agent 在本机拉起进程；
Streamable HTTP 适合一个常驻服务供多个客户端共享。两者使用同一工具注册表和
后端连接配置。

| 项目 | 路径 |
| --- | --- |
| Python 解释器 | `D:\dev-projects\carbon-lens\.venv\Scripts\python.exe` |
| MCP 入口 | `D:\dev-projects\carbon-lens\run_mcp.py` |
| CarbonLens 配置 | `D:\dev-projects\carbon-lens\config.json` |

HTTP 的默认值也可写在 `config.json` 的顶层 `http` 对象中，或由命令行/环境变量
覆盖；占位示例见 `config.example.json`。HTTP 默认只监听 `127.0.0.1`。

`config.json` 中包含 SSH 连接、Carbon 客户端、Kerberos、导出目录等运行参数。该文件可能包含明文凭据，应限制访问权限，不要提交到公共代码仓库。

## 2. Codex 配置

Codex 的用户级配置文件是 `%USERPROFILE%\.codex\config.toml`，也可以在受信任的项目中使用项目级 `.codex\config.toml`。

在 `config.toml` 中加入：

```toml
[mcp_servers.carbon-lens]
command = 'D:\dev-projects\carbon-lens\.venv\Scripts\python.exe'
args = ['D:\dev-projects\carbon-lens\run_mcp.py']

[mcp_servers.carbon-lens.env]
CARBON_LENS_CONFIG = 'D:\dev-projects\carbon-lens\config.json'
```

也可以使用 Codex CLI 配置：

```powershell
codex mcp add carbon-lens `
  --env CARBON_LENS_CONFIG=D:\dev-projects\carbon-lens\config.json `
  -- D:\dev-projects\carbon-lens\.venv\Scripts\python.exe `
     D:\dev-projects\carbon-lens\run_mcp.py
```

验证配置：

```powershell
codex mcp list
```

Codex CLI、Codex IDE 扩展和 ChatGPT 桌面端共享该 MCP 配置层；配置一次即可在这些 Codex 客户端中使用。[OpenAI MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)

## 3. Claude Desktop、Cursor 等 JSON 配置

以下配置结构适用于使用 `mcpServers` 的本地 JSON MCP 客户端：

```json
{
  "mcpServers": {
    "carbon-lens": {
      "command": "D:\\dev-projects\\carbon-lens\\.venv\\Scripts\\python.exe",
      "args": [
        "D:\\dev-projects\\carbon-lens\\run_mcp.py"
      ],
      "env": {
        "CARBON_LENS_CONFIG": "D:\\dev-projects\\carbon-lens\\config.json"
      }
    }
  }
}
```

常见配置位置：

- Claude Desktop：`%APPDATA%\Claude\claude_desktop_config.json`
- Cursor 全局配置：`%USERPROFILE%\.cursor\mcp.json`
- Cursor 项目配置：项目目录下的 `.cursor\mcp.json`
- GitHub Copilot CLI：`%USERPROFILE%\.copilot\mcp-config.json`

Cursor 支持在全局或项目级 `mcp.json` 中配置本地 `command` 型 MCP 服务。[Cursor MCP 配置](https://cursor.com/docs/help/customization/mcp)

## 4. VS Code 配置

VS Code 的 `.vscode\mcp.json` 使用顶层 `servers` 字段。将以下文件放在项目根目录的 `.vscode\mcp.json`：

```json
{
  "servers": {
    "carbon-lens": {
      "type": "stdio",
      "command": "D:\\dev-projects\\carbon-lens\\.venv\\Scripts\\python.exe",
      "args": [
        "D:\\dev-projects\\carbon-lens\\run_mcp.py"
      ],
      "env": {
        "CARBON_LENS_CONFIG": "D:\\dev-projects\\carbon-lens\\config.json"
      }
    }
  }
}
```

VS Code 也支持项目根目录的便携格式 `.mcp.json`，该格式使用顶层 `mcpServers` 字段。[VS Code MCP 配置参考](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)

## 5. 使用打包文件时的路径调整

解压 [MCP 包](packages/carbon-lens-mcp-0.1.1.zip) 后，应根据实际解压位置修改三个路径：

1. `command` 指向解压目录下虚拟环境的 Python，或目标机器上的 Python。
2. `args` 指向解压目录下的 `run_mcp.py`。
3. `CARBON_LENS_CONFIG` 指向解压目录下的 `config.json`。

ZIP 包只负责交付 MCP 服务和配置文件，不会自动写入 Codex 或其它 Agent 的用户配置。手工添加上述配置即可，不需要为了本地 Agent 注册而重新打包。

## 6. 运行前检查

在 PowerShell 中检查 Python 和入口文件：

```powershell
Test-Path 'D:\dev-projects\carbon-lens\.venv\Scripts\python.exe'
Test-Path 'D:\dev-projects\carbon-lens\run_mcp.py'
Test-Path 'D:\dev-projects\carbon-lens\config.json'
```

手工启动 MCP 服务：

```powershell
& 'D:\dev-projects\carbon-lens\.venv\Scripts\python.exe' `
  'D:\dev-projects\carbon-lens\run_mcp.py'
```

正常启动后进程会等待 MCP 客户端通过标准输入输出通信，不应把它当作普通 HTTP 服务访问。

CarbonLens 会按 `CARBON_LENS_CONFIG` 和父 Agent 进程对 MCP 进程加单实例锁。CarbonLens 支持监控启动它的 Agent 进程，但默认关闭，因为部分 Agent 会通过短生命周期包装进程启动 stdio MCP。同一个 Agent 不能重复启动同一配置的多个实例；不同 Agent 可以同时使用同一个配置文件。

后台文件导出使用 `carbon_export_start`、`carbon_export_status`、`carbon_export_list` 和 `carbon_export_cancel`。远程导出完成后，`carbon_export_status` 会自动触发 SFTP 回传；只有文件已经写入本地 `export_dir` 才会报告成功。回传失败时，重复查询同一个 `export_id` 会重试回传，不会重新执行远程 SQL。

确认启动器的父进程 PID 始终稳定时，可设置 `CARBON_LENS_WATCH_PARENT=1` 启用父进程监控；父进程退出后，MCP 会自动退出。

## 7. 远程或云端 Agent 注意事项

以上配置是本机 `stdio` 配置，只能在同一台 Windows 机器上访问 `D:\dev-projects\carbon-lens` 的 Agent 中使用。

如果 Agent 在远程主机、容器或云端运行：

- 远程环境必须拥有 MCP 源码、Python 依赖、`config.json` 和 SSH 网络连通性；或
- 启动 Streamable HTTP 服务，再使用 URL 配置；客户端不需要目标 SSH 密钥或 Kerberos 凭据。

远程 Agent 不能直接读取本机的 `D:` 盘路径。

### Streamable HTTP 服务与客户端

Windows PowerShell：

```powershell
Set-Location 'D:\apps\carbon-lens'
$env:CARBON_LENS_CONFIG = 'D:\apps\carbon-lens\config.json'
& '.\.venv\Scripts\python.exe' '.\run_mcp.py' serve `
  --transport streamable-http --host 127.0.0.1 --port 8765 --path /mcp
```

Linux：

```bash
export CARBON_LENS_CONFIG=/srv/carbon-lens/config.json
/srv/carbon-lens/.venv/bin/python /srv/carbon-lens/run_mcp.py serve \
  --transport streamable-http --host 127.0.0.1 --port 8765 --path /mcp
```

Codex 的 URL 型 MCP 配置（字段由当前 Codex CLI 支持）：

```toml
[mcp_servers.carbon_lens]
url = "http://127.0.0.1:8765/mcp"
```

内网绑定必须在服务端设置 `CARBON_LENS_HTTP_ACCESS_TOKEN`，并使用例如
`--allowed-host 'carbon-lens.internal:*' --public-base-url
'http://carbon-lens.internal:8765'`。不要把 token 写入 TOML、截图、日志或 Git。
服务健康检查为 `GET /healthz`。导出完成后，HTTP 工具结果会提供
`download_url`；客户端用同一个 bearer token 对该 URL 发起 GET，可重复下载同一个
文件而不重新执行 SQL。

## 8. 安全要求

- 不要把包含真实密码的 `config.json` 上传到公共仓库、插件市场或聊天记录。
- 交付给其他机器时，优先使用 `config.example.json`，再在目标环境中配置密码环境变量。
- 限制 `config.json`、SSH 私钥和 `keys\known_hosts` 的文件权限。
- MCP 服务只提供 CarbonData 只读查询和受限导出能力；不要通过其它 Agent 配置绕过服务端的只读限制。
