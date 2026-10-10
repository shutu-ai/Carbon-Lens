#!/usr/bin/env python3
"""Run safe, repeatable Carbon-Lens live-environment acceptance checks.

The default checks use only bounded, read-only ``SELECT 1`` SQL.  Business SQL
is opt-in and must be supplied with ``--allow-data``.  Evidence contains
metadata and hashes, never raw result rows or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import statistics
import subprocess
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
KNOWN_CHECK_STATES = {"PASS", "FAIL", "BLOCKED_ENV", "UNVERIFIED", "NOT_RUN"}


class AcceptanceError(RuntimeError):
    def __init__(self, message: str, status: str = "FAIL", **details: Any):
        super().__init__(message)
        self.status = status
        self.details = details


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
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
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and set(parsed) == {"result"}:
                    return parsed["result"]
                return parsed
            except json.JSONDecodeError:
                return {"text": text}
    return {}


def baseline_sha() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True,
            capture_output=True, text=True,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def local_preflight(config: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "PASS", "config_path": str(config),
        "config_exists": config.exists(), "python": sys.executable,
    }
    if not config.exists():
        result.update(status="BLOCKED_ENV", reason="config.json not found")
        return result
    try:
        parsed = json.loads(config.read_text(encoding="utf-8"))
        raw = parsed.get("connections", []) if isinstance(parsed, dict) else []
        named = list(raw.items()) if isinstance(raw, dict) else [
            (item.get("name"), item) for item in raw if isinstance(item, dict)
        ]
        result["connection_count"] = len(named)
        result["connection_aliases"] = [name or item.get("name") for name, item in named]
        result["credentials_present"] = {
            key: key in json.dumps(parsed, ensure_ascii=False).lower()
            for key in ("ssh_password", "root_password", "keytab")
        }
    except (OSError, json.JSONDecodeError) as exc:
        result.update(status="BLOCKED_ENV", reason=f"config unreadable: {type(exc).__name__}")
    return result


def summarize_query(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    keys = (
        "connection", "transport", "columns", "row_count", "returned_row_count",
        "total_row_count", "total_count_known", "has_more", "truncated", "elapsed_ms",
    )
    return {key: json_safe(payload[key]) for key in keys if key in payload}


def summarize_export(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    keys = ("connection", "format", "path", "file_path", "columns", "row_count", "returned_row_count", "truncated", "elapsed_ms", "status")
    return {key: json_safe(payload[key]) for key in keys if key in payload}


def validate_export_file(payload: dict[str, Any], fmt: str) -> dict[str, Any]:
    raw_path = payload.get("path") or payload.get("file_path") or payload.get("local_path")
    if not raw_path:
        raise AcceptanceError(f"{fmt} export did not return a local file path", "FAIL")
    path = Path(str(raw_path))
    if not path.is_file() or not os.access(path, os.R_OK):
        raise AcceptanceError(f"{fmt} export file is missing or unreadable", "FAIL", path=str(path))
    if path.stat().st_size == 0:
        raise AcceptanceError(f"{fmt} export file is empty", "FAIL", path=str(path))
    if fmt == "csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            columns = next(reader, [])
            if not columns or any(not column for column in columns):
                raise AcceptanceError("CSV export has no valid header", "FAIL", path=str(path))
            rows = 0
            for row in reader:
                if len(row) != len(columns):
                    raise AcceptanceError("CSV export has inconsistent columns", "FAIL", path=str(path))
                rows += 1
        return {"path": str(path), "format": fmt, "columns": columns, "row_count": rows, "bytes": path.stat().st_size}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"JSON export is not valid JSON: {type(exc).__name__}", "FAIL", path=str(path)) from exc
    if not isinstance(decoded, list):
        raise AcceptanceError("JSON export root must be an array", "FAIL", path=str(path))
    columns = sorted({str(key) for row in decoded if isinstance(row, dict) for key in row})
    if any(not isinstance(row, dict) for row in decoded):
        raise AcceptanceError("JSON export contains a non-object row", "FAIL", path=str(path))
    return {"path": str(path), "format": fmt, "columns": columns, "row_count": len(decoded), "bytes": path.stat().st_size}


def canonical_digest(payload: dict[str, Any]) -> str | None:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class LiveClient:
    def __init__(self, python: Path, config: Path, job_db: Path):
        self.python, self.config, self.job_db = python, config, job_db
        self.session: Any = None
        self._stdio: Any = None
        self._session_context: Any = None

    async def __aenter__(self) -> "LiveClient":
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ModuleNotFoundError as exc:
            raise AcceptanceError("MCP SDK is unavailable in the selected Python", "BLOCKED_ENV") from exc
        env = os.environ.copy()
        env["CARBON_LENS_CONFIG"] = str(self.config)
        env["CARBON_LENS_JOB_DB"] = str(self.job_db)
        server = StdioServerParameters(
            command=str(self.python), args=[str(REPO_ROOT / "run_mcp.py")],
            env=env, cwd=str(REPO_ROOT),
        )
        self._stdio = stdio_client(server)
        read, write = await self._stdio.__aenter__()
        self._session_context = ClientSession(read, write)
        self.session = await self._session_context.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Some MCP/AnyIO versions can raise a secondary cancel-scope error
        # while unwinding a timed-out stdio call.  Never replace the original
        # acceptance failure with that cleanup noise.
        cleanup_error: Exception | None = None
        if self._session_context is not None:
            try:
                await self._session_context.__aexit__(exc_type, exc, tb)
            except Exception as cleanup_exc:  # pragma: no cover - SDK-specific
                cleanup_error = cleanup_exc
        if self._stdio is not None:
            try:
                await self._stdio.__aexit__(exc_type, exc, tb)
            except Exception as cleanup_exc:  # pragma: no cover - SDK-specific
                cleanup_error = cleanup_error or cleanup_exc
        if cleanup_error is not None and exc_type is None:
            raise cleanup_error

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self.session.call_tool(name, arguments)
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            raise AcceptanceError(f"{name} returned an MCP error", "BLOCKED_ENV", detail=json_safe(result_payload(result)))
        return result_payload(result)


async def poll(client: LiveClient, tool: str, identifier_key: str, identifier: str, timeout: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        response = await client.call(tool, {identifier_key: identifier})
        latest = response if isinstance(response, dict) else {"payload": response}
        trace.append({key: json_safe(latest.get(key)) for key in ("status", "state", "elapsed_ms", "error") if key in latest})
        state = str(latest.get("status", latest.get("state", ""))).lower()
        if state in TERMINAL_STATES:
            return latest, trace
        await asyncio.sleep(1)
    raise AcceptanceError(f"{tool} timed out; last status retained", "FAIL", last=latest, trace=trace)


def check_error(name: str, exc: Exception) -> dict[str, Any]:
    status = getattr(exc, "status", "BLOCKED_ENV" if isinstance(exc, (TimeoutError, OSError)) else "FAIL")
    item: dict[str, Any] = {"name": name, "status": status, "reason": str(exc)[:500]}
    details = getattr(exc, "details", {})
    if details:
        item["details"] = json_safe(details)
    return item


async def run_restart(args: argparse.Namespace, config: Path, python: Path, job_db: Path, connection: str) -> list[dict[str, Any]]:
    started: dict[str, Any]
    client_a = LiveClient(python, config, job_db)
    try:
        await client_a.__aenter__()
        started = await client_a.call("carbon_query_start", {"connection": connection, "sql": "SELECT 1 AS acceptance_value", "limit": 1, "timeout_seconds": args.remote_timeout})
    finally:
        await client_a.__aexit__(None, None, None)
    query_id = started.get("query_id") if isinstance(started, dict) else None
    if not query_id:
        raise AcceptanceError("restart check did not receive query_id", "FAIL")
    client_b = LiveClient(python, config, job_db)
    try:
        await client_b.__aenter__()
        completed, trace = await poll(client_b, "carbon_query_status", "query_id", query_id, args.poll_timeout)
        listed = await client_b.call("carbon_query_list", {"limit": 100})
    finally:
        await client_b.__aexit__(None, None, None)
    entries = listed.get("jobs", listed.get("items", listed)) if isinstance(listed, dict) else listed
    ids = {item.get("query_id") for item in entries if isinstance(item, dict)} if isinstance(entries, list) else set()
    state = str(completed.get("status", "")).lower()
    return [{
        "name": "cross-process-query-restart", "status": "PASS" if state in SUCCESS_STATES and query_id in ids else "FAIL",
        "result": {"query_id": query_id, "status": state, "server_a_closed": True, "server_b_reopened": True, "listed_after_restart": query_id in ids, "trace": trace},
    }]


async def run_live(args: argparse.Namespace, evidence_dir: Path) -> dict[str, Any]:
    config, python = Path(args.config).resolve(), Path(args.python).resolve()
    job_db = evidence_dir / "jobs.sqlite3"
    output: dict[str, Any] = {"started_at": utc_now(), "mode": args.mode, "checks": [], "job_db": str(job_db)}
    output["preflight"] = local_preflight(config)
    if output["preflight"]["status"] != "PASS":
        return output
    try:
        async with LiveClient(python, config, job_db) as client:
            connections = await client.call("carbon_list_connections", {})
            public_connections = [
                {key: item.get(key) for key in ("name", "transport", "connection_mode") if key in item}
                for item in connections if isinstance(item, dict)
            ] if isinstance(connections, list) else []
            output["connections"] = json_safe(public_connections)
            if not public_connections:
                output["checks"].append({"name": "connection-discovery", "status": "BLOCKED_ENV", "reason": "no configured connection"})
                return output
            connection = args.connection or public_connections[0].get("name")

            if args.mode in {"smoke", "all"}:
                payload = await client.call("carbon_query", {"connection": connection, "sql": "SELECT 1 AS acceptance_value", "limit": 1, "timeout_seconds": args.remote_timeout})
                output["checks"].append({"name": "mcp-ssh-beeline-smoke", "status": "PASS", "result": summarize_query(payload)})

            if args.mode in {"jobs", "all"}:
                started = await client.call("carbon_query_start", {"connection": connection, "sql": "SELECT 1 AS acceptance_value", "limit": 1, "timeout_seconds": args.remote_timeout})
                query_id = started.get("query_id") if isinstance(started, dict) else None
                if not query_id:
                    raise AcceptanceError("async query did not return query_id", "FAIL")
                completed, trace = await poll(client, "carbon_query_status", "query_id", query_id, args.poll_timeout)
                state = str(completed.get("status", "")).lower()
                output["checks"].append({"name": "async-query-persistence-smoke", "status": "PASS" if state in SUCCESS_STATES else "FAIL", "result": {"query_id": query_id, "status": state, "trace": trace, "summary": summarize_query(completed)}})

            if args.mode in {"exports", "all"}:
                results = []
                formats = ("csv", "json") if args.format == "both" else (args.format,)
                for fmt in formats:
                    filename = f"live-{evidence_dir.name.removeprefix('live-')}-{fmt}.{fmt}"
                    started = await client.call("carbon_export_start", {"connection": connection, "sql": "SELECT 1 AS acceptance_value", "format": fmt, "filename": filename, "limit": 1, "timeout_seconds": args.remote_timeout})
                    export_id = started.get("export_id") if isinstance(started, dict) else None
                    if not export_id:
                        raise AcceptanceError(f"{fmt} export did not return export_id", "FAIL")
                    completed, trace = await poll(client, "carbon_export_status", "export_id", export_id, args.poll_timeout)
                    state = str(completed.get("status", "")).lower()
                    validation = validate_export_file(completed, fmt) if state in SUCCESS_STATES else {}
                    results.append({"format": fmt, "export_id": export_id, "status": "PASS" if state in SUCCESS_STATES else "FAIL", "trace": trace, "summary": summarize_export(completed), "file": validation})
                output["checks"].append({"name": "bounded-constant-export-csv-json", "status": "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL", "result": results})

            if args.mode in {"benchmark", "all"}:
                timings: list[int] = []
                for _ in range(max(1, args.repetitions)):
                    began = time.monotonic()
                    payload = await client.call("carbon_query", {"connection": connection, "sql": "SELECT 1 AS acceptance_value", "limit": 1, "timeout_seconds": args.remote_timeout})
                    timings.append(int((time.monotonic() - began) * 1000))
                ordered = sorted(timings)
                p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
                output["checks"].append({"name": "safe-smoke-timing-only", "status": "PASS", "result": {"repetitions": len(timings), "median_ms": int(statistics.median(timings)), "p95_ms": p95, "max_ms": max(timings), "samples_ms": timings, "note": "not a business-data SLA"}})

            if args.mode == "correctness":
                if not args.allow_data or not args.sql_file:
                    output["checks"].append({"name": "business-data-correctness", "status": "BLOCKED_ENV", "reason": "requires --allow-data and --sql-file"})
                else:
                    sql = Path(args.sql_file).read_text(encoding="utf-8")
                    payload = await client.call("carbon_query", {"connection": connection, "sql": sql, "limit": args.limit, "timeout_seconds": args.remote_timeout})
                    item = {"name": "business-data-correctness", "status": "UNVERIFIED", "result": summarize_query(payload), "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(), "result_sha256": canonical_digest(payload), "note": "expected native-Beeline output was not supplied"}
                    if args.expected_file:
                        expected = Path(args.expected_file).read_bytes()
                        actual = json.dumps(payload.get("rows", []), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        item["status"] = "PASS" if hashlib.sha256(expected).hexdigest() == hashlib.sha256(actual).hexdigest() else "FAIL"
                        item["expected_sha256"] = hashlib.sha256(expected).hexdigest()
                    output["checks"].append(item)
    except Exception as exc:  # noqa: BLE001 - evidence must retain the exact gate failure
        output["checks"].append(check_error("live-runner", exc))
    finally:
        output["finished_at"] = utc_now()
    return output


def finalize(evidence: dict[str, Any]) -> dict[str, Any]:
    checks = evidence.get("checks", [])
    states = [item.get("status", "NOT_RUN") for item in checks if isinstance(item, dict)]
    if any(state == "FAIL" for state in states):
        status, code = "FAIL", 1
    elif not states or all(state in {"BLOCKED_ENV", "NOT_RUN"} for state in states):
        status, code = "BLOCKED_ENV", 2
    elif any(state in {"BLOCKED_ENV", "UNVERIFIED", "NOT_RUN"} for state in states):
        status, code = "PARTIAL", 2
    else:
        status, code = "PASS", 0
    evidence["overall_status"] = status
    evidence["exit_code"] = code
    evidence["summary"] = {"check_count": len(states), "pass": states.count("PASS"), "fail": states.count("FAIL"), "blocked_env": states.count("BLOCKED_ENV"), "unverified": states.count("UNVERIFIED")}
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "smoke", "jobs", "exports", "benchmark", "correctness", "restart", "all"), default="all")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--connection")
    parser.add_argument("--sql-file")
    parser.add_argument("--expected-file")
    parser.add_argument("--allow-data", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--remote-timeout", type=int, default=120)
    parser.add_argument("--poll-timeout", type=int, default=180)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--format", choices=("csv", "json", "both"), default="both")
    args = parser.parse_args()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    evidence_dir = REPO_ROOT / ".acceptance-data" / f"live-{run_id}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "preflight":
        evidence = {"started_at": utc_now(), "mode": args.mode, "preflight": local_preflight(Path(args.config).resolve()), "checks": [], "finished_at": utc_now()}
        evidence["checks"].append({"name": "local-preflight", "status": evidence["preflight"]["status"]})
    elif args.mode == "restart":
        evidence = {"started_at": utc_now(), "mode": args.mode, "checks": [], "job_db": str(evidence_dir / "jobs.sqlite3"), "preflight": local_preflight(Path(args.config).resolve())}
        try:
            if evidence["preflight"]["status"] != "PASS":
                raise AcceptanceError(evidence["preflight"].get("reason", "preflight blocked"), "BLOCKED_ENV")
            async def discover() -> list[dict[str, Any]]:
                async with LiveClient(Path(args.python).resolve(), Path(args.config).resolve(), evidence_dir / "jobs.sqlite3") as client:
                    return await client.call("carbon_list_connections", {})
            connections = asyncio.run(discover())
            selected = args.connection or (connections[0].get("name") if connections else None)
            if not selected:
                raise AcceptanceError("no configured connection", "BLOCKED_ENV")
            evidence["connections"] = [{"name": item.get("name"), "transport": item.get("transport")} for item in connections if isinstance(item, dict)]
            evidence["checks"] = asyncio.run(run_restart(args, Path(args.config).resolve(), Path(args.python).resolve(), evidence_dir / "jobs.sqlite3", selected))
        except Exception as exc:  # noqa: BLE001
            evidence["checks"].append(check_error("cross-process-query-restart", exc))
        evidence["finished_at"] = utc_now()
    else:
        evidence = asyncio.run(run_live(args, evidence_dir))
    evidence["run_id"] = run_id
    evidence["baseline_sha"] = baseline_sha()
    evidence = finalize(evidence)
    evidence_path = evidence_dir / "results.json"
    evidence_path.write_text(json.dumps(json_safe(evidence), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": run_id, "evidence": str(evidence_path), "mode": args.mode, "overall_status": evidence["overall_status"], "exit_code": evidence["exit_code"], "summary": evidence["summary"]}, ensure_ascii=False, indent=2))
    return int(evidence["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
