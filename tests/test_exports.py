from __future__ import annotations

import shutil
from pathlib import Path
import uuid

from carbon_lens.config import ConnectionConfig
from carbon_lens.exports import ExportJobManager
from carbon_lens.service import _csv_summary


def runtime_dir() -> Path:
    path = Path(__file__).resolve().parents[1] / (".test-export-" + uuid.uuid4().hex)
    path.mkdir(parents=True, exist_ok=False)
    return path


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
        run_as_user="root",
        root_password="",
        remote_tmp_dir="/tmp",
        export_dir=tmp_path / "exports",
        default_timeout=60,
        max_rows=1000,
        max_output_bytes=1024,
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
    def __init__(self):
        self.started = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def make_dir(self, path: str):
        self.started = True

    def upload_text(self, path: str, content: str):
        self.started = True

    def start_detached(self, command: str, password: str = "") -> str:
        assert "nohup" in command
        return "12345"

    def read_text(self, path: str, max_bytes: int | None = None):
        if path.endswith("/status"):
            return "SUCCEEDED\n"
        if path.endswith("/pid"):
            return "12345\n"
        return None

    def download_file(self, remote_path: str, local_path: str | Path):
        Path(local_path).write_text("name,value\na,1\nb,2\n", encoding="utf-8")

    def run(self, command: str, timeout: int = 30, password: str = "", username: str = ""):
        return 0, "", ""


class FlakyDownloadSession(FakeSession):
    def __init__(self):
        self.download_attempts = 0

    def download_file(self, remote_path: str, local_path: str | Path):
        self.download_attempts += 1
        if self.download_attempts == 1:
            raise OSError("simulated interrupted SFTP download")
        super().download_file(remote_path, local_path)


def test_background_export_downloads_after_remote_success(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession()
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.exports.connect", lambda config: session)

        manager = ExportJobManager(lambda: service)
        queued = manager.submit("test", "SELECT * FROM demo", limit=5)

        assert queued["status"] == "running"
        assert queued["download_status"] == "pending"
        completed = manager.status(queued["export_id"])

        assert completed["status"] == "succeeded"
        assert completed["download_status"] == "completed"
        assert completed["row_count"] == 2
        assert completed["columns"] == ["name", "value"]
        assert Path(completed["local_path"]).read_text(encoding="utf-8") == "name,value\na,1\nb,2\n"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_cancel_race_uses_remote_success_and_does_not_fake_cancellation(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FakeSession()
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.exports.connect", lambda config: session)

        manager = ExportJobManager(lambda: service)
        queued = manager.submit("test", "SELECT * FROM demo", limit=5)
        requested = manager.cancel(queued["export_id"])
        assert requested["status"] == "cancel_requested"

        completed = manager.status(queued["export_id"])
        assert completed["status"] == "succeeded"
        assert completed["download_status"] == "completed"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_interrupted_download_is_retryable_without_resubmitting_sql(monkeypatch):
    tmp_path = runtime_dir()
    try:
        service = FakeService(tmp_path)
        session = FlakyDownloadSession()
        monkeypatch.setenv("CARBON_LENS_JOB_DB", str(tmp_path / "jobs.sqlite3"))
        monkeypatch.setattr("carbon_lens.exports.connect", lambda config: session)

        manager = ExportJobManager(lambda: service)
        queued = manager.submit("test", "SELECT * FROM demo", limit=5)
        first = manager.status(queued["export_id"])
        assert first["status"] == "downloading"
        assert first["download_status"] == "failed"

        completed = manager.status(queued["export_id"])
        assert completed["status"] == "succeeded"
        assert session.download_attempts == 2
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_background_json_export_is_materialized_without_loading_rows():
    tmp_path = runtime_dir()
    try:
        source = tmp_path / "result.csv"
        target = tmp_path / "result.json"
        source.write_text("name,value\na,1\nb,2\n", encoding="utf-8")

        ExportJobManager._csv_to_json(source, target)

        assert target.read_text(encoding="utf-8") == '[\n{"name": "a", "value": "1"},\n{"name": "b", "value": "2"}\n]\n'
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_json_export_preserves_duplicate_columns():
    tmp_path = runtime_dir()
    try:
        source = tmp_path / "duplicate.csv"
        target = tmp_path / "duplicate.json"
        source.write_text("id,id,ID,\n1,2,3,\n", encoding="utf-8")

        columns, original = ExportJobManager._csv_to_json(source, target)

        assert columns == ["id", "id__2", "ID__3", "column_4"]
        assert original == ["id", "id", "ID", ""]
        assert target.read_text(encoding="utf-8") == '[\n{"id": "1", "id__2": "2", "ID__3": "3", "column_4": ""}\n]\n'
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_csv_summary_rejects_mismatched_rows():
    tmp_path = runtime_dir()
    try:
        source = tmp_path / "invalid.csv"
        source.write_text("a,b\n1\n", encoding="utf-8")
        try:
            _csv_summary(source)
        except Exception as exc:
            assert "different column count" in str(exc)
        else:
            raise AssertionError("malformed CSV was accepted")
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
