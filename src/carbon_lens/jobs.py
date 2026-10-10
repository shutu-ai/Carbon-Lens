"""Persistent remote jobs for long-running Carbon queries."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
import time
import uuid
from typing import Any, Callable

from .beeline import (
    QUERY_CANCELLED,
    QUERY_FAILED,
    QUERY_RUNNING,
    QUERY_SUCCEEDED,
    build_query_job_cleanup_command,
    build_query_job_launch_command,
    build_query_job_script,
    parse_dsv,
    rows_as_dicts,
    unique_columns,
    validate_read_only,
)
from .config import MAX_TIMEOUT_SECONDS, CarbonLensError
from .service import CarbonLensService
from .ssh import connect


ASYNC_QUERY_TTL_SECONDS = 30 * 24 * 60 * 60
REMOTE_QUERY_TTL_SECONDS = 2 * 24 * 60 * 60
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
KNOWN_STATUSES = {"queued", "starting", "running", "cancel_requested", *TERMINAL_STATUSES}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueryJobManager:
    """Persist job metadata locally while executing SQL in a detached SSH job."""

    def __init__(self, service_factory: Callable[[], CarbonLensService]):
        self._service_factory = service_factory
        service = service_factory()
        configured = os.environ.get("CARBON_LENS_JOB_DB", "").strip()
        database = Path(configured).expanduser() if configured else service.config_store.path.parent / ".carbon-lens-jobs.sqlite3"
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
        limit: int = 100,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        validate_read_only(sql)
        config = self._service_factory().config_store.get(connection)
        requested_limit = int(limit)
        effective_limit = max(1, min(requested_limit, config.max_rows))
        effective_timeout = (
            config.default_timeout
            if timeout_seconds is None
            else max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
        )
        query_id = "cq_" + uuid.uuid4().hex
        remote_dir = f"{config.remote_tmp_dir.rstrip('/')}/carbon-lens-query-{uuid.uuid4().hex}"
        result_path = self._result_path(query_id)
        now = time.time()
        with self._lock:
            self._insert_job(
                {
                    "query_id": query_id,
                    "connection": connection,
                    "sql": sql,
                    "requested_limit": requested_limit,
                    "effective_limit": effective_limit,
                    "requested_timeout_seconds": timeout_seconds,
                    "effective_timeout_seconds": effective_timeout,
                    "status": "queued",
                    "created_at": _timestamp(),
                    "created_epoch": now,
                    "started_at": None,
                    "started_epoch": None,
                    "finished_at": None,
                    "finished_epoch": None,
                    "remote_dir": remote_dir,
                    "remote_pid": None,
                    "result_path": str(result_path),
                    "result_meta_json": None,
                    "result_json": None,
                    "error": None,
                }
            )

            self._update_job(query_id, status="starting")
            try:
                remote_pid = self._start_remote_job(
                    config,
                    remote_dir,
                    sql,
                    effective_timeout,
                )
            except Exception as exc:
                try:
                    with connect(config) as session:
                        self._cleanup_remote(session, config, remote_dir)
                except Exception:
                    pass
                self._update_job(
                    query_id,
                    status="failed",
                    finished_at=_timestamp(),
                    finished_epoch=time.time(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                started_at = _timestamp()
                self._update_job(
                    query_id,
                    status="running",
                    started_at=started_at,
                    started_epoch=time.time(),
                    remote_pid=remote_pid or None,
                )
            return self._snapshot(self._get_job(query_id), include_result=False)

    def status(self, query_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._get_job(query_id)
            if row["status"] in TERMINAL_STATUSES:
                return self._snapshot(row, include_result=True)
            return self._refresh_remote(row)

    def list_jobs(self, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        if status is not None and status not in KNOWN_STATUSES:
            raise CarbonLensError(f"Unknown background query status: {status}")
        limit = max(1, min(int(limit), 100))
        with self._lock:
            self._prune_jobs()
            conn = self._connect_db()
            try:
                if status is None:
                    rows = conn.execute(
                        "SELECT * FROM query_jobs ORDER BY created_epoch DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    count = conn.execute("SELECT COUNT(*) FROM query_jobs").fetchone()[0]
                else:
                    rows = conn.execute(
                        "SELECT * FROM query_jobs WHERE status = ? ORDER BY created_epoch DESC LIMIT ?",
                        (status, limit),
                    ).fetchall()
                    count = conn.execute(
                        "SELECT COUNT(*) FROM query_jobs WHERE status = ?",
                        (status,),
                    ).fetchone()[0]
            finally:
                conn.close()
            return {
                "jobs": [self._snapshot(row, include_result=False) for row in rows],
                "count": int(count),
                "returned_count": len(rows),
            }

    def cancel(self, query_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._get_job(query_id)
            if row["status"] in TERMINAL_STATUSES:
                return self._snapshot(row, include_result=False)
            if row["status"] == "queued":
                refreshed = self._refresh_remote(row)
                if refreshed["status"] in TERMINAL_STATUSES:
                    return refreshed
                row = self._get_job(query_id)
                if row["status"] == "queued":
                    self._mark_cancelled(query_id)
                    return self._snapshot(self._get_job(query_id), include_result=False)

            self._update_job(query_id, status="cancel_requested", error=None)
            row = self._get_job(query_id)
            config = self._service_factory().config_store.get(row["connection"])
            try:
                with connect(config) as session:
                    session.upload_text(self._cancel_path(row), "CANCEL\n")
            except Exception as exc:
                self._update_job(query_id, error=f"{type(exc).__name__}: {exc}")
            return self._snapshot(self._get_job(query_id), include_result=False)

    def _start_remote_job(
        self,
        config: Any,
        remote_dir: str,
        sql: str,
        timeout_seconds: int,
    ) -> str:
        remote_sql = self._sql_path(remote_dir)
        remote_output = self._output_path(remote_dir)
        remote_status = self._status_path(remote_dir)
        remote_error = self._error_path(remote_dir)
        remote_pid = self._pid_path_from_dir(remote_dir)
        remote_cancel = self._cancel_path_from_dir(remote_dir)
        with connect(config) as session:
            session.make_dir(remote_dir)
            session.upload_text(remote_sql, sql)
            script = build_query_job_script(
                config,
                remote_sql,
                remote_output,
                remote_status,
                remote_error,
                remote_pid,
                remote_dir,
                remote_cancel_path=remote_cancel,
                ttl_seconds=REMOTE_QUERY_TTL_SECONDS,
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
                self._update_job(row["query_id"], remote_pid=remote_pid.strip())
                row = self._get_job(row["query_id"])

            # Do not let a stale RUNNING or CANCEL_REQUESTED state bypass the
            # local deadline forever when the remote wrapper disappeared.
            if self._has_timed_out(row):
                self._mark_failed(row["query_id"], "Remote job status did not complete before timeout_seconds")
                self._cleanup_remote(session, config, row["remote_dir"])
                return self._snapshot(self._get_job(row["query_id"]), include_result=False)

            if row["status"] == "cancel_requested":
                if state == QUERY_SUCCEEDED:
                    return self._complete_success(session, config, row)
                if state == QUERY_FAILED:
                    error = session.read_text(self._error_path(row["remote_dir"]), max_bytes=4000) or "Remote query failed"
                    self._mark_failed(row["query_id"], error.strip()[-4000:])
                    self._cleanup_remote(session, config, row["remote_dir"])
                    return self._snapshot(self._get_job(row["query_id"]), include_result=False)
                if state == QUERY_CANCELLED:
                    self._mark_cancelled(row["query_id"])
                    self._cleanup_remote(session, config, row["remote_dir"])
                    return self._snapshot(self._get_job(row["query_id"]), include_result=False)
                return self._snapshot(row, include_result=False)

            if state == QUERY_RUNNING:
                if row["status"] in {"queued", "starting"}:
                    self._update_job(
                        row["query_id"],
                        status="running",
                        started_at=row["created_at"],
                        started_epoch=row["created_epoch"],
                    )
                    row = self._get_job(row["query_id"])
                return self._snapshot(row, include_result=False)

            if state == QUERY_SUCCEEDED:
                return self._complete_success(session, config, row)

            if state == QUERY_FAILED:
                error = session.read_text(self._error_path(row["remote_dir"]), max_bytes=4000) or "Remote query failed"
                self._mark_failed(row["query_id"], error.strip()[-4000:])
                self._cleanup_remote(session, config, row["remote_dir"])
                return self._snapshot(self._get_job(row["query_id"]), include_result=False)

            if state == QUERY_CANCELLED:
                self._mark_cancelled(row["query_id"])
                return self._snapshot(self._get_job(row["query_id"]), include_result=False)

            return self._snapshot(row, include_result=False)

    def _complete_success(self, session: Any, config: Any, row: sqlite3.Row) -> dict[str, Any]:
        output = session.read_text(
            self._output_path(row["remote_dir"]),
            max_bytes=config.max_output_bytes,
        )
        if output is None:
            return self._snapshot(row, include_result=False)
        try:
            columns, rows, truncated = parse_dsv(output, int(row["effective_limit"]))
        except Exception as exc:
            self._mark_failed(row["query_id"], f"{type(exc).__name__}: {exc}")
            self._cleanup_remote(session, config, row["remote_dir"])
            return self._snapshot(self._get_job(row["query_id"]), include_result=False)

        finished_at = _timestamp()
        finished_epoch = time.time()
        elapsed_ms = self._elapsed_ms(row["started_epoch"], finished_epoch)
        result_columns, original_columns, column_name_map = unique_columns(columns)
        result = {
            "connection": row["connection"],
            "transport": "ssh-beeline-persistent",
            "columns": result_columns,
            "original_columns": original_columns,
            "column_name_map": column_name_map,
            "rows": rows_as_dicts(columns, rows, result_columns),
            "row_count": len(rows),
            "returned_row_count": len(rows),
            "total_row_count": None,
            "total_count_known": False,
            "has_more": truncated,
            "truncated": truncated,
            "type_info_unavailable": True,
            "elapsed_ms": elapsed_ms,
        }
        supplied_total = self._provided_total_count(result)
        if supplied_total is not None:
            result["total_row_count"] = supplied_total
            result["total_count_known"] = True
        result_path = Path(row["result_path"] or self._result_path(row["query_id"]))
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_part = result_path.with_name(f".{result_path.name}.{uuid.uuid4().hex}.part")
        try:
            result_part.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            result_part.replace(result_path)
        except Exception as exc:
            result_part.unlink(missing_ok=True)
            self._mark_failed(row["query_id"], f"result persistence failed: {type(exc).__name__}: {exc}")
            self._cleanup_remote(session, config, row["remote_dir"])
            return self._snapshot(self._get_job(row["query_id"]), include_result=False)
        result_meta = {
            key: result[key]
            for key in (
                "columns", "original_columns", "column_name_map", "row_count",
                "returned_row_count", "total_row_count", "total_count_known",
                "has_more", "truncated", "type_info_unavailable", "elapsed_ms",
            )
        }
        self._update_job(
            row["query_id"],
            status="succeeded",
            finished_at=finished_at,
            finished_epoch=finished_epoch,
            result_meta_json=json.dumps(result_meta, ensure_ascii=False),
            error=None,
        )
        self._update_job(row["query_id"], result_json=None)
        self._cleanup_remote(session, config, row["remote_dir"])
        return self._snapshot(self._get_job(row["query_id"]), include_result=True)

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

    def _init_db(self) -> None:
        conn = self._connect_db()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS query_jobs (
                    query_id TEXT PRIMARY KEY,
                    connection TEXT NOT NULL,
                    sql TEXT NOT NULL,
                    requested_limit INTEGER NOT NULL,
                    effective_limit INTEGER NOT NULL,
                    requested_timeout_seconds INTEGER,
                    effective_timeout_seconds INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    started_at TEXT,
                    started_epoch REAL,
                    finished_at TEXT,
                    finished_epoch REAL,
                    remote_dir TEXT NOT NULL,
                    remote_pid TEXT,
                    result_path TEXT,
                    result_meta_json TEXT,
                    result_json TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_query_jobs_status ON query_jobs(status);
                CREATE INDEX IF NOT EXISTS idx_query_jobs_created ON query_jobs(created_epoch DESC);
                """
            )
            self._ensure_column(conn, "query_jobs", "result_path", "TEXT")
            self._ensure_column(conn, "query_jobs", "result_meta_json", "TEXT")
            conn.commit()
        finally:
            conn.close()

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_job(self, values: dict[str, Any]) -> None:
        conn = self._connect_db()
        try:
            conn.execute(
                """
                INSERT INTO query_jobs (
                    query_id, connection, sql, requested_limit, effective_limit,
                    requested_timeout_seconds, effective_timeout_seconds, status,
                    created_at, created_epoch, started_at, started_epoch,
                    finished_at, finished_epoch, remote_dir, remote_pid,
                    result_path, result_meta_json, result_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(values[key] for key in (
                    "query_id", "connection", "sql", "requested_limit", "effective_limit",
                    "requested_timeout_seconds", "effective_timeout_seconds", "status",
                    "created_at", "created_epoch", "started_at", "started_epoch",
                    "finished_at", "finished_epoch", "remote_dir", "remote_pid",
                    "result_path", "result_meta_json", "result_json", "error",
                )),
            )
            conn.commit()
        finally:
            conn.close()

    def _update_job(self, query_id: str, **fields: Any) -> None:
        allowed = {
            "status", "started_at", "started_epoch", "finished_at", "finished_epoch",
            "remote_pid", "result_path", "result_meta_json", "result_json", "error",
        }
        if not fields or not set(fields).issubset(allowed):
            raise CarbonLensError("后台任务更新字段无效")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        conn = self._connect_db()
        try:
            cursor = conn.execute(
                f"UPDATE query_jobs SET {assignments} WHERE query_id = ?",
                (*fields.values(), query_id),
            )
            if cursor.rowcount != 1:
                raise CarbonLensError(f"Background query job not found: {query_id or '<empty>'}")
            conn.commit()
        finally:
            conn.close()

    def _get_job(self, query_id: str) -> sqlite3.Row:
        conn = self._connect_db()
        try:
            row = conn.execute("SELECT * FROM query_jobs WHERE query_id = ?", (query_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
                raise CarbonLensError(f"Background query job not found: {query_id or '<empty>'}")
        return row

    def _mark_failed(self, query_id: str, error: str) -> None:
        self._update_job(
            query_id,
            status="failed",
            finished_at=_timestamp(),
            finished_epoch=time.time(),
            error=error,
        )

    def _mark_cancelled(self, query_id: str) -> None:
        self._update_job(
            query_id,
            status="cancelled",
            finished_at=_timestamp(),
            finished_epoch=time.time(),
            error=None,
        )

    def _prune_jobs(self) -> None:
        cutoff = time.time() - ASYNC_QUERY_TTL_SECONDS
        conn = self._connect_db()
        try:
            old_results = conn.execute(
                "SELECT result_path FROM query_jobs WHERE finished_epoch IS NOT NULL AND finished_epoch < ?",
                (cutoff,),
            ).fetchall()
            conn.execute(
                "DELETE FROM query_jobs WHERE finished_epoch IS NOT NULL AND finished_epoch < ?",
                (cutoff,),
            )
            conn.commit()
        finally:
            conn.close()
        for row in old_results:
            if row["result_path"]:
                try:
                    Path(row["result_path"]).unlink(missing_ok=True)
                except OSError:
                    pass

    @classmethod
    def _snapshot(cls, row: sqlite3.Row, include_result: bool) -> dict[str, Any]:
        result = None
        if include_result:
            result_path = row["result_path"]
            if result_path and Path(result_path).is_file():
                try:
                    result = json.loads(Path(result_path).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise CarbonLensError(f"Background query result is unreadable: {exc}") from exc
            elif row["result_json"]:
                result = json.loads(row["result_json"])
        try:
            metadata = json.loads(row["result_meta_json"]) if row["result_meta_json"] else {}
        except json.JSONDecodeError:
            metadata = {}
        snapshot = {
            "query_id": row["query_id"],
            "connection": row["connection"],
            "sql": row["sql"],
            "requested_limit": row["requested_limit"],
            "effective_limit": row["effective_limit"],
            "requested_timeout_seconds": row["requested_timeout_seconds"],
            "effective_timeout_seconds": row["effective_timeout_seconds"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "remote_pid": row["remote_pid"],
            "elapsed_ms": cls._elapsed_ms(row["started_epoch"], row["finished_epoch"]),
            "returned_row_count": None,
            "total_row_count": None,
            "total_count_known": False,
            "preview_rows": [],
            "truncated": metadata.get("truncated"),
            "has_more": metadata.get("has_more"),
            "type_info_unavailable": metadata.get("type_info_unavailable", True),
            "result_available": bool(row["result_path"] and Path(row["result_path"]).is_file()) or bool(row["result_json"]),
        }
        if row["error"]:
            snapshot["error"] = row["error"]
        if row["status"] == "succeeded":
            rows = (result or {}).get("rows") or []
            snapshot.update(
                {
                    "elapsed_ms": (result or metadata).get("elapsed_ms", snapshot["elapsed_ms"]),
                    "returned_row_count": (result or metadata).get("returned_row_count", (result or metadata).get("row_count", len(rows))),
                    "preview_rows": rows[:3] if result is not None else [],
                    "truncated": (result or metadata).get("truncated"),
                    "has_more": (result or metadata).get("has_more"),
                }
            )
            if metadata:
                snapshot["columns"] = metadata.get("columns", [])
                snapshot["original_columns"] = metadata.get("original_columns", snapshot["columns"])
                snapshot["column_name_map"] = metadata.get("column_name_map", {})
            supplied_total = cls._provided_total_count(result or {})
            if supplied_total is not None:
                snapshot["total_row_count"] = supplied_total
                snapshot["total_count_known"] = True
            if include_result:
                if result is not None:
                    snapshot["result"] = result
        return snapshot

    @staticmethod
    def _provided_total_count(result: dict[str, Any]) -> int | None:
        columns = result.get("columns") or []
        rows = result.get("rows") or []
        if not rows:
            return None
        for column in ("total_row_count", "__carbon_lens_total_row_count"):
            if column in columns:
                try:
                    return int(rows[0].get(column))
                except (TypeError, ValueError):
                    return None
        return None

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
    def _sql_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/query.sql"

    @staticmethod
    def _output_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/result.dsv"

    def _result_path(self, query_id: str) -> Path:
        service = self._service_factory()
        result_dir = service.config_store.path.parent / ".carbon-lens-query-results"
        return result_dir / f"{query_id}.json"

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _status_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/status"

    @staticmethod
    def _error_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/error.log"

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
