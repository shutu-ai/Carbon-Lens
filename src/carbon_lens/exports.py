"""Persistent background export jobs with automatic SFTP download."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
import shlex
from pathlib import Path
import sqlite3
from threading import RLock
import time
import uuid
from typing import Any, Callable

from .beeline import (
    EXPORT_CANCELLED,
    EXPORT_FAILED,
    EXPORT_RUNNING,
    EXPORT_SUCCEEDED,
    build_export_job_script,
    build_query_job_cleanup_command,
    build_query_job_launch_command,
    unique_columns,
    validate_read_only,
)
from .config import MAX_TIMEOUT_SECONDS, CarbonLensError
from .service import CarbonLensService, _bounded_export_sql, _csv_summary
from .ssh import connect


ASYNC_EXPORT_TTL_SECONDS = 30 * 24 * 60 * 60
REMOTE_EXPORT_TTL_SECONDS = 2 * 24 * 60 * 60
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
KNOWN_STATUSES = {"queued", "starting", "running", "downloading", "cancel_requested", *TERMINAL_STATUSES}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExportJobManager:
    """Persist remote export jobs and pull completed files to export_dir."""

    def __init__(self, service_factory: Callable[[], CarbonLensService]):
        self._service_factory = service_factory
        service = service_factory()
        configured = os.environ.get("CARBON_LENS_JOB_DB", "").strip()
        database = (
            Path(configured).expanduser()
            if configured
            else service.config_store.path.parent / ".carbon-lens-jobs.sqlite3"
        )
        if not database.is_absolute():
            database = service.config_store.path.parent / database
        self._db_path = database.resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._init_db()

    def submit(
        self,
        connection: str,
        sql: str,
        format: str = "csv",
        filename: str | None = None,
        limit: int = 10000,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        validate_read_only(sql)
        format = format.lower().strip()
        if format not in {"csv", "json"}:
            raise CarbonLensError("当前只支持 csv 或 json 导出")
        config = self._service_factory().config_store.get(connection)
        requested_limit = int(limit)
        effective_limit = max(1, min(requested_limit, config.max_rows))
        effective_timeout = (
            config.default_timeout
            if timeout_seconds is None
            else max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
        )
        target = self._target_path(config, format, filename)
        export_id = "ce_" + uuid.uuid4().hex
        remote_dir = f"{config.remote_tmp_dir.rstrip('/')}/carbon-lens-export-{uuid.uuid4().hex}"
        now = time.time()
        with self._lock:
            self._insert_job(
                {
                    "export_id": export_id,
                    "connection": connection,
                    "sql": sql,
                    "format": format,
                    "filename": target.name,
                    "requested_limit": requested_limit,
                    "effective_limit": effective_limit,
                    "requested_timeout_seconds": timeout_seconds,
                    "effective_timeout_seconds": effective_timeout,
                    "status": "queued",
                    "download_status": "pending",
                    "created_at": _timestamp(),
                    "created_epoch": now,
                    "started_at": None,
                    "started_epoch": None,
                    "finished_at": None,
                    "finished_epoch": None,
                    "remote_dir": remote_dir,
                    "remote_pid": None,
                    "local_path": str(target),
                    "columns_json": None,
                    "source_columns_json": None,
                    "row_count": None,
                    "file_size": None,
                    "truncated": None,
                    "error": None,
                }
            )
            self._update_job(export_id, status="starting")
            try:
                remote_pid = self._start_remote_job(
                    config,
                    remote_dir,
                    sql,
                    effective_limit,
                    effective_timeout,
                )
            except Exception as exc:
                try:
                    with connect(config) as session:
                        self._cleanup_remote(session, config, remote_dir)
                except Exception:
                    pass
                self._mark_failed(export_id, f"{type(exc).__name__}: {exc}")
            else:
                self._update_job(
                    export_id,
                    status="running",
                    started_at=_timestamp(),
                    started_epoch=time.time(),
                    remote_pid=remote_pid or None,
                )
            return self._snapshot(self._get_job(export_id))

    def status(self, export_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._get_job(export_id)
            if row["status"] in TERMINAL_STATUSES:
                return self._snapshot(row)
            return self._refresh_remote(row)

    def list_jobs(self, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        if status is not None and status not in KNOWN_STATUSES:
            raise CarbonLensError(f"Unknown background export status: {status}")
        limit = max(1, min(int(limit), 100))
        with self._lock:
            self._prune_jobs()
            conn = self._connect_db()
            try:
                if status is None:
                    rows = conn.execute(
                        "SELECT * FROM export_jobs ORDER BY created_epoch DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    count = conn.execute("SELECT COUNT(*) FROM export_jobs").fetchone()[0]
                else:
                    rows = conn.execute(
                        "SELECT * FROM export_jobs WHERE status = ? ORDER BY created_epoch DESC LIMIT ?",
                        (status, limit),
                    ).fetchall()
                    count = conn.execute(
                        "SELECT COUNT(*) FROM export_jobs WHERE status = ?",
                        (status,),
                    ).fetchone()[0]
            finally:
                conn.close()
            return {
                "exports": [self._snapshot(row) for row in rows],
                "count": int(count),
                "returned_count": len(rows),
            }

    def cancel(self, export_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._get_job(export_id)
            if row["status"] in TERMINAL_STATUSES:
                return self._snapshot(row)
            self._update_job(export_id, status="cancel_requested", error=None)
            config = self._service_factory().config_store.get(row["connection"])
            try:
                with connect(config) as session:
                    session.upload_text(self._cancel_path(row), "CANCEL\n")
            except Exception as exc:
                self._update_job(export_id, error=f"{type(exc).__name__}: {exc}")
            return self._snapshot(self._get_job(export_id))

    def _start_remote_job(
        self,
        config: Any,
        remote_dir: str,
        sql: str,
        effective_limit: int,
        timeout_seconds: int,
    ) -> str:
        remote_sql = self._sql_path(remote_dir)
        remote_output = self._output_path(remote_dir)
        remote_log = self._log_path(remote_dir)
        remote_status = self._status_path(remote_dir)
        remote_pid = self._pid_path_from_dir(remote_dir)
        remote_cancel = self._cancel_path_from_dir(remote_dir)
        with connect(config) as session:
            session.make_dir(remote_dir)
            session.upload_text(remote_sql, _bounded_export_sql(sql, effective_limit))
            script = build_export_job_script(
                config,
                remote_sql,
                remote_output,
                remote_log,
                remote_status,
                remote_pid,
                remote_cancel,
                remote_dir=remote_dir,
                ttl_seconds=REMOTE_EXPORT_TTL_SECONDS,
                timeout_seconds=timeout_seconds,
            )
            command = build_query_job_launch_command(config, script)
            return session.start_detached(
                command,
                password=config.root_password if config.run_as_user else "",
            )

    def _refresh_remote(self, row: sqlite3.Row) -> dict[str, Any]:
        config = self._service_factory().config_store.get(row["connection"])
        with connect(config) as session:
            state_text = session.read_text(self._status_path(row["remote_dir"]))
            state = (state_text or "").strip().upper()
            remote_pid = session.read_text(self._pid_path(row))
            if remote_pid and remote_pid.strip() != (row["remote_pid"] or ""):
                self._update_job(row["export_id"], remote_pid=remote_pid.strip())
                row = self._get_job(row["export_id"])

            # A remote process can remain in RUNNING or CANCEL_REQUESTED
            # forever if the scheduler, Beeline, or the SSH-launched wrapper
            # stops making progress.  Check the deadline before either fast
            # path so such jobs do not become immortal local records.
            if self._has_timed_out(row):
                diagnostic = self._read_remote_log(session, row["remote_dir"])
                detail = "Remote export did not complete before timeout_seconds"
                if diagnostic and diagnostic.strip():
                    detail += f": {diagnostic.strip()[-2000:]}"
                self._mark_failed(row["export_id"], detail)
                self._cleanup_remote(session, config, row["remote_dir"])
                return self._snapshot(self._get_job(row["export_id"]))

            if row["status"] == "cancel_requested":
                if state == EXPORT_SUCCEEDED:
                    return self._complete_success(session, config, row)
                if state == EXPORT_FAILED:
                    error = session.read_text(self._log_path(row["remote_dir"]), max_bytes=4000) or "Remote export failed"
                    self._mark_failed(row["export_id"], error.strip()[-4000:])
                    self._cleanup_remote(session, config, row["remote_dir"])
                    return self._snapshot(self._get_job(row["export_id"]))
                if state == EXPORT_CANCELLED:
                    self._mark_cancelled(row["export_id"])
                    self._cleanup_remote(session, config, row["remote_dir"])
                    return self._snapshot(self._get_job(row["export_id"]))
                return self._snapshot(row)

            if state == EXPORT_RUNNING:
                if row["status"] in {"queued", "starting"}:
                    self._update_job(
                        row["export_id"],
                        status="running",
                        started_at=row["created_at"],
                        started_epoch=row["created_epoch"],
                    )
                    row = self._get_job(row["export_id"])
                return self._snapshot(row)

            if state == EXPORT_SUCCEEDED:
                return self._complete_success(session, config, row)

            if state == EXPORT_FAILED:
                error = session.read_text(self._log_path(row["remote_dir"]), max_bytes=4000) or "Remote export failed"
                self._mark_failed(row["export_id"], error.strip()[-4000:])
                self._cleanup_remote(session, config, row["remote_dir"])
                return self._snapshot(self._get_job(row["export_id"]))

            if state == EXPORT_CANCELLED:
                self._mark_cancelled(row["export_id"])
                self._cleanup_remote(session, config, row["remote_dir"])
                return self._snapshot(self._get_job(row["export_id"]))

            return self._snapshot(row)

    def _complete_success(self, session: Any, config: Any, row: sqlite3.Row) -> dict[str, Any]:
        target = Path(row["local_path"])
        remote_part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.csv.part")
        final_part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        self._update_job(row["export_id"], status="downloading", download_status="running", error=None)
        try:
            session.download_file(self._output_path(row["remote_dir"]), remote_part)
            source_columns, row_count = _csv_summary(remote_part)
            if row["format"] == "csv":
                columns = source_columns
                remote_part.replace(final_part)
            else:
                columns, source_columns = self._csv_to_json(remote_part, final_part)
                remote_part.unlink(missing_ok=True)
            final_part.replace(target)
            file_size = target.stat().st_size
        except Exception as exc:
            remote_part.unlink(missing_ok=True)
            final_part.unlink(missing_ok=True)
            self._update_job(
                row["export_id"],
                status="downloading",
                download_status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._snapshot(self._get_job(row["export_id"]))

        finished_at = _timestamp()
        finished_epoch = time.time()
        truncation: bool | str = "unknown" if row_count >= int(row["effective_limit"]) else False
        self._update_job(
            row["export_id"],
            status="succeeded",
            download_status="completed",
            finished_at=finished_at,
            finished_epoch=finished_epoch,
            columns_json=json.dumps(columns, ensure_ascii=False),
            source_columns_json=json.dumps(source_columns, ensure_ascii=False),
            row_count=row_count,
            file_size=file_size,
            truncated=truncation,
            error=None,
        )
        self._cleanup_remote(session, config, row["remote_dir"])
        return self._snapshot(self._get_job(row["export_id"]))

    @staticmethod
    def _csv_to_json(source: Path, target: Path) -> tuple[list[str], list[str]]:
        with source.open("r", encoding="utf-8-sig", newline="") as source_handle:
            reader = csv.reader(source_handle)
            source_columns = next(reader, [])
            columns, original_columns, _ = unique_columns(source_columns)
            with target.open("w", encoding="utf-8", newline="") as target_handle:
                target_handle.write("[\n")
                first = True
                for row in reader:
                    if len(row) != len(source_columns):
                        raise CarbonLensError("CSV export contains a row with a different column count")
                    if not first:
                        target_handle.write(",\n")
                    json.dump(dict(zip(columns, row)), target_handle, ensure_ascii=False)
                    first = False
                target_handle.write("\n]\n")
        return columns, original_columns

    def _cleanup_remote(self, session: Any, config: Any, remote_dir: str) -> None:
        command = build_query_job_cleanup_command(config, remote_dir)
        try:
            session.run(
                command,
                timeout=30,
                password=config.root_password if config.run_as_user else "",
            )
        except Exception:
            pass

    def _read_remote_log(self, session: Any, remote_dir: str) -> str | None:
        """Read a bounded log tail without rejecting a large remote log."""
        try:
            status, output, _ = session.run(
                "tail -c 4000 -- " + shlex.quote(self._log_path(remote_dir)),
                timeout=15,
            )
            return output if status == 0 else None
        except Exception:
            return None

    def _init_db(self) -> None:
        conn = self._connect_db()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS export_jobs (
                    export_id TEXT PRIMARY KEY,
                    connection TEXT NOT NULL,
                    sql TEXT NOT NULL,
                    format TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    requested_limit INTEGER NOT NULL,
                    effective_limit INTEGER NOT NULL,
                    requested_timeout_seconds INTEGER,
                    effective_timeout_seconds INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    download_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    started_at TEXT,
                    started_epoch REAL,
                    finished_at TEXT,
                    finished_epoch REAL,
                    remote_dir TEXT NOT NULL,
                    remote_pid TEXT,
                    local_path TEXT NOT NULL,
                    columns_json TEXT,
                    source_columns_json TEXT,
                    row_count INTEGER,
                    file_size INTEGER,
                    truncated TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_export_jobs_status ON export_jobs(status);
                CREATE INDEX IF NOT EXISTS idx_export_jobs_created ON export_jobs(created_epoch DESC);
                """
            )
            self._ensure_column(conn, "export_jobs", "source_columns_json", "TEXT")
            self._ensure_column(conn, "export_jobs", "truncated", "TEXT")
            conn.commit()
        finally:
            conn.close()

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_job(self, values: dict[str, Any]) -> None:
        fields = (
            "export_id", "connection", "sql", "format", "filename",
            "requested_limit", "effective_limit", "requested_timeout_seconds",
            "effective_timeout_seconds", "status", "download_status", "created_at",
            "created_epoch", "started_at", "started_epoch", "finished_at", "finished_epoch",
            "remote_dir", "remote_pid", "local_path", "columns_json", "row_count",
            "source_columns_json", "file_size", "truncated", "error",
        )
        conn = self._connect_db()
        try:
            conn.execute(
                f"INSERT INTO export_jobs ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                tuple(values[field] for field in fields),
            )
            conn.commit()
        finally:
            conn.close()

    def _update_job(self, export_id: str, **fields: Any) -> None:
        allowed = {
            "status", "download_status", "started_at", "started_epoch", "finished_at",
            "finished_epoch", "remote_pid", "columns_json", "row_count", "file_size", "error",
            "source_columns_json", "truncated",
        }
        if not fields or not set(fields).issubset(allowed):
            raise CarbonLensError("后台导出任务更新字段无效")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        conn = self._connect_db()
        try:
            cursor = conn.execute(
                f"UPDATE export_jobs SET {assignments} WHERE export_id = ?",
                (*fields.values(), export_id),
            )
            if cursor.rowcount != 1:
                raise CarbonLensError(f"Background export job not found: {export_id or '<empty>'}")
            conn.commit()
        finally:
            conn.close()

    def _get_job(self, export_id: str) -> sqlite3.Row:
        conn = self._connect_db()
        try:
            row = conn.execute("SELECT * FROM export_jobs WHERE export_id = ?", (export_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise CarbonLensError(f"Background export job not found: {export_id or '<empty>'}")
        return row

    def _mark_failed(self, export_id: str, error: str) -> None:
        self._update_job(
            export_id,
            status="failed",
            download_status="failed",
            finished_at=_timestamp(),
            finished_epoch=time.time(),
            error=error,
        )

    def _mark_cancelled(self, export_id: str) -> None:
        self._update_job(
            export_id,
            status="cancelled",
            download_status="cancelled",
            finished_at=_timestamp(),
            finished_epoch=time.time(),
            error=None,
        )

    def _prune_jobs(self) -> None:
        cutoff = time.time() - ASYNC_EXPORT_TTL_SECONDS
        conn = self._connect_db()
        try:
            conn.execute(
                "DELETE FROM export_jobs WHERE finished_epoch IS NOT NULL AND finished_epoch < ?",
                (cutoff,),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        columns = json.loads(row["columns_json"]) if row["columns_json"] else []
        original_columns = json.loads(row["source_columns_json"]) if row["source_columns_json"] else columns
        snapshot = {
            "export_id": row["export_id"],
            "connection": row["connection"],
            "sql": row["sql"],
            "format": row["format"],
            "filename": row["filename"],
            "requested_limit": row["requested_limit"],
            "effective_limit": row["effective_limit"],
            "requested_timeout_seconds": row["requested_timeout_seconds"],
            "effective_timeout_seconds": row["effective_timeout_seconds"],
            "status": row["status"],
            "download_status": row["download_status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "local_path": row["local_path"],
            "columns": columns,
            "original_columns": original_columns,
            "row_count": row["row_count"],
            "returned_row_count": row["row_count"],
            "total_row_count": None,
            "total_count_known": False,
            "truncated": row["truncated"],
            "has_more": row["truncated"],
            "type_info_unavailable": True,
            "file_size": row["file_size"],
            "elapsed_ms": ExportJobManager._elapsed_ms(row["started_epoch"], row["finished_epoch"]),
        }
        if row["error"]:
            snapshot["error"] = row["error"]
        return snapshot

    @staticmethod
    def _elapsed_ms(started_epoch: float | None, finished_epoch: float | None) -> int | None:
        if started_epoch is None:
            return None
        finished = finished_epoch or time.time()
        return max(0, int((float(finished) - float(started_epoch)) * 1000))

    @staticmethod
    def _has_timed_out(row: sqlite3.Row) -> bool:
        if row["started_epoch"] is None:
            return False
        return time.time() - float(row["started_epoch"]) > int(row["effective_timeout_seconds"]) + 60

    @staticmethod
    def _target_path(config: Any, format: str, filename: str | None) -> Path:
        suffix = ".csv" if format == "csv" else ".json"
        requested = filename or f"carbon-export-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
        safe_name = Path(requested).name
        if not safe_name.lower().endswith(suffix):
            safe_name += suffix
        export_dir = Path(config.export_dir).resolve()
        target = (export_dir / safe_name).resolve()
        if export_dir not in target.parents:
            raise CarbonLensError("导出文件路径必须位于配置的 export_dir 内")
        export_dir.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _sql_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/query.sql"

    @staticmethod
    def _output_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/result.csv"

    @staticmethod
    def _log_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/result.log"

    @staticmethod
    def _status_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/status"

    @staticmethod
    def _pid_path_from_dir(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/pid"

    @staticmethod
    def _cancel_path_from_dir(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/cancel"

    @classmethod
    def _pid_path(cls, row: sqlite3.Row) -> str:
        return cls._pid_path_from_dir(row["remote_dir"])

    @classmethod
    def _cancel_path(cls, row: sqlite3.Row) -> str:
        return cls._cancel_path_from_dir(row["remote_dir"])

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
