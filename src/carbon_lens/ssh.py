"""Small SSH adapter used by CarbonLens.

The adapter deliberately exposes command execution only to the fixed Carbon
query service; no arbitrary shell tool is exposed to the model.
"""

from __future__ import annotations

import base64
import hashlib
import shlex
from threading import Event
import time
from pathlib import Path
from typing import Any

from .config import CarbonLensError, ConnectionConfig


def _fingerprint(client: Any) -> str | None:
    try:
        key = client.get_transport().get_remote_server_key()
        digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        return "SHA256:" + digest
    except Exception:
        return None


def _save_host_keys(client: Any, known_hosts_path: str) -> None:
    """Persist the key accepted by Paramiko's host-key policy."""
    if not known_hosts_path:
        return
    path = Path(known_hosts_path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        client.get_host_keys().save(str(path))
    except Exception as exc:
        raise CarbonLensError(f"known_hosts save failed: {path}: {exc}") from exc


def _connect_client(
    client: Any,
    options: dict[str, Any],
    known_hosts: str = "",
    sock: Any = None,
    allow_unknown_host: bool = False,
    save_host_key: bool = False,
) -> None:
    import paramiko

    known_hosts_path = str(options.get("known_hosts") or known_hosts or "").strip()
    if known_hosts_path:
        path = Path(known_hosts_path).expanduser()
        if path.is_file():
            client.load_host_keys(str(path))
    else:
        client.load_system_host_keys()
    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()
        if allow_unknown_host or (save_host_key and bool(known_hosts_path))
        else paramiko.RejectPolicy()
    )
    try:
        client.connect(
            hostname=options.get("hostname"),
            port=int(options.get("port", 22)),
            username=options.get("username"),
            password=options.get("password") or None,
            key_filename=(options.get("private_key") or None),
            sock=sock,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )
    except Exception as exc:
        raise CarbonLensError(f"SSH connection failed: {options.get('hostname')}: {exc}") from exc


class SSHSession:
    def __init__(self, client: Any, jump_client: Any = None):
        self.client = client
        self.jump_client = jump_client

    def upload_text(self, remote_path: str, content: str) -> None:
        try:
            with self.client.open_sftp() as sftp:
                with sftp.open(remote_path, "w") as handle:
                    handle.write(content)
        except Exception as exc:
            raise CarbonLensError(f"remote SQL upload failed: {exc}") from exc

    def read_text(self, remote_path: str, max_bytes: int | None = None) -> str | None:
        try:
            with self.client.open_sftp() as sftp:
                with sftp.open(remote_path, "r") as handle:
                    payload = handle.read(max_bytes + 1 if max_bytes is not None else -1)
                    if max_bytes is not None and len(payload) > max_bytes:
                        raise CarbonLensError(f"remote file exceeds output limit: {remote_path}")
                    return payload.decode("utf-8", errors="replace")
        except Exception as exc:
            if getattr(exc, "errno", None) == 2 or "No such file" in str(exc):
                return None
            raise CarbonLensError(f"remote file read failed: {remote_path}: {exc}") from exc

    def make_dir(self, remote_path: str) -> None:
        try:
            with self.client.open_sftp() as sftp:
                sftp.mkdir(remote_path, mode=0o700)
        except Exception as exc:
            raise CarbonLensError(f"remote export directory creation failed: {exc}") from exc

    def download_file(self, remote_path: str, local_path: str | Path) -> None:
        try:
            with self.client.open_sftp() as sftp:
                sftp.get(remote_path, str(local_path))
        except Exception as exc:
            raise CarbonLensError(f"remote export download failed: {exc}") from exc

    def remove(self, remote_path: str) -> None:
        try:
            with self.client.open_sftp() as sftp:
                sftp.remove(remote_path)
        except Exception:
            pass

    def _run_privileged(self, command: str, username: str, password: str, timeout: int = 15) -> None:
        try:
            wrapped = "su - " + shlex.quote(username) + " -c " + shlex.quote(command)
            stdin, stdout, _ = self.exec(wrapped, timeout, get_pty=False)
            if stdin is not None:
                stdin.write(password + "\n")
                stdin.flush()
                stdin.close()
            self.wait_exit(stdout, timeout)
        except Exception:
            pass

    def remove_privileged(
        self,
        remote_path: str,
        username: str,
        password: str,
        timeout: int = 15,
        recursive: bool = False,
    ) -> None:
        command = ("rm -rf -- " if recursive else "rm -f ") + shlex.quote(remote_path)
        self._run_privileged(command, username, password, timeout)

    def cleanup_stale_export_dirs(
        self,
        remote_tmp_dir: str,
        username: str = "",
        password: str = "",
        ttl_seconds: int = 1800,
        timeout: int = 30,
    ) -> None:
        minutes = max(5, (int(ttl_seconds) + 59) // 60)
        command = (
            "find "
            + shlex.quote(remote_tmp_dir)
            + " -maxdepth 1 -mindepth 1 -type d -name "
            + shlex.quote("carbon-lens-export-*")
            + f" -mmin +{minutes} -exec rm -rf -- {{}} +"
        )
        if username and password:
            self._run_privileged(command, username, password, timeout)
            return
        try:
            stdin, stdout, _ = self.exec(command, timeout, get_pty=False)
            if stdin is not None:
                stdin.close()
            self.wait_exit(stdout, timeout)
        except Exception:
            pass

    def exec(self, command: str, timeout: int, get_pty: bool = False):
        try:
            return self.client.exec_command(command, timeout=timeout, get_pty=get_pty)
        except Exception as exc:
            raise CarbonLensError(f"remote Carbon command failed to start: {exc}") from exc

    def run(
        self,
        command: str,
        timeout: int = 30,
        password: str = "",
        username: str = "",
    ) -> tuple[int, str, str]:
        wrapped = command
        if username:
            wrapped = "su - " + shlex.quote(username) + " -c " + shlex.quote(command)
        stdin, stdout, stderr = self.exec(wrapped, timeout, get_pty=False)
        if stdin is not None:
            if password:
                stdin.write(password + "\n")
                stdin.flush()
            stdin.close()
        status = self.wait_exit(stdout, timeout)
        output = stdout.read().decode("utf-8", errors="replace") if stdout is not None else ""
        error = stderr.read().decode("utf-8", errors="replace") if stderr is not None else ""
        return status, output, error

    def start_detached(self, command: str, password: str = "", timeout: int = 30) -> str:
        status, output, error = self.run(command, timeout=timeout, password=password)
        if status != 0:
            detail = error.strip()[-4000:] or output.strip()[-4000:] or f"exit code {status}"
            raise CarbonLensError(f"remote Carbon job failed to start: {detail}")
        return output.strip().splitlines()[-1] if output.strip() else ""

    def remove_tree(
        self,
        remote_path: str,
        username: str = "",
        password: str = "",
        timeout: int = 30,
    ) -> None:
        command = "rm -rf -- " + shlex.quote(remote_path)
        try:
            self.run(command, timeout=timeout, username=username, password=password)
        except Exception:
            pass

    def cancel(self) -> None:
        """Close the active SSH session to interrupt a remote command."""
        self.close()

    def wait_exit(self, channel_file: Any, timeout: int, cancel_event: Event | None = None) -> int:
        channel = channel_file.channel
        deadline = time.monotonic() + timeout
        while not channel.exit_status_ready():
            if cancel_event is not None and cancel_event.is_set():
                channel.close()
                raise CarbonLensError("Carbon 查询已取消")
            if time.monotonic() >= deadline:
                channel.close()
                raise CarbonLensError("remote Carbon command timed out")
            time.sleep(0.1)
        return channel.recv_exit_status()

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            if self.jump_client is not None:
                self.jump_client.close()

    def __enter__(self) -> "SSHSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


def connect(config: ConnectionConfig) -> SSHSession:
    import paramiko

    target = config.ssh
    jump = config.jump
    jump_client = None
    channel = None
    try:
        if jump.get("hostname"):
            jump_client = paramiko.SSHClient()
            jump_known_hosts = str(jump.get("known_hosts") or "")
            _connect_client(
                jump_client,
                jump,
                allow_unknown_host=bool(jump.get("host_key_sha256")),
                save_host_key=bool(jump_known_hosts),
            )
            if jump_known_hosts:
                _save_host_keys(jump_client, jump_known_hosts)
            transport = jump_client.get_transport()
            if transport is None or not transport.is_active():
                raise CarbonLensError("SSH jump host connection was not established")
            channel = transport.open_channel(
                "direct-tcpip",
                (target["hostname"], int(target.get("port", 22))),
                ("127.0.0.1", 0),
                timeout=15,
            )

        target_client = paramiko.SSHClient()
        _connect_client(
            target_client,
            target,
            config.known_hosts,
            sock=channel,
            allow_unknown_host=bool(config.host_key_sha256),
            save_host_key=config.auto_save_host_key,
        )
        expected = config.host_key_sha256.strip()
        actual = _fingerprint(target_client)
        if expected and actual != expected:
            target_client.close()
            raise CarbonLensError(
                f"SSH host fingerprint mismatch: expected {expected}, actual {actual or '<unknown>'}"
            )
        if config.auto_save_host_key:
            _save_host_keys(target_client, config.known_hosts)
        return SSHSession(target_client, jump_client)
    except CarbonLensError:
        if jump_client is not None:
            jump_client.close()
        raise
    except Exception as exc:
        if jump_client is not None:
            jump_client.close()
        raise CarbonLensError(f"Carbon SSH connection failed: {exc}") from exc
