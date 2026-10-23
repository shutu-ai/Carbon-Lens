from __future__ import annotations

from pathlib import Path
import shutil
import uuid

import pytest

from carbon_lens.config import CarbonLensError, ConnectionConfig
from carbon_lens.ssh import SSHSession, connect


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


def test_connect_retries_only_transient_setup_errors(monkeypatch):
    tmp_path = Path(__file__).resolve().parents[1] / (".test-ssh-" + uuid.uuid4().hex)
    tmp_path.mkdir(parents=True)
    calls: list[int] = []
    session = SSHSession(object())

    def flaky_connect(config):
        calls.append(1)
        if len(calls) == 1:
            raise CarbonLensError("SSH connection failed: example: timed out")
        return session

    monkeypatch.setattr("carbon_lens.ssh._connect_once", flaky_connect)

    try:
        result = connect(config(tmp_path), attempts=2, backoff_seconds=0)

        assert result is session
        assert len(calls) == 2
        assert result.connection_attempts == 2
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_connect_does_not_retry_authentication_errors(monkeypatch):
    tmp_path = Path(__file__).resolve().parents[1] / (".test-ssh-auth-" + uuid.uuid4().hex)
    tmp_path.mkdir(parents=True)
    calls: list[int] = []

    def auth_failure(config):
        calls.append(1)
        raise CarbonLensError("SSH connection failed: Authentication failed")

    monkeypatch.setattr("carbon_lens.ssh._connect_once", auth_failure)

    try:
        with pytest.raises(CarbonLensError, match="Authentication failed"):
            connect(config(tmp_path), attempts=3, backoff_seconds=0)

        assert len(calls) == 1
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
