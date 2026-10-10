#!/usr/bin/env python3
"""Run safe, repeatable Carbon-Lens live-environment acceptance checks.

The default modes only issue constant read-only SQL. Business-data checks are
opt-in and require an explicitly supplied SQL file and ``--allow-data``.
Evidence is written below .acceptance-data/, which is intentionally ignored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config.json"
TERMINAL_STATES = {"completed", "succeeded", "success", "failed", "cancelled", "canceled"}
SUCCESS_STATES = {"completed", "succeeded", "success"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
    """Remove credential-like fields before evidence is persisted."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("password", "secret", "token", "private_key", "keytab")) and isinstance(item, (str, bytes)):
                clean[str(key)] = "[REDACTED]"
            else:
                clean[str(key)] = json_safe(item)
        return clean
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def result_payload(result: Any) -> Any:
    """Normalize MCP SDK results across SDK minor versions."""
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


class LiveClient:
    def __init__(self, python: Path, config: Path, job_db: Path):
        self.python = python
        self.config = config
        self.job_db = job_db
        self.session: Any = None
        self._stdio: Any = None
        self._session_context: Any = None

    async def __aenter__(self) -> "LiveClient":
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ModuleNotFoundError as exc:
            raise RuntimeError("当前 Python 环境未安装 MCP SDK；请使用项目 .venv 运行") from exc

        env = os.environ.copy()
        env["CARBON_LENS_CONFIG"] = str(self.config)
        env["CARBON_LENS_JOB_DB"] = str(self.job_db)
        server = StdioServerParameters(
            command=str(self.python),
            args=[str(REPO_ROOT / "run_mcp.py")],
            env=env,
            cwd=str(REPO_ROOT),
        )
        self._stdio = stdio_client(server)
        read, write = await self._stdio.__aenter__()
        self._session_context = ClientSession(read, write)
        self.session = await self._session_context.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(exc_type, exc, tb)
        if self._stdio is not None:
            await self._stdio.__aexit__(exc_type, exc, tb)

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self.session.call_tool(name, arguments)
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            raise RuntimeError(json.dumps(json_safe(result_payload(result)), ensure_ascii=False))
        return result_payload(result)


def local_preflight(config: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "PASS",
        "config_path": str(config),
        "config_exists": config.exists(),
        "python": sys.executable,
    }
    if not config.exists():
        result.update(status="BLOCKED_ENV", reason="未找到 config.json；未启动 MCP live 检查")
        return result
    try:
        parsed = json.loads(config.read_text(encoding="utf-8"))
        connections_raw = parsed.get("connections", []) if isinstance(parsed, dict) else []
        if isinstance(connections_raw, dict):
            named_connections = list(connections_raw.items())
        else:
            named_connections = [(item.get("name"), item) for item in connections_raw if isinstance(item, dict)]
        connections = [(name, item) for name, item in named_connections if isinstance(item, dict)]
        result["connection_count"] = len(connections)
        result["connection_aliases"] = [name or item.get("name") for name, item in connections]

        def has_truthy_key(value: Any, target: str) -> bool:
            if isinstance(value, dict):
                return any(
                    (str(key).lower() == target and bool(item)) or has_truthy_key(item, target)
                    for key, item in value.items()
                )
            if isinstance(value, list):
                return any(has_truthy_key(item, target) for item in value)
            return False

        result["credentials_present"] = {
            "ssh_password": has_truthy_key(parsed, "ssh_password"),
            "root_password": has_truthy_key(parsed, "root_password"),
            "keytab": has_truthy_key(parsed, "keytab"),
        }
    except (OSError, json.JSONDecodeError) as exc:
        result.update(status="BLOCKED_ENV", reason=f"配置无法读取或解析：{type(exc).__name__}")
    return result


def summarize_query(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    return {
        key: json_safe(payload.get(key))
        for key in (
            "connection", "transport", "columns", "row_count", "returned_row_count",
            "total_row_count", "total_count_known", "has_more", "truncated", "elapsed_ms",
        )
        if key in payload
    }


async def poll(client: LiveClient, tool: str, identifier_key: str, identifier: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: Any = {}
    while time.monotonic() < deadline:
        latest = await client.call(tool, {identifier_key: identifier})
        state = str(latest.get("status", latest.get("state", ""))).lower() if isinstance(latest, dict) else ""
        if state in TERMINAL_STATES:
            return json_safe(latest)
        await asyncio.sleep(1)
    raise TimeoutError(f"{tool} 未在 {timeout}s 内进入终态")


async def run_live(args: argparse.Namespace, evidence_dir: Path) -> dict[str, Any]:
    config = Path(args.config).resolve()
    python = Path(args.python).resolve()
    job_db = evidence_dir / "jobs.sqlite3"
    output: dict[str, Any] = {"started_at": utc_now(), "mode": args.mode, "checks": []}
    output["preflight"] = local_preflight(config)
    if output["preflight"]["status"] != "PASS":
        return output

    try:
        async with LiveClient(python, config, job_db) as client:
            connections = await client.call("carbon_list_connections", {})
            public_connections = []
            for item in connections if isinstance(connections, list) else []:
                if isinstance(item, dict):
                    public_connections.append({key: item.get(key) for key in ("name", "transport", "connection_mode") if key in item})
            output["connections"] = json_safe(public_connections)
            if not public_connections:
                output["checks"].append({"name": "connection-discovery", "status": "BLOCKED_ENV"})
                return output
            connection = args.connection or public_connections[0].get("name")

            if args.mode in {"smoke", "all"}:
                payload = await client.call("carbon_query", {"connection": connection, "sql": "SELECT 1 AS acceptance_value", "limit": 1, "timeout_seconds": 120})
                output["checks"].append({"name": "mcp-ssh-beeline-smoke", "status": "PASS", "result": summarize_query(payload)})

            if args.mode in {"jobs", "all"}:
                started = await client.call("carbon_query_start", {"connection": connection, "sql": "SELECT 1 AS acceptance_value", "limit": 1, "timeout_seconds": 120})
                query_id = started.get("query_id") if isinstance(started, dict) else None
                if not query_id:
                    raise RuntimeError("异步查询未返回 query_id")
                completed = await poll(client, "carbon_query_status", "query_id", query_id, args.poll_timeout)
                status = str(completed.get("status", "")).lower()
                output["checks"].append({"name": "async-query-persistence-smoke", "status": "PASS" if status in SUCCESS_STATES else "FAIL", "result": completed})

            if args.mode in {"exports", "all"}:
                export_checks = []
                for fmt in ("csv", "json"):
                    filename = f"live-{evidence_dir.name.removeprefix('live-')}-{fmt}.{fmt}"
                    started = await client.call("carbon_export_start", {"connection": connection, "sql": "SELECT 1 AS acceptance_value", "format": fmt, "filename": filename, "limit": 1, "timeout_seconds": 120})
                    export_id = started.get("export_id") if isinstance(started, dict) else None
                    if not export_id:
                        raise RuntimeError(f"{fmt} 导出未返回 export_id")
                    completed = await poll(client, "carbon_export_status", "export_id", export_id, args.poll_timeout)
                    status = str(completed.get("status", "")).lower()
                    export_checks.append({"format": fmt, "status": "PASS" if status in SUCCESS_STATES else "FAIL", "result": completed})
                output["checks"].append({"name": "bounded-constant-export-csv-json", "status": "PASS" if all(item["status"] == "PASS" for item in export_checks) else "FAIL", "result": export_checks})

            if args.mode in {"benchmark", "all"}:
                timings = []
                for _ in range(args.repetitions):
                    started_at = time.monotonic()
                    payload = await client.call("carbon_query", {"connection": connection, "sql": "SELECT 1 AS acceptance_value", "limit": 1, "timeout_seconds": 120})
                    timings.append({"wall_ms": int((time.monotonic() - started_at) * 1000), "query": summarize_query(payload)})
                output["checks"].append({"name": "safe-smoke-timing-only", "status": "PASS", "result": {"repetitions": args.repetitions, "timings": timings, "note": "不是业务数据性能结论"}})

            if args.mode == "correctness":
                if not args.allow_data or not args.sql_file:
                    output["checks"].append({"name": "business-data-correctness", "status": "BLOCKED_ENV", "reason": "需要明确授权测试 SQL 文件并显式传入 --allow-data"})
                else:
                    sql = Path(args.sql_file).read_text(encoding="utf-8")
                    payload = await client.call("carbon_query", {"connection": connection, "sql": sql, "limit": args.limit, "timeout_seconds": 120})
                    output["checks"].append({"name": "business-data-correctness", "status": "UNVERIFIED", "result": summarize_query(payload), "note": "结果需由业务验收人按测试集核对"})
    except Exception as exc:  # noqa: BLE001 - evidence must capture environmental failures
        output["checks"].append({"name": "live-runner", "status": "BLOCKED_ENV", "reason": f"{type(exc).__name__}: {str(exc)[:500]}"})
    finally:
        output["finished_at"] = utc_now()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "smoke", "jobs", "exports", "benchmark", "correctness", "all"), default="all")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--connection")
    parser.add_argument("--sql-file")
    parser.add_argument("--allow-data", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--poll-timeout", type=int, default=180)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    evidence_dir = REPO_ROOT / ".acceptance-data" / f"live-{run_id}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "preflight":
        evidence = {"started_at": utc_now(), "mode": args.mode, "preflight": local_preflight(Path(args.config).resolve()), "checks": []}
        evidence["finished_at"] = utc_now()
    else:
        evidence = asyncio.run(run_live(args, evidence_dir))
    evidence["run_id"] = run_id
    evidence_path = evidence_dir / "results.json"
    evidence_path.write_text(json.dumps(json_safe(evidence), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": run_id, "evidence": str(evidence_path), "mode": args.mode, "checks": evidence.get("checks", [])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
