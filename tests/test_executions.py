from __future__ import annotations

import shutil
from pathlib import Path
import re
import time
import uuid

import pytest

from carbon_lens.config import CarbonLensError, ConnectionConfig
from carbon_lens.executions import WriteExecutionManager
from carbon_lens.policy import WritePolicyError


def runtime_dir() -> Path:
    path = Path(__file__).resolve().parents[1] / (".test-execution-" + uuid.uuid4().hex)
    path.mkdir(parents=True, exist_ok=False)
    return path


def config(tmp_path: Path, enabled: bool = True) -> ConnectionConfig:
    return ConnectionConfig(
        name="test",
        ssh={"hostname": "example", "username": "u"},
        jump={},
        env_script="/opt/client/bigdata_env",
        beeline_path="/opt/client/Fiber/bin/fiber-beeline",
        beeline_connection="Carbon_Query_XDR",
        principal="ossuser",
        keytab="/opt/client/keytab/ossuser.keytab",
        run_as_user="root",
        root_password="",
        remote_tmp_dir="/tmp",
        export_dir=tmp_path / "exports",
        default_timeout=60,
        max_rows=1000,
        max_output_bytes=1024,
        known_hosts="known_hosts",
        host_key_sha256="",
        write_workspace_enabled=enabled,
        write_workspace_schema="carbon_lens_workspace",
        write_table_prefix="cl_tmp_",
    )


class FakeStore:
    def __init__(self, tmp_path: Path, enabled: bool = True):
        self.path = tmp_path / "config.json"
        self._config = config(tmp_path, enabled)

    def get(self, name: str) -> ConnectionConfig:
        assert name == "test"
        return self._config


class FakeService:
    def __init__(self, tmp_path: Path, enabled: bool = True):
        self.config_store = FakeStore(tmp_path, enabled)
        self.exists: set[str] = set()

    def describe(self, connection: str, table: str):
        assert connection == "test"
        if table not in self.exists:
            raise CarbonLensError("table does not exist")
        return {"columns": ["col_name"], "rows": []}


class FakeSession:
    def __init__(self, service: FakeService, state: str = "SUCCEEDED", log: str = "remote failure\n"):
        self.service = service
        self.state = state
        self.log = log
        self.sql = ""
        self.start_count = 0
        self.cancel_uploads = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def make_dir(self, remote_dir: str):
        pass

    def upload_text(self, remote_path: str, content: str):
        if remote_path.endswith("statement.sql"):
            self.sql = content
            match = re.search(r"CREATE\s+TABLE\s+([A-Za-z_][\w$]*\.[A-Za-z_][\w$]*)", content, re.I)
            if match:
                self.service.exists.add(match.group(1))
        elif remote_path.endswith("/cancel"):
            self.cancel_uploads += 1

    def start_detached(self, command: str, password: str = "") -> str:
        self.start_count += 1
        return "12345"

    def read_text(self, path: str, max_bytes: int | None = None):
        if path.endswith("/status"):
            return self.state + "\n"
        if path.endswith("/pid"):
            return "12345\n"
        if path.endswith("/result.log"):
            return self.log
        return None

    def run(self, command: str, timeout: int = 30, password: str = "", username: str = ""):
        if command.startswith("tail -c"):
            return 0, self.log, ""
        return 0, "", ""


def test_write_disabled_and_policy_rejection_happen_before_remote_connect(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path, enabled=False)
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        manager = WriteExecutionManager(lambda: service)
        called = False

        def fail_connect(config):
            nonlocal called
            called = True
            raise AssertionError("remote connect must not be reached")

        monkeypatch.setattr("carbon_lens.executions.connect", fail_connect)
        with pytest.raises(WritePolicyError, match="WRITE_DISABLED"):
            manager.submit("test", "INSERT INTO business.original_table SELECT 1")
        service.config_store._config = config(tmp_path, enabled=True)
        with pytest.raises(WritePolicyError):
            manager.submit("test", "INSERT INTO business.original_table SELECT 1")
        assert called is False
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_ctas_reserves_before_remote_start_and_activates_only_after_identity_check(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession(service)
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.executions.connect", lambda config: session)
        manager = WriteExecutionManager(lambda: service)

        queued = manager.submit("test", "CREATE TABLE carbon_lens_workspace.cl_tmp_a1 AS SELECT 1", idempotency_key="create-a1")

        assert queued["status"] == "running"
        reserved = manager.workspace_list("test")["tables"]
        assert reserved[0]["state"] == "creating"
        completed = manager.status(queued["execution_id"])

        assert completed["status"] == "succeeded"
        assert manager.workspace_list("test")["tables"][0]["state"] == "active"
        assert completed["affected_row_count_known"] is False
        assert session.start_count == 1
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_idempotency_reuses_one_remote_submission_and_conflict_is_rejected(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession(service)
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.executions.connect", lambda config: session)
        manager = WriteExecutionManager(lambda: service)

        first = manager.submit("test", "CREATE TABLE carbon_lens_workspace.cl_tmp_a2 AS SELECT 1", idempotency_key="same")
        second = manager.submit("test", "CREATE TABLE carbon_lens_workspace.cl_tmp_a2 AS SELECT 1", idempotency_key="same")

        assert second["execution_id"] == first["execution_id"]
        assert session.start_count == 1
        with pytest.raises(WritePolicyError, match="IDEMPOTENCY_CONFLICT"):
            manager.submit("test", "CREATE TABLE carbon_lens_workspace.cl_tmp_a3 AS SELECT 1", idempotency_key="same")
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_insert_and_drop_require_active_registration_and_update_state(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession(service)
        service.exists.add("carbon_lens_workspace.cl_tmp_owned")
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.executions.connect", lambda config: session)
        manager = WriteExecutionManager(lambda: service)
        manager._insert_table(
            {
                "table_id": "mt_owned",
                "connection": "test",
                "workspace_schema": "carbon_lens_workspace",
                "qualified_name": "carbon_lens_workspace.cl_tmp_owned",
                "create_execution_id": "cx_seed",
                "create_sql_fingerprint": "seed",
                "state": "active",
                "created_at": "now",
                "activated_at": "now",
                "dropped_at": None,
                "last_verified_at": "now",
                "error": None,
            }
        )

        inserted = manager.submit("test", "INSERT INTO carbon_lens_workspace.cl_tmp_owned SELECT 1", idempotency_key="insert")
        assert manager.status(inserted["execution_id"])["status"] == "succeeded"
        dropped = manager.submit("test", "DROP TABLE carbon_lens_workspace.cl_tmp_owned", idempotency_key="drop")
        service.exists.remove("carbon_lens_workspace.cl_tmp_owned")
        assert manager.status(dropped["execution_id"])["status"] == "succeeded"
        assert manager.workspace_list("test")["tables"][0]["state"] == "dropped"
        with pytest.raises(WritePolicyError, match="UNOWNED_TABLE"):
            manager.submit("test", "INSERT INTO carbon_lens_workspace.cl_tmp_owned SELECT 1")
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_start_failure_is_unknown_and_never_resubmits(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        starts = 0

        def fail_connect(config):
            nonlocal starts
            starts += 1
            raise OSError("connection lost after submit")

        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.executions.connect", fail_connect)
        manager = WriteExecutionManager(lambda: service)
        result = manager.submit("test", "CREATE TABLE carbon_lens_workspace.cl_tmp_unknown AS SELECT 1", idempotency_key="unknown")

        assert result["status"] == "unknown"
        assert result["error_kind"] == "EXECUTION_UNKNOWN"
        assert "reconciliation" in result["error"]
        assert starts == 1
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_remote_failure_marks_ctas_failed_and_never_active(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession(service, state="FAILED")
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.executions.connect", lambda config: session)
        manager = WriteExecutionManager(lambda: service)

        submitted = manager.submit(
            "test",
            "CREATE TABLE carbon_lens_workspace.cl_tmp_failed AS SELECT 1",
            idempotency_key="remote-failure",
        )
        result = manager.status(submitted["execution_id"])

        assert result["status"] == "failed"
        assert result["error_kind"] == "REMOTE_FAILED"
        assert manager.workspace_list("test")["tables"][0]["state"] == "create_failed"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_remote_dialect_failure_has_distinct_error_kind(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession(service, state="FAILED", log="syntax error near OVERWRITE\n")
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.executions.connect", lambda config: session)
        manager = WriteExecutionManager(lambda: service)

        submitted = manager.submit(
            "test",
            "CREATE TABLE carbon_lens_workspace.cl_tmp_dialect AS SELECT 1",
            idempotency_key="dialect-failure",
        )

        assert manager.status(submitted["execution_id"])["error_kind"] == "UNSUPPORTED_DIALECT"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_cancel_race_does_not_override_remote_success(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession(service, state="RUNNING")
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.executions.connect", lambda config: session)
        manager = WriteExecutionManager(lambda: service)

        submitted = manager.submit(
            "test",
            "CREATE TABLE carbon_lens_workspace.cl_tmp_race AS SELECT 1",
            idempotency_key="cancel-race",
        )
        assert manager.cancel(submitted["execution_id"])["status"] == "cancel_requested"
        session.state = "SUCCEEDED"

        result = manager.status(submitted["execution_id"])

        assert result["status"] == "succeeded"
        assert manager.workspace_list("test")["tables"][0]["state"] == "active"
        assert session.cancel_uploads == 1
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_missing_remote_status_times_out_to_unknown_without_resubmission(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession(service, state="")
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.executions.connect", lambda config: session)
        manager = WriteExecutionManager(lambda: service)

        submitted = manager.submit(
            "test",
            "CREATE TABLE carbon_lens_workspace.cl_tmp_missing AS SELECT 1",
            idempotency_key="missing-status",
        )
        manager._update_execution(
            submitted["execution_id"],
            started_epoch=time.time() - 10000,
            started_at="old",
        )
        result = manager.status(submitted["execution_id"])
        again = manager.submit(
            "test",
            "CREATE TABLE carbon_lens_workspace.cl_tmp_missing AS SELECT 1",
            idempotency_key="missing-status",
        )

        assert result["status"] == "unknown"
        assert "reconciliation_required" in result["error"]
        assert again["execution_id"] == submitted["execution_id"]
        assert again["status"] == "unknown"
        assert session.start_count == 1
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_new_manager_recovers_existing_execution_without_second_remote_start(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession(service, state="RUNNING")
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.executions.connect", lambda config: session)
        manager_a = WriteExecutionManager(lambda: service)

        submitted = manager_a.submit(
            "test",
            "CREATE TABLE carbon_lens_workspace.cl_tmp_restart AS SELECT 1",
            idempotency_key="restart",
        )
        session.state = "SUCCEEDED"
        manager_b = WriteExecutionManager(lambda: service)
        result = manager_b.status(submitted["execution_id"])

        assert result["status"] == "succeeded"
        assert session.start_count == 1
        assert manager_b.workspace_list("test")["tables"][0]["state"] == "active"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
