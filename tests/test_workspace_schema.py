from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from carbon_lens.config import CarbonLensError, ConnectionConfig
from carbon_lens.service import CarbonLensService


TEST_BASE = Path("C:/carbon-lens-workspace-schema-test")


def make_config(base_dir: Path, *, enabled: bool = True, schema: str = "carbon_lens") -> ConnectionConfig:
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
        export_dir=base_dir / "exports",
        default_timeout=60,
        max_rows=1000,
        max_output_bytes=1024 * 1024,
        known_hosts="known_hosts",
        host_key_sha256="",
        write_workspace_enabled=enabled,
        write_workspace_schema=schema,
        write_table_prefix="cl_g8_",
    )


class Store:
    def __init__(self, base_dir: Path, *, enabled: bool = True, schema: str = "carbon_lens"):
        self.path = base_dir / "config.json"
        self.config = make_config(base_dir, enabled=enabled, schema=schema)

    def get(self, name: str) -> ConnectionConfig:
        assert name == "test"
        return self.config


class Session:
    def __init__(self):
        self.uploads: list[tuple[str, str]] = []
        self.removed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def upload_text(self, path: str, content: str):
        self.uploads.append((path, content))

    def exec(self, script: str, timeout: int, get_pty: bool = False):
        assert timeout == 60
        assert get_pty is False
        return None, [""], BytesIO()

    def wait_exit(self, stdout, timeout: int):
        return 0

    def remove(self, path: str):
        self.removed.append(path)


def test_schema_create_is_plan_only_by_default():
    service = CarbonLensService(Store(TEST_BASE))

    result = service.create_workspace_schema("test")

    assert result == {
        "connection": "test",
        "workspace_schema": "carbon_lens",
        "sql": "CREATE DATABASE IF NOT EXISTS carbon_lens",
        "confirmed": False,
        "executed": False,
        "status": "planned",
    }


def test_schema_create_requires_a_configured_schema():
    service = CarbonLensService(Store(TEST_BASE, schema=""))

    with pytest.raises(CarbonLensError, match="workspace schema"):
        service.create_workspace_schema("test", confirm=True)


def test_schema_create_uses_only_configured_schema(monkeypatch):
    service = CarbonLensService(Store(TEST_BASE, enabled=False))
    session = Session()
    monkeypatch.setattr("carbon_lens.service.connect", lambda config: session)

    result = service.create_workspace_schema("test", confirm=True)

    assert result["status"] == "succeeded"
    assert result["executed"] is True
    assert session.uploads[0][1] == "CREATE DATABASE IF NOT EXISTS carbon_lens"
    assert session.removed == [session.uploads[0][0]]


def test_schema_status_does_not_treat_truncated_list_as_absent():
    service = CarbonLensService(Store(TEST_BASE))
    service.query = lambda *args, **kwargs: {
        "rows": [{"databaseName": "default"}],
        "returned_row_count": 1,
        "truncated": True,
        "elapsed_ms": 4,
    }

    result = service.workspace_schema_status("test")

    assert result["exists"] is None
    assert result["read_only"] is True
