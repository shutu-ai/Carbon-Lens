"""CarbonLens MCP server entry point."""

from __future__ import annotations

import os
import sys
import argparse
import ipaddress
import json
from pathlib import Path
import socket
from threading import Lock
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover - exercised at startup only
    raise RuntimeError("未安装 MCP SDK，请执行 pip install -e '.[dev]'") from exc

from .jobs import QueryJobManager
from .exports import ExportJobManager
from .executions import WriteExecutionManager
from .lifecycle import InstanceAlreadyRunning, InstanceLock, ParentProcessMonitor
from .service import CarbonLensService
from .http import HTTPServerSettings, build_http_app

app = FastMCP("carbon-lens")
_service: CarbonLensService | None = None
_query_jobs: QueryJobManager | None = None
_export_jobs: ExportJobManager | None = None
_write_executions: WriteExecutionManager | None = None
_manager_lock = Lock()
_http_settings: HTTPServerSettings | None = None


def service() -> CarbonLensService:
    global _service
    if _service is None:
        with _manager_lock:
            if _service is None:
                _service = CarbonLensService()
    return _service


def query_jobs() -> QueryJobManager:
    global _query_jobs
    if _query_jobs is None:
        with _manager_lock:
            if _query_jobs is None:
                _query_jobs = QueryJobManager(service)
    return _query_jobs


def export_jobs() -> ExportJobManager:
    global _export_jobs
    if _export_jobs is None:
        with _manager_lock:
            if _export_jobs is None:
                _export_jobs = ExportJobManager(service)
    return _export_jobs


def write_executions() -> WriteExecutionManager:
    global _write_executions
    if _write_executions is None:
        with _manager_lock:
            if _write_executions is None:
                _write_executions = WriteExecutionManager(service)
    return _write_executions


@app.tool()
def carbon_list_connections() -> list[dict[str, str]]:
    """List configured Carbon connections without credentials."""
    return service().list_connections()


@app.tool()
def carbon_workspace_schema_status(connection: str, timeout_seconds: int | None = None) -> dict[str, Any]:
    """Check whether the configured CarbonLens workspace schema exists."""
    return service().workspace_schema_status(connection, timeout_seconds=timeout_seconds)


@app.tool()
def carbon_workspace_schema_create(
    connection: str,
    confirm: bool = False,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Plan or explicitly create only the configured CarbonLens workspace schema."""
    return service().create_workspace_schema(
        connection,
        confirm=confirm,
        timeout_seconds=timeout_seconds,
    )


@app.tool()
def carbon_query(connection: str, sql: str, limit: int = 100, timeout_seconds: int | None = None) -> dict[str, Any]:
    """Run one bounded read-only Carbon SQL query through SSH and fiber-beeline."""
    return service().query(connection, sql, limit=limit, timeout_seconds=timeout_seconds)


@app.tool()
def carbon_query_start(
    connection: str,
    sql: str,
    limit: int = 100,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Start a bounded Carbon SQL query in the background and return its query ID."""
    return query_jobs().submit(
        connection,
        sql,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )


@app.tool()
def carbon_query_status(query_id: str) -> dict[str, Any]:
    """Return the status or completed result of a background Carbon query."""
    return query_jobs().status(query_id)


@app.tool()
def carbon_query_list(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List local background Carbon query jobs without issuing a Carbon query."""
    return query_jobs().list_jobs(status=status, limit=limit)


@app.tool()
def carbon_query_cancel(query_id: str) -> dict[str, Any]:
    """Cancel a queued or running background Carbon query."""
    return query_jobs().cancel(query_id)


@app.tool()
def carbon_export_start(
    connection: str,
    sql: str,
    format: str = "csv",
    filename: str | None = None,
    limit: int = 10000,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Start a background export and download the completed file to export_dir."""
    return _export_http_metadata(export_jobs().submit(
        connection,
        sql,
        format=format,
        filename=filename,
        limit=limit,
        timeout_seconds=timeout_seconds,
    ))


@app.tool()
def carbon_export_status(export_id: str) -> dict[str, Any]:
    """Refresh a background export and return its local file status and path."""
    return _export_http_metadata(export_jobs().status(export_id))


@app.tool()
def carbon_export_list(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List persisted background export jobs without issuing Carbon SQL."""
    result = export_jobs().list_jobs(status=status, limit=limit)
    result["exports"] = [_export_http_metadata(item) for item in result["exports"]]
    return result


@app.tool()
def carbon_export_cancel(export_id: str) -> dict[str, Any]:
    """Cancel a background export job and its remote Carbon process."""
    return export_jobs().cancel(export_id)


@app.tool()
def carbon_execute(
    connection: str,
    sql: str,
    idempotency_key: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Execute one approved managed-table CTAS/INSERT/DROP operation."""
    return write_executions().execute(
        connection, sql, idempotency_key=idempotency_key, timeout_seconds=timeout_seconds
    )


@app.tool()
def carbon_execute_start(
    connection: str,
    sql: str,
    idempotency_key: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Start one approved managed-table CTAS/INSERT/DROP operation."""
    return write_executions().submit(
        connection, sql, idempotency_key=idempotency_key, timeout_seconds=timeout_seconds
    )


@app.tool()
def carbon_execute_status(execution_id: str) -> dict[str, Any]:
    """Observe a managed-table write without resubmitting SQL."""
    return write_executions().status(execution_id)


@app.tool()
def carbon_execute_list(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List persisted managed-table write executions."""
    return write_executions().list_jobs(status=status, limit=limit)


@app.tool()
def carbon_execute_cancel(execution_id: str) -> dict[str, Any]:
    """Request cancellation of a managed-table write."""
    return write_executions().cancel(execution_id)


@app.tool()
def carbon_workspace_list(connection: str, state: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List only CarbonLens-registered intermediate tables."""
    return write_executions().workspace_list(connection, state=state, limit=limit)


@app.tool()
def carbon_workspace_create_start(
    connection: str,
    select_sql: str,
    idempotency_key: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Generate and start a uniquely named managed CTAS operation."""
    return write_executions().create_from_select(
        connection, select_sql, idempotency_key=idempotency_key, timeout_seconds=timeout_seconds
    )


@app.tool()
def carbon_workspace_create(
    connection: str,
    select_sql: str,
    idempotency_key: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Generate and synchronously complete a uniquely named managed CTAS operation."""
    return write_executions().create_from_select(
        connection, select_sql, idempotency_key=idempotency_key, timeout_seconds=timeout_seconds, wait=True
    )


@app.tool()
def carbon_describe(connection: str, table: str, timeout_seconds: int | None = None) -> dict[str, Any]:
    """Describe a Carbon table using a validated table identifier."""
    return service().describe(connection, table, timeout_seconds=timeout_seconds)


@app.tool()
def carbon_export(
    connection: str,
    sql: str,
    format: str = "csv",
    filename: str | None = None,
    limit: int = 10000,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Export bounded read-only Carbon query results to the server export directory."""
    return service().export(
        connection,
        sql,
        format=format,
        filename=filename,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )


def _export_http_metadata(result: dict[str, Any]) -> dict[str, Any]:
    if _http_settings is None:
        return result
    export_id = str(result.get("export_id") or "")
    base_url = _http_settings.base_url
    available = result.get("status") == "succeeded"
    if available:
        try:
            available = export_jobs().download_info(export_id)["path"].is_file()
        except Exception:
            available = False
    result = dict(result)
    result["download_available"] = bool(available)
    result["download_url"] = (
        f"{base_url}/exports/{export_id}/download" if available and base_url else None
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="carbon-lens", description="CarbonLens MCP server")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve", help="run a long-lived MCP Streamable HTTP server")
    serve.add_argument("--transport", choices=("streamable-http",), default="streamable-http")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--path")
    serve.add_argument("--public-base-url")
    serve.add_argument("--access-token", default="")
    serve.add_argument("--access-token-env")
    serve.add_argument("--allowed-host", action="append")
    serve.add_argument("--allowed-origin", action="append")
    serve.add_argument("--log-level")
    return parser


def _http_config_defaults() -> dict[str, Any]:
    configured = os.environ.get("CARBON_LENS_CONFIG") or "config.json"
    path = Path(configured).expanduser()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read HTTP settings from CARBON_LENS_CONFIG: {exc}") from exc
    http = payload.get("http") if isinstance(payload, dict) else None
    return dict(http) if isinstance(http, dict) else {}


def _http_settings_from_args(args: argparse.Namespace) -> HTTPServerSettings:
    configured = _http_config_defaults()
    host = str(args.host or os.environ.get("CARBON_LENS_HTTP_HOST") or configured.get("host") or "127.0.0.1").strip()
    port = int(args.port or os.environ.get("CARBON_LENS_HTTP_PORT") or configured.get("port") or 8765)
    path = str(args.path or os.environ.get("CARBON_LENS_HTTP_PATH") or configured.get("path") or "/mcp").strip()
    if not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("--path must be an absolute URL path without query or fragment")
    if not 1 <= port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    token_env = str(
        args.access_token_env
        or os.environ.get("CARBON_LENS_HTTP_ACCESS_TOKEN_ENV")
        or configured.get("access_token_env")
        or "CARBON_LENS_HTTP_ACCESS_TOKEN"
    )
    token = os.environ.get(token_env, "").strip() or str(args.access_token or configured.get("access_token") or "").strip()
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.casefold() == "localhost"
    if not is_loopback and not token:
        raise ValueError("non-loopback HTTP binds require --access-token or the configured token environment variable")
    configured_hosts = configured.get("allowed_hosts") or []
    configured_origins = configured.get("allowed_origins") or []
    allowed_hosts = tuple(args.allowed_host or configured_hosts or (["127.0.0.1:*", "localhost:*", "[::1]:*"] if is_loopback else []))
    allowed_origins = tuple(args.allowed_origin or configured_origins or (["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"] if is_loopback else []))
    if not is_loopback and not allowed_hosts:
        # Wildcard binds still accept private IP Host headers through the
        # middleware; explicit names can be added with --allowed-host.
        allowed_hosts = (f"{host}:*",) if host not in {"0.0.0.0", "::"} else ()
    return HTTPServerSettings(
        host=host,
        port=port,
        path=path,
        public_base_url=str(args.public_base_url or os.environ.get("CARBON_LENS_HTTP_PUBLIC_BASE_URL") or configured.get("public_base_url") or "").strip(),
        access_token=token,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        log_level=str(args.log_level or os.environ.get("CARBON_LENS_HTTP_LOG_LEVEL") or configured.get("log_level") or "INFO").upper(),
    )


def _run_http(settings: HTTPServerSettings) -> None:
    global _http_settings
    import anyio
    import uvicorn

    try:
        probe = socket.create_server((settings.host, settings.port), backlog=1)
    except OSError as exc:
        raise OSError(f"HTTP bind failed for {settings.host}:{settings.port}: {exc}") from exc
    else:
        probe.close()
    _http_settings = settings
    http_app = build_http_app(app, settings, export_jobs)
    server = uvicorn.Server(
        uvicorn.Config(http_app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())
    )
    lock = InstanceLock.for_http()
    with lock:
        anyio.run(server.serve)


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        try:
            _run_http(_http_settings_from_args(args))
        except KeyboardInterrupt:
            return
        except (InstanceAlreadyRunning, ValueError, OSError) as exc:
            print(f"CarbonLens HTTP startup failed: {exc}", file=sys.stderr, flush=True)
            raise SystemExit(16) from exc
        return
    parent_pid = os.getppid()
    instance_lock = InstanceLock.for_config(parent_pid=parent_pid)
    try:
        with instance_lock:
            parent_monitor = ParentProcessMonitor(parent_pid=parent_pid)
            parent_monitor.start()
            try:
                app.run(transport="stdio")
            finally:
                parent_monitor.stop()
    except InstanceAlreadyRunning as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(16) from exc


if __name__ == "__main__":
    main()
