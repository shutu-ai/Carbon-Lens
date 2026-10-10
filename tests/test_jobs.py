from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from carbon_lens.config import ConnectionConfig
from carbon_lens.jobs import QueryJobManager
from carbon_lens.beeline import BEGIN_MARKER, END_MARKER


def config(tmp_path: Path) -> ConnectionConfig:
    return ConnectionConfig(
        name="test",
        ssh={"hostname": "example", "username": "u"},
        jump={},
        env_script="/opt/client/bigdata_env",
        beeline_path="/opt/client/Fiber/bin/fiber-beeline",
        beeline_connection="Carbon_Query_XDR",
        principal="ossuser",
        keytab="/opt/client/keytab/ossuser.keytab",
        run_as_user="",
        root_password="",
        remote_tmp_dir="/tmp",
        export_dir=tmp_path / "exports",
        default_timeout=60,
        max_rows=1000,
        max_output_bytes=1024 * 1024,
        known_hosts="known_hosts",
        host_key_sha256="",
    )


class FakeStore:
    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "config.json"
        self._config = config(tmp_path)

    def get(self, name: str) -> ConnectionConfig:
        assert name == "test"
        return self._config


class FakeService:
    def __init__(self, tmp_path: Path):
        self.config_store = FakeStore(tmp_path)


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def make_dir(self, path: str):
        return None

    def upload_text(self, path: str, content: str):
        return None

    def start_detached(self, command: str, password: str = "") -> str:
        return "12345"

    def read_text(self, path: str, max_bytes: int | None = None):
        if path.endswith("/status"):
            return "SUCCEEDED\n"
        if path.endswith("/pid"):
            return "12345\n"
        if path.endswith("/result.dsv"):
            return f"{BEGIN_MARKER}\nid|id\n1|2\n{END_MARKER}\n"
        return None

    def run(self, command: str, timeout: int = 30, password: str = "", username: str = ""):
        return 0, "", ""


class RunningSession(FakeSession):
    def read_text(self, path: str, max_bytes: int | None = None):
        if path.endswith("/status"):
            return "RUNNING\n"
        if path.endswith("/pid"):
            return "12345\n"
        return None


def test_query_result_is_persisted_outside_sqlite_and_keeps_duplicate_fields(monkeypatch):
    tmp_path = Path(__file__).resolve().parents[1] / (".test-query-" + uuid.uuid4().hex)
    tmp_path.mkdir(parents=True)
    try:
        service = FakeService(tmp_path)
        session = FakeSession()
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.jobs.connect", lambda config: session)

        manager = QueryJobManager(lambda: service)
        queued = manager.submit("test", "SELECT id, id FROM demo", limit=5)
        completed = manager.status(queued["query_id"])

        assert completed["status"] == "succeeded"
        assert completed["result"]["columns"] == ["id", "id__2"]
        assert completed["result"]["rows"] == [{"id": "1", "id__2": "2"}]
        assert completed["result_available"] is True
        assert completed["result"]["type_info_unavailable"] is True
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as db:
            result_json, result_path = db.execute(
                "SELECT result_json, result_path FROM query_jobs"
            ).fetchone()
        assert result_json is None
        assert Path(result_path).is_file()
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_query_cancel_race_uses_remote_success(monkeypatch):
    tmp_path = Path(__file__).resolve().parents[1] / (".test-query-race-" + uuid.uuid4().hex)
    tmp_path.mkdir(parents=True)
    try:
        service = FakeService(tmp_path)
        session = FakeSession()
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.jobs.connect", lambda config: session)

        manager = QueryJobManager(lambda: service)
        queued = manager.submit("test", "SELECT id FROM demo", limit=5)
        requested = manager.cancel(queued["query_id"])
        assert requested["status"] == "cancel_requested"

        completed = manager.status(queued["query_id"])
        assert completed["status"] == "succeeded"
        assert completed["result"]["rows"] == [{"id": "1", "id__2": "2"}]
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_running_query_is_failed_after_timeout(monkeypatch):
    tmp_path = Path(__file__).resolve().parents[1] / (".test-query-timeout-" + uuid.uuid4().hex)
    tmp_path.mkdir(parents=True)
    try:
        service = FakeService(tmp_path)
        session = RunningSession()
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.jobs.connect", lambda config: session)

        manager = QueryJobManager(lambda: service)
        queued = manager.submit("test", "SELECT 1", limit=1, timeout_seconds=1)
        manager._update_job(queued["query_id"], started_epoch=time.time() - 1000)

        failed = manager.status(queued["query_id"])

        assert failed["status"] == "failed"
        assert "did not complete" in failed["error"]
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
