"""CarbonLens MCP server entry point."""

from __future__ import annotations

import os
import sys
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover - exercised at startup only
    raise RuntimeError("未安装 MCP SDK，请执行 pip install -e '.[dev]'") from exc

from .jobs import QueryJobManager
from .exports import ExportJobManager
from .lifecycle import InstanceAlreadyRunning, InstanceLock, ParentProcessMonitor
from .service import CarbonLensService

app = FastMCP("carbon-lens")
_service: CarbonLensService | None = None
_query_jobs: QueryJobManager | None = None
_export_jobs: ExportJobManager | None = None


def service() -> CarbonLensService:
    global _service
    if _service is None:
        _service = CarbonLensService()
    return _service


def query_jobs() -> QueryJobManager:
    global _query_jobs
    if _query_jobs is None:
        _query_jobs = QueryJobManager(service)
    return _query_jobs


def export_jobs() -> ExportJobManager:
    global _export_jobs
    if _export_jobs is None:
        _export_jobs = ExportJobManager(service)
    return _export_jobs


@app.tool()
def carbon_list_connections() -> list[dict[str, str]]:
    """List configured Carbon connections without credentials."""
    return service().list_connections()


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
    return export_jobs().submit(
        connection,
        sql,
        format=format,
        filename=filename,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )


@app.tool()
def carbon_export_status(export_id: str) -> dict[str, Any]:
    """Refresh a background export and return its local file status and path."""
    return export_jobs().status(export_id)


@app.tool()
def carbon_export_list(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List persisted background export jobs without issuing Carbon SQL."""
    return export_jobs().list_jobs(status=status, limit=limit)


@app.tool()
def carbon_export_cancel(export_id: str) -> dict[str, Any]:
    """Cancel a background export job and its remote Carbon process."""
    return export_jobs().cancel(export_id)


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


def main() -> None:
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
