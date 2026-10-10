"""Managed intermediate-table write executions.

The write path is deliberately separate from read/query/export jobs while
reusing the same SSH, Beeline, detached-script, and SQLite conventions.  A
write is never retried automatically: an uncertain remote outcome is retained
as ``unknown`` and must be reconciled by an operator or a later status poll.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
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
    build_write_job_script,
)
from .config import CarbonLensError, MAX_TIMEOUT_SECONDS
from .policy import SqlPlan, WritePolicyError, parse_write_sql
from .service import CarbonLensService
from .ssh import connect


EXECUTION_RUNNING = "RUNNING"
EXECUTION_SUCCEEDED = "SUCCEEDED"
EXECUTION_FAILED = "FAILED"
EXECUTION_CANCELLED = "CANCELLED"
REMOTE_STATES = {
    EXECUTION_RUNNING,
    EXECUTION_SUCCEEDED,
    EXECUTION_FAILED,
    EXECUTION_CANCELLED,
}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
OBSERVATION_STATUSES = {"queued", "starting", "running", "cancel_requested", "unknown"}
TABLE_STATES = {"reserved", "creating", "active", "dropping", "dropped", "create_unknown", "create_failed", "drop_unknown", "drop_failed"}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(sql: str) -> str:
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()


def _not_found(error: str) -> bool:
    value = error.lower()
    return any(
        marker in value
        for marker in ("not found", "does not exist", "not exist", "不存在", "unknown table", "table not found")
    )


def _is_unsupported_dialect(error: str) -> bool:
    value = error.lower()
    return any(
        marker in value
        for marker in (
            "syntax error",
            "parse error",
            "unsupported",
            "not supported",
            "mismatched input",
            "cannot recognize input",
        )
    )


class WriteExecutionManager:
    """Persist, execute, recover, and reconcile managed-table writes."""

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

    def _config(self, connection: str) -> Any:
        config = self._service_factory().config_store.get(connection)
        if not config.write_workspace_enabled or not config.write_workspace_schema:
            raise WritePolicyError("WRITE_DISABLED", "write workspace is disabled or not configured")
        return config

    def _plan(self, config: Any, sql: str) -> SqlPlan:
        return parse_write_sql(sql, config.write_workspace_schema, config.write_table_prefix)

    def submit(self, connection: str, sql: str, idempotency_key: str | None = None, timeout_seconds: int | None = None) -> dict[str, Any]:
        idempotency_key = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else None
        config = self._config(connection)
        plan = self._plan(config, sql)
        fingerprint = _fingerprint(sql)
        requested_timeout = timeout_seconds
        effective_timeout = config.default_timeout if timeout_seconds is None else max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
        with self._lock:
            existing = self._find_idempotency(connection, idempotency_key) if idempotency_key else None
            if existing is not None:
                if existing["sql_fingerprint"] != fingerprint or existing["target_table"] != plan.target or existing["operation"] != plan.kind:
                    raise WritePolicyError("IDEMPOTENCY_CONFLICT", "idempotency_key is already bound to a different write request")
                return self._snapshot(existing)

            table = None
            if plan.kind == "create_table_as_select":
                if self._get_table(connection, plan.target, include_dropped=False) is not None:
                    raise WritePolicyError("UNOWNED_TABLE", "CTAS target is already registered")
                self._assert_remote_absent(config, plan.target)
            else:
                table = self._require_active_table(connection, plan.target, config.write_workspace_schema)
                self._assert_remote_present(config, plan.target)

            execution_id = "cx_" + uuid.uuid4().hex
            remote_dir = f"{config.remote_tmp_dir.rstrip('/')}/carbon-lens-write-{uuid.uuid4().hex}"
            now = time.time()
            self._insert_execution(
                {
                    "execution_id": execution_id,
                    "connection": connection,
                    "sql": sql,
                    "sql_fingerprint": fingerprint,
                    "idempotency_key": idempotency_key,
                    "operation": plan.kind,
                    "target_table": plan.target,
                    "table_id": table["table_id"] if table is not None else None,
                    "requested_timeout_seconds": requested_timeout,
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
                    "error": None,
                    "error_kind": None,
                }
            )
            if plan.kind == "create_table_as_select":
                self._insert_table(
                    {
                        "table_id": "mt_" + uuid.uuid4().hex,
                        "connection": connection,
                        "workspace_schema": config.write_workspace_schema,
                        "qualified_name": plan.target,
                        "create_execution_id": execution_id,
                        "create_sql_fingerprint": fingerprint,
                        "state": "reserved",
                        "created_at": _timestamp(),
                        "activated_at": None,
                        "dropped_at": None,
                        "last_verified_at": None,
                        "error": None,
                    }
                )
                table = self._get_table_by_execution(execution_id)
                self._update_execution(execution_id, table_id=table["table_id"])
            self._update_execution(execution_id, status="starting")
            try:
                remote_pid = self._start_remote_job(config, remote_dir, sql, effective_timeout)
            except Exception as exc:
                # A failed SSH/start response does not prove that the remote
                # command was not submitted. Preserve the ID and require
                # reconciliation instead of issuing a second write.
                self._mark_unknown(execution_id, f"{type(exc).__name__}: {exc}", plan.kind)
            else:
                self._update_execution(
                    execution_id,
                    status="running",
                    started_at=_timestamp(),
                    started_epoch=time.time(),
                    remote_pid=remote_pid or None,
                )
                if plan.kind == "create_table_as_select":
                    self._update_table_by_execution(execution_id, state="creating")
                elif plan.kind == "drop_table":
                    self._update_table_by_id(table["table_id"], state="dropping")
            return self._snapshot(self._get_execution(execution_id))

    def execute(self, connection: str, sql: str, idempotency_key: str | None = None, timeout_seconds: int | None = None) -> dict[str, Any]:
        submitted = self.submit(connection, sql, idempotency_key=idempotency_key, timeout_seconds=timeout_seconds)
        execution_id = submitted["execution_id"]
        deadline = time.monotonic() + (submitted["effective_timeout_seconds"] + 60)
        latest = submitted
        while time.monotonic() < deadline:
            latest = self.status(execution_id)
            if latest["status"] in TERMINAL_STATUSES or latest["status"] == "unknown":
                return latest
            time.sleep(0.2)
        self._mark_unknown(execution_id, "local execution wait exceeded timeout; reconcile the original remote job", latest.get("operation", ""))
        return self._snapshot(self._get_execution(execution_id))

    def create_from_select(
        self,
        connection: str,
        select_sql: str,
        idempotency_key: str | None = None,
        timeout_seconds: int | None = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        """Generate a fresh managed target and submit a CTAS operation."""
        config = self._config(connection)
        from .beeline import validate_read_only

        validate_read_only(select_sql)
        if select_sql.lstrip().split(None, 1)[0].upper().rstrip(";") not in {"SELECT", "WITH"}:
            raise WritePolicyError("UNSUPPORTED_SQL", "workspace create source must be SELECT or WITH ... SELECT")
        suffix = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24] if idempotency_key else uuid.uuid4().hex[:24]
        target = f"{config.write_workspace_schema}.{config.write_table_prefix}{suffix}"
        source = select_sql.strip().rstrip(";").strip()
        sql = f"CREATE TABLE {target} AS {source}"
        if wait:
            return self.execute(connection, sql, idempotency_key=idempotency_key, timeout_seconds=timeout_seconds)
        return self.submit(connection, sql, idempotency_key=idempotency_key, timeout_seconds=timeout_seconds)

    def status(self, execution_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._get_execution(execution_id)
            if row["status"] in TERMINAL_STATUSES:
                return self._snapshot(row)
            return self._refresh_remote(row)

    def list_jobs(self, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        if status is not None and status not in OBSERVATION_STATUSES | TERMINAL_STATUSES:
            raise CarbonLensError(f"Unknown execution status: {status}")
        limit = max(1, min(int(limit), 100))
        with self._lock:
            conn = self._connect_db()
            try:
                if status is None:
                    rows = conn.execute("SELECT * FROM executions ORDER BY created_epoch DESC LIMIT ?", (limit,)).fetchall()
                    count = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
                else:
                    rows = conn.execute("SELECT * FROM executions WHERE status = ? ORDER BY created_epoch DESC LIMIT ?", (status, limit)).fetchall()
                    count = conn.execute("SELECT COUNT(*) FROM executions WHERE status = ?", (status,)).fetchone()[0]
            finally:
                conn.close()
            return {"executions": [self._snapshot(row) for row in rows], "count": int(count), "returned_count": len(rows)}

    def cancel(self, execution_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._get_execution(execution_id)
            if row["status"] in TERMINAL_STATUSES:
                return self._snapshot(row)
            self._update_execution(execution_id, status="cancel_requested", error=None, error_kind=None)
            config = self._config(row["connection"])
            try:
                with connect(config) as session:
                    session.upload_text(self._cancel_path(row), "CANCEL\n")
            except Exception as exc:
                self._update_execution(execution_id, error=f"{type(exc).__name__}: {exc}", error_kind="CANCEL_REQUEST_FAILED")
            return self._snapshot(self._get_execution(execution_id))

    def workspace_list(self, connection: str, state: str | None = None, limit: int = 100) -> dict[str, Any]:
        config = self._config(connection)
        if state is not None and state not in TABLE_STATES:
            raise CarbonLensError(f"Unknown managed table state: {state}")
        limit = max(1, min(int(limit), 200))
        conn = self._connect_db()
        try:
            if state is None:
                rows = conn.execute("SELECT * FROM managed_tables WHERE connection = ? ORDER BY created_at DESC LIMIT ?", (connection, limit)).fetchall()
                count = conn.execute("SELECT COUNT(*) FROM managed_tables WHERE connection = ?", (connection,)).fetchone()[0]
            else:
                rows = conn.execute("SELECT * FROM managed_tables WHERE connection = ? AND state = ? ORDER BY created_at DESC LIMIT ?", (connection, state, limit)).fetchall()
                count = conn.execute("SELECT COUNT(*) FROM managed_tables WHERE connection = ? AND state = ?", (connection, state)).fetchone()[0]
        finally:
            conn.close()
        return {
            "connection": connection,
            "workspace_schema": config.write_workspace_schema,
            "tables": [self._table_snapshot(row) for row in rows],
            "count": int(count),
            "returned_count": len(rows),
        }

    def _start_remote_job(self, config: Any, remote_dir: str, sql: str, timeout_seconds: int) -> str:
        remote_sql = remote_dir + "/statement.sql"
        remote_log = remote_dir + "/result.log"
        remote_status = remote_dir + "/status"
        remote_pid = remote_dir + "/pid"
        remote_cancel = remote_dir + "/cancel"
        with connect(config) as session:
            session.make_dir(remote_dir)
            session.upload_text(remote_sql, sql.strip() + "\n")
            script = build_write_job_script(
                config, remote_sql, remote_log, remote_status, remote_pid,
                remote_cancel, remote_dir=remote_dir, timeout_seconds=timeout_seconds,
            )
            return session.start_detached(
                build_query_job_launch_command(config, script),
                password=config.root_password if config.run_as_user else "",
            )

    def _refresh_remote(self, row: sqlite3.Row) -> dict[str, Any]:
        config = self._config(row["connection"])
        with connect(config) as session:
            state_text = session.read_text(self._status_path(row["remote_dir"]), max_bytes=64)
            state = (state_text or "").strip().upper()
            remote_pid = session.read_text(self._pid_path(row), max_bytes=128)
            if remote_pid and remote_pid.strip() != (row["remote_pid"] or ""):
                self._update_execution(row["execution_id"], remote_pid=remote_pid.strip())
                row = self._get_execution(row["execution_id"])
            if state == EXECUTION_SUCCEEDED:
                return self._complete_remote_success(session, config, row)
            if state == EXECUTION_FAILED:
                error = self._read_remote_log(session, row["remote_dir"]) or "remote write failed"
                self._mark_failed(row["execution_id"], error.strip()[-4000:], self._operation(row))
                self._cleanup_remote(session, config, row["remote_dir"])
                return self._snapshot(self._get_execution(row["execution_id"]))
            if state == EXECUTION_CANCELLED:
                self._mark_cancelled(row["execution_id"], self._operation(row))
                self._cleanup_remote(session, config, row["remote_dir"])
                return self._snapshot(self._get_execution(row["execution_id"]))
            if self._has_timed_out(row):
                self._mark_unknown(row["execution_id"], "remote write status is not terminal after timeout; reconciliation_required", self._operation(row))
                return self._snapshot(self._get_execution(row["execution_id"]))
            if state == EXECUTION_RUNNING and row["status"] in {"queued", "starting"}:
                self._update_execution(row["execution_id"], status="running", started_at=row["created_at"], started_epoch=row["created_epoch"])
                row = self._get_execution(row["execution_id"])
            return self._snapshot(row)

    def _complete_remote_success(self, session: Any, config: Any, row: sqlite3.Row) -> dict[str, Any]:
        operation = self._operation(row)
        try:
            if operation == "create_table_as_select":
                if not self._table_exists(config, row["target_table"]):
                    raise WritePolicyError("EXECUTION_UNKNOWN", "remote CREATE succeeded but target identity could not be verified")
                self._update_table_by_execution(row["execution_id"], state="active", activated_at=_timestamp(), last_verified_at=_timestamp(), error=None)
            elif operation == "drop_table":
                if self._table_exists(config, row["target_table"]):
                    raise WritePolicyError("EXECUTION_UNKNOWN", "remote DROP succeeded but target still exists")
                self._update_table_by_id(row["table_id"], state="dropped", dropped_at=_timestamp(), last_verified_at=_timestamp(), error=None)
            else:
                self._assert_remote_present(config, row["target_table"])
        except WritePolicyError as exc:
            self._mark_unknown(row["execution_id"], str(exc), operation)
            return self._snapshot(self._get_execution(row["execution_id"]))
        except Exception as exc:
            self._mark_unknown(
                row["execution_id"],
                f"{type(exc).__name__}: {exc}; reconciliation_required",
                operation,
            )
            return self._snapshot(self._get_execution(row["execution_id"]))
        self._update_execution(row["execution_id"], status="succeeded", finished_at=_timestamp(), finished_epoch=time.time(), error=None, error_kind=None)
        self._cleanup_remote(session, config, row["remote_dir"])
        return self._snapshot(self._get_execution(row["execution_id"]))

    def _assert_remote_absent(self, config: Any, target: str) -> None:
        try:
            if self._table_exists(config, target):
                raise WritePolicyError("UNOWNED_TABLE", "target table already exists")
        except WritePolicyError:
            raise
        except Exception as exc:
            raise WritePolicyError("WORKSPACE_NOT_READY", f"cannot verify target absence: {exc}") from exc

    def _assert_remote_present(self, config: Any, target: str) -> None:
        try:
            if not self._table_exists(config, target):
                raise WritePolicyError("UNOWNED_TABLE", "managed target does not exist remotely")
        except WritePolicyError:
            raise
        except Exception as exc:
            raise WritePolicyError("WORKSPACE_NOT_READY", f"cannot verify target identity: {exc}") from exc

    def _table_exists(self, config: Any, target: str) -> bool:
        service = self._service_factory()
        try:
            service.describe(config.name, target)
            return True
        except CarbonLensError as exc:
            if _not_found(str(exc)):
                return False
            raise

    def _read_remote_log(self, session: Any, remote_dir: str) -> str | None:
        try:
            status, output, _ = session.run("tail -c 4000 -- " + self._log_path(remote_dir), timeout=15)
            return output if status == 0 else None
        except Exception:
            return None

    def _cleanup_remote(self, session: Any, config: Any, remote_dir: str) -> None:
        try:
            session.run(build_query_job_cleanup_command(config, remote_dir), timeout=30, password=config.root_password if config.run_as_user else "")
        except Exception:
            pass

    def _operation(self, row: sqlite3.Row) -> str:
        return str(row["operation"])

    def _require_active_table(self, connection: str, target: str, workspace_schema: str) -> sqlite3.Row:
        row = self._get_table(connection, target, include_dropped=False)
        if row is None or row["workspace_schema"] != workspace_schema or row["state"] != "active":
            raise WritePolicyError("UNOWNED_TABLE", "target is not an active CarbonLens-managed table")
        return row

    def _find_idempotency(self, connection: str, key: str | None) -> sqlite3.Row | None:
        conn = self._connect_db()
        try:
            return conn.execute("SELECT * FROM executions WHERE connection = ? AND idempotency_key = ?", (connection, key)).fetchone()
        finally:
            conn.close()

    def _get_execution(self, execution_id: str) -> sqlite3.Row:
        conn = self._connect_db()
        try:
            row = conn.execute("SELECT * FROM executions WHERE execution_id = ?", (execution_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise CarbonLensError(f"Execution not found: {execution_id or '<empty>'}")
        return row

    def _get_table(self, connection: str, target: str, include_dropped: bool) -> sqlite3.Row | None:
        conn = self._connect_db()
        try:
            sql = "SELECT * FROM managed_tables WHERE connection = ? AND qualified_name = ?"
            params: tuple[Any, ...] = (connection, target)
            if not include_dropped:
                sql += " AND state != 'dropped'"
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _get_table_by_execution(self, execution_id: str) -> sqlite3.Row:
        conn = self._connect_db()
        try:
            row = conn.execute("SELECT * FROM managed_tables WHERE create_execution_id = ?", (execution_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise CarbonLensError(f"Managed table reservation missing for execution: {execution_id}")
        return row

    def _insert_execution(self, values: dict[str, Any]) -> None:
        fields = [
            "execution_id", "connection", "sql", "sql_fingerprint", "idempotency_key", "operation", "target_table", "table_id",
            "requested_timeout_seconds", "effective_timeout_seconds", "status", "created_at", "created_epoch", "started_at", "started_epoch",
            "finished_at", "finished_epoch", "remote_dir", "remote_pid", "error", "error_kind",
        ]
        conn = self._connect_db()
        try:
            conn.execute(f"INSERT INTO executions ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})", tuple(values[field] for field in fields))
            conn.commit()
        finally:
            conn.close()

    def _insert_table(self, values: dict[str, Any]) -> None:
        fields = ["table_id", "connection", "workspace_schema", "qualified_name", "create_execution_id", "create_sql_fingerprint", "state", "created_at", "activated_at", "dropped_at", "last_verified_at", "error"]
        conn = self._connect_db()
        try:
            conn.execute(f"INSERT INTO managed_tables ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})", tuple(values[field] for field in fields))
            conn.commit()
        finally:
            conn.close()

    def _update_execution(self, execution_id: str, **fields: Any) -> None:
        allowed = {"status", "started_at", "started_epoch", "finished_at", "finished_epoch", "remote_pid", "error", "error_kind", "table_id"}
        if not fields or not set(fields).issubset(allowed):
            raise CarbonLensError("invalid execution update fields")
        conn = self._connect_db()
        try:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            cursor = conn.execute(f"UPDATE executions SET {assignments} WHERE execution_id = ?", (*fields.values(), execution_id))
            if cursor.rowcount != 1:
                raise CarbonLensError(f"Execution not found: {execution_id}")
            conn.commit()
        finally:
            conn.close()

    def _update_table_by_execution(self, execution_id: str, **fields: Any) -> None:
        allowed = {"state", "activated_at", "dropped_at", "last_verified_at", "error"}
        if not fields or not set(fields).issubset(allowed):
            raise CarbonLensError("invalid managed table update fields")
        conn = self._connect_db()
        try:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            cursor = conn.execute(f"UPDATE managed_tables SET {assignments} WHERE create_execution_id = ?", (*fields.values(), execution_id))
            if cursor.rowcount != 1:
                raise CarbonLensError(f"Managed table reservation missing for execution: {execution_id}")
            conn.commit()
        finally:
            conn.close()

    def _update_table_by_id(self, table_id: str, **fields: Any) -> None:
        allowed = {"state", "activated_at", "dropped_at", "last_verified_at", "error"}
        if not fields or not set(fields).issubset(allowed):
            raise CarbonLensError("invalid managed table update fields")
        conn = self._connect_db()
        try:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            cursor = conn.execute(f"UPDATE managed_tables SET {assignments} WHERE table_id = ?", (*fields.values(), table_id))
            if cursor.rowcount != 1:
                raise CarbonLensError(f"Managed table not found: {table_id}")
            conn.commit()
        finally:
            conn.close()

    def _mark_failed(self, execution_id: str, error: str, operation: str) -> None:
        error_kind = "UNSUPPORTED_DIALECT" if _is_unsupported_dialect(error) else "REMOTE_FAILED"
        self._update_execution(execution_id, status="failed", finished_at=_timestamp(), finished_epoch=time.time(), error=error, error_kind=error_kind)
        if operation == "create_table_as_select":
            self._update_table_by_execution(execution_id, state="create_failed", error=error)
        elif operation == "drop_table":
            row = self._get_execution(execution_id)
            self._update_table_by_id(row["table_id"], state="drop_failed", error=error)

    def _mark_unknown(self, execution_id: str, error: str, operation: str) -> None:
        if "reconciliation_required" not in error:
            error = error.rstrip(".") + "; reconciliation_required"
        self._update_execution(execution_id, status="unknown", finished_at=_timestamp(), finished_epoch=time.time(), error=error, error_kind="EXECUTION_UNKNOWN")
        if operation == "create_table_as_select":
            self._update_table_by_execution(execution_id, state="create_unknown", error=error)
        elif operation == "drop_table":
            row = self._get_execution(execution_id)
            self._update_table_by_id(row["table_id"], state="drop_unknown", error=error)

    def _mark_cancelled(self, execution_id: str, operation: str) -> None:
        self._update_execution(execution_id, status="cancelled", finished_at=_timestamp(), finished_epoch=time.time(), error=None, error_kind=None)
        if operation == "create_table_as_select":
            self._update_table_by_execution(execution_id, state="create_failed", error="remote create cancelled")
        elif operation == "drop_table":
            row = self._get_execution(execution_id)
            self._update_table_by_id(row["table_id"], state="drop_failed", error="remote drop cancelled")

    def _has_timed_out(self, row: sqlite3.Row) -> bool:
        started = row["started_epoch"] or row["created_epoch"]
        return time.time() - float(started) > int(row["effective_timeout_seconds"]) + 60

    def _init_db(self) -> None:
        conn = self._connect_db()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    connection TEXT NOT NULL,
                    sql TEXT NOT NULL,
                    sql_fingerprint TEXT NOT NULL,
                    idempotency_key TEXT,
                    operation TEXT NOT NULL,
                    target_table TEXT NOT NULL,
                    table_id TEXT,
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
                    error TEXT,
                    error_kind TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_idempotency ON executions(connection, idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_executions_status ON executions(status);
                CREATE INDEX IF NOT EXISTS idx_executions_created ON executions(created_epoch DESC);
                CREATE TABLE IF NOT EXISTS managed_tables (
                    table_id TEXT PRIMARY KEY,
                    connection TEXT NOT NULL,
                    workspace_schema TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    create_execution_id TEXT NOT NULL UNIQUE,
                    create_sql_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    dropped_at TEXT,
                    last_verified_at TEXT,
                    error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_managed_tables_name ON managed_tables(connection, qualified_name);
                CREATE INDEX IF NOT EXISTS idx_managed_tables_state ON managed_tables(connection, state);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _status_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/status"

    @staticmethod
    def _log_path(remote_dir: str) -> str:
        return remote_dir.rstrip("/") + "/result.log"

    @staticmethod
    def _pid_path(row: sqlite3.Row) -> str:
        return row["remote_dir"].rstrip("/") + "/pid"

    @staticmethod
    def _cancel_path(row: sqlite3.Row) -> str:
        return row["remote_dir"].rstrip("/") + "/cancel"

    @staticmethod
    def _table_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in ("table_id", "connection", "workspace_schema", "qualified_name", "create_execution_id", "state", "created_at", "activated_at", "dropped_at", "last_verified_at", "error") if row[key] is not None}

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "execution_id": row["execution_id"],
            "connection": row["connection"],
            "operation": row["operation"],
            "target_table": row["target_table"],
            "table_id": row["table_id"],
            "idempotency_key": row["idempotency_key"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "requested_timeout_seconds": row["requested_timeout_seconds"],
            "effective_timeout_seconds": row["effective_timeout_seconds"],
            "affected_row_count": None,
            "affected_row_count_known": False,
            "elapsed_ms": WriteExecutionManager._elapsed_ms(row["started_epoch"], row["finished_epoch"]),
            "error_kind": row["error_kind"],
            "sql_fingerprint": row["sql_fingerprint"],
        }
        if row["error"]:
            result["error"] = row["error"]
        return result

    @staticmethod
    def _elapsed_ms(started_epoch: float | None, finished_epoch: float | None) -> int | None:
        if started_epoch is None:
            return None
        finished = finished_epoch or time.time()
        return max(0, int((float(finished) - float(started_epoch)) * 1000))
