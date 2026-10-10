"""CarbonLens read-only query and export service."""

from __future__ import annotations

import csv
import json
import re
import time
import uuid
from pathlib import Path
from threading import Event
from typing import Any, Callable

from .beeline import (
    _bounded_text,
    build_export_script,
    build_script,
    is_row_returning_sql,
    parse_dsv,
    rows_as_dicts,
    unique_columns,
    validate_read_only,
)
from .config import MAX_TIMEOUT_SECONDS, CarbonLensError, ConfigStore
from .ssh import connect


def _bounded_export_sql(sql: str, limit: int) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    bounded_limit = max(1, int(limit))
    if is_row_returning_sql(cleaned):
        return f"SELECT * FROM ({cleaned}) AS carbon_lens_export LIMIT {bounded_limit};"
    return cleaned


def _csv_summary(path: Path) -> tuple[list[str], int]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        columns = next(reader, [])
        row_count = 0
        for row in reader:
            if len(row) != len(columns):
                raise CarbonLensError("CSV export contains a row with a different column count")
            row_count += 1
    return columns, row_count


class CarbonLensService:
    def __init__(self, config_store: ConfigStore | None = None):
        self.config_store = config_store or ConfigStore()

    def list_connections(self) -> list[dict[str, str]]:
        return self.config_store.public_list()

    def query(
        self,
        connection: str,
        sql: str,
        limit: int = 100,
        timeout_seconds: int | None = None,
        cancel_event: Event | None = None,
        session_callback: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        config = self.config_store.get(connection)
        validate_read_only(sql)
        if cancel_event is not None and cancel_event.is_set():
            raise CarbonLensError("Carbon 查询已取消")
        limit = max(1, min(int(limit), config.max_rows))
        timeout = (
            config.default_timeout
            if timeout_seconds is None
            else max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
        )
        started = time.monotonic()
        remote_sql = f"{config.remote_tmp_dir.rstrip('/')}/carbon-lens-{uuid.uuid4().hex}.sql"
        with connect(config) as session:
            if session_callback is not None:
                session_callback(session)
            if cancel_event is not None and cancel_event.is_set():
                session.cancel()
                raise CarbonLensError("Carbon 查询已取消")
            session.upload_text(remote_sql, sql)
            try:
                stdin, stdout, stderr = session.exec(
                    build_script(config, remote_sql),
                    timeout,
                    # A PTY causes Fiber's long table output to be terminal-
                    # wrapped and decorated with cursor-control sequences.
                    # `su` on the target accepts its password from stdin, so
                    # keep this channel non-interactive and parser-friendly.
                    get_pty=False,
                )
                if stdin is not None:
                    if config.root_password:
                        stdin.write(config.root_password + "\n")
                        stdin.flush()
                    stdin.close()
                try:
                    output = _bounded_text(stdout, config.max_output_bytes, cancel_event=cancel_event)
                except TimeoutError as exc:
                    raise CarbonLensError(
                        "Carbon query result read timed out; reduce the query scope or increase timeout_seconds"
                    ) from exc
                error_output = stderr.read().decode("utf-8", errors="replace") if stderr is not None else ""
                status = session.wait_exit(stdout, timeout, cancel_event=cancel_event)
                if status != 0:
                    detail = error_output.strip()[-4000:] or f"退出码 {status}"
                    raise CarbonLensError(f"fiber-beeline 执行失败：{detail}")
                columns, rows, truncated = parse_dsv(output, limit)
            finally:
                session.remove(remote_sql)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        result_columns, original_columns, column_name_map = unique_columns(columns)
        result_rows = rows_as_dicts(columns, rows, result_columns)
        return {
            "connection": connection,
            "transport": "ssh-beeline",
            "columns": result_columns,
            "original_columns": original_columns,
            "column_name_map": column_name_map,
            "rows": result_rows,
            "row_count": len(rows),
            "returned_row_count": len(rows),
            "total_row_count": None,
            "total_count_known": False,
            "has_more": truncated,
            "truncated": truncated,
            "type_info_unavailable": True,
            "elapsed_ms": elapsed_ms,
        }

    def describe(self, connection: str, table: str, timeout_seconds: int | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", table or ""):
            raise CarbonLensError("表名只能包含字母、数字、下划线和一个点")
        return self.query(connection, f"DESCRIBE {table}", limit=500, timeout_seconds=timeout_seconds)

    def describe_formatted(self, connection: str, table: str, timeout_seconds: int | None = None) -> dict[str, Any]:
        """Read Carbon table metadata, including TBLPROPERTIES, without rows."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", table or ""):
            raise CarbonLensError("表名只能包含字母、数字、下划线和一个点")
        return self.query(connection, f"DESC FORMATTED {table}", limit=500, timeout_seconds=timeout_seconds)

    def _export_csv(
        self,
        connection: str,
        sql: str,
        filename: str | None,
        limit: int,
        timeout_seconds: int | None,
    ) -> dict[str, Any]:
        config = self.config_store.get(connection)
        validate_read_only(sql)
        limit = max(1, min(int(limit), config.max_rows))
        timeout = (
            config.default_timeout
            if timeout_seconds is None
            else max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
        )
        config.export_dir.mkdir(parents=True, exist_ok=True)

        suffix = ".csv"
        requested = filename or f"carbon-export-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
        safe_name = Path(requested).name
        if not safe_name.lower().endswith(suffix):
            safe_name += suffix
        target = (config.export_dir / safe_name).resolve()
        if config.export_dir not in target.parents:
            raise CarbonLensError("export path must stay inside export_dir")

        remote_dir = f"{config.remote_tmp_dir.rstrip('/')}/carbon-lens-export-{uuid.uuid4().hex}"
        remote_sql = remote_dir + "/query.sql"
        remote_csv = remote_dir + "/result.csv"
        remote_log = remote_dir + "/result.log"
        local_part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        started = time.monotonic()
        try:
            with connect(config) as session:
                session.cleanup_stale_export_dirs(
                    config.remote_tmp_dir,
                    username=config.run_as_user,
                    password=config.root_password,
                    ttl_seconds=config.export_temp_ttl_seconds,
                )
                session.make_dir(remote_dir)
                session.upload_text(remote_sql, _bounded_export_sql(sql, limit))
                try:
                    stdin, stdout, stderr = session.exec(
                        build_export_script(
                            config,
                            remote_sql,
                            remote_csv,
                            remote_log,
                            remote_dir=remote_dir,
                            ttl_seconds=config.export_temp_ttl_seconds,
                        ),
                        timeout,
                        get_pty=False,
                    )
                    if stdin is not None:
                        if config.root_password:
                            stdin.write(config.root_password + "\n")
                            stdin.flush()
                        stdin.close()
                    status = session.wait_exit(stdout, timeout)
                    error_output = stderr.read().decode("utf-8", errors="replace") if stderr is not None else ""
                    if status != 0:
                        detail = error_output.strip()[-4000:] or f"exit code {status}"
                        raise CarbonLensError(f"fiber-beeline export failed: {detail}")
                    session.download_file(remote_csv, local_part)
                finally:
                    if config.run_as_user and config.root_password:
                        session.remove_privileged(
                            remote_dir,
                            config.run_as_user,
                            config.root_password,
                            recursive=True,
                        )
                    else:
                        session.remove(remote_sql)
                        session.remove(remote_csv)
                        session.remove(remote_log)
                        try:
                            with session.client.open_sftp() as sftp:
                                sftp.rmdir(remote_dir)
                        except Exception:
                            pass
            local_part.replace(target)
            columns, row_count = _csv_summary(target)
        except Exception:
            local_part.unlink(missing_ok=True)
            raise

        if row_count > limit:
            truncation: bool | str = True
        elif row_count == limit and is_row_returning_sql(sql):
            # The remote file was intentionally capped.  Reaching the cap is
            # not evidence that the source contained no additional rows.
            truncation = "unknown"
        else:
            truncation = False
        return {
            "connection": connection,
            "format": "csv",
            "path": str(target),
            "columns": columns,
            "row_count": row_count,
            "returned_row_count": row_count,
            "total_row_count": None,
            "total_count_known": False,
            "has_more": truncation,
            "truncated": truncation,
            "effective_limit": limit,
            "type_info_unavailable": True,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    def export(
        self,
        connection: str,
        sql: str,
        format: str = "csv",
        filename: str | None = None,
        limit: int = 10000,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        format = format.lower().strip()
        if format not in {"csv", "json"}:
            raise CarbonLensError("当前只支持 csv 或 json 导出")
        if format == "csv":
            return self._export_csv(connection, sql, filename, limit, timeout_seconds)
        result = self.query(connection, sql, limit=limit, timeout_seconds=timeout_seconds)
        config = self.config_store.get(connection)
        config.export_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".json"
        requested = filename or f"carbon-export-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
        safe_name = Path(requested).name
        if not safe_name.lower().endswith(suffix):
            safe_name += suffix
        target = (config.export_dir / safe_name).resolve()
        if config.export_dir not in target.parents:
            raise CarbonLensError("导出文件路径必须位于配置的 export_dir 内")
        part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            part.write_text(json.dumps(result["rows"], ensure_ascii=False, indent=2), encoding="utf-8")
            part.replace(target)
        except Exception:
            part.unlink(missing_ok=True)
            raise
        return {
            "connection": connection,
            "format": format,
            "path": str(target),
            "columns": result["columns"],
            "original_columns": result.get("original_columns", result["columns"]),
            "row_count": result["row_count"],
            "returned_row_count": result.get("returned_row_count", result["row_count"]),
            "total_row_count": result.get("total_row_count"),
            "total_count_known": result.get("total_count_known", False),
            "has_more": result.get("has_more", result["truncated"]),
            "truncated": result["truncated"],
            "effective_limit": max(1, min(int(limit), config.max_rows)),
            "type_info_unavailable": True,
            "elapsed_ms": result["elapsed_ms"],
        }
