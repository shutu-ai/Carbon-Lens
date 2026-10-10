"""Configuration loading for CarbonLens.

Credentials stay in the server-side configuration. Tool inputs only contain a
connection name, SQL, and bounded query/export options.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 7200


class CarbonLensError(RuntimeError):
    """A safe, user-facing CarbonLens error."""


@dataclass(frozen=True)
class ConnectionConfig:
    name: str
    ssh: dict[str, Any]
    jump: dict[str, Any]
    env_script: str
    beeline_path: str
    beeline_connection: str
    principal: str
    keytab: str
    run_as_user: str
    root_password: str
    remote_tmp_dir: str
    export_dir: Path
    default_timeout: int
    max_rows: int
    max_output_bytes: int
    known_hosts: str
    host_key_sha256: str
    auto_save_host_key: bool = True
    export_temp_ttl_seconds: int = 1800

    @classmethod
    def from_mapping(cls, name: str, raw: dict[str, Any], base_dir: Path) -> "ConnectionConfig":
        transport = str(raw.get("carbon_transport") or "beeline").lower()
        if transport != "beeline":
            raise CarbonLensError(
                f"连接 {name!r} 当前只支持 carbon_transport=beeline；JDBC 适配将在后续版本加入"
            )
        ssh = dict(raw.get("ssh_config") or raw.get("ssh") or {})
        jump = dict(raw.get("jump_config") or raw.get("jump") or {})
        if not ssh.get("hostname"):
            raise CarbonLensError(f"连接 {name!r} 缺少 ssh_config.hostname")
        if not ssh.get("username"):
            raise CarbonLensError(f"连接 {name!r} 缺少 ssh_config.username")

        password_env = str(ssh.get("password_env") or raw.get("password_env") or "").strip()
        if password_env:
            ssh["password"] = os.environ.get(password_env, "")

        run_as_user = str(raw.get("run_as_user") or "").strip()
        if run_as_user not in ("", "root"):
            raise CarbonLensError("run_as_user 当前只支持 root")
        root_password_env = str(raw.get("root_password_env") or "").strip()

        def resolve_local_path(value: Any) -> str:
            path = Path(str(value)).expanduser()
            if not path.is_absolute():
                path = base_dir / path
            return str(path.resolve())

        # These paths are read by the local MCP process. Relative values are
        # intentionally resolved from the configuration file directory, not
        # from whichever working directory Codex happens to use.
        for local_key in ("private_key", "known_hosts"):
            if ssh.get(local_key):
                ssh[local_key] = resolve_local_path(ssh[local_key])
            if jump.get(local_key):
                jump[local_key] = resolve_local_path(jump[local_key])

        export_dir_value = str(raw.get("export_dir") or "exports")
        export_dir = Path(export_dir_value).expanduser()
        if not export_dir.is_absolute():
            export_dir = base_dir / export_dir

        def bounded_int(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(raw.get(key, default))
            except (TypeError, ValueError):
                value = default
            return max(low, min(value, high))

        return cls(
            name=name,
            ssh=ssh,
            jump=jump,
            env_script=str(raw.get("env_script") or "/opt/client/bigdata_env"),
            beeline_path=str(raw.get("beeline_path") or "/opt/client/Fiber/bin/fiber-beeline"),
            beeline_connection=str(
                raw.get("beeline_connection")
                or raw.get("connection_name")
                or "Carbon_Query_XDR"
            ),
            principal=str(raw.get("principal") or "ossuser"),
            keytab=str(raw.get("keytab") or "/opt/client/keytab/ossuser.keytab"),
            run_as_user=run_as_user,
            root_password=str(raw.get("root_password") or "")
            or (os.environ.get(root_password_env, "") if root_password_env else ""),
            remote_tmp_dir=str(raw.get("remote_tmp_dir") or "/tmp"),
            export_dir=export_dir.resolve(),
            default_timeout=bounded_int("timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 1, MAX_TIMEOUT_SECONDS),
            max_rows=bounded_int("max_rows", 1000, 1, 1_000_000),
            max_output_bytes=bounded_int("max_output_bytes", 100 * 1024 * 1024, 1024, 512 * 1024 * 1024),
            known_hosts=(
                resolve_local_path(raw["known_hosts"])
                if raw.get("known_hosts")
                else str(ssh.get("known_hosts") or "")
            ),
            host_key_sha256=str(raw.get("host_key_sha256") or ""),
            auto_save_host_key=bool(raw.get("auto_save_host_key", True)),
            export_temp_ttl_seconds=bounded_int("export_temp_ttl_seconds", 1800, 300, 86400),
        )


class ConfigStore:
    """Load named Carbon connections from a JSON file."""

    def __init__(self, path: str | Path | None = None):
        configured = path or os.environ.get("CARBON_LENS_CONFIG") or "config.json"
        self.path = Path(configured).expanduser().resolve()
        self._payload: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._payload is None:
            if not self.path.is_file():
                raise CarbonLensError(
                    f"CarbonLens 配置文件不存在：{self.path}。"
                    "请复制 config.example.json 并设置 CARBON_LENS_CONFIG。"
                )
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CarbonLensError(f"无法读取 CarbonLens 配置：{exc}") from exc
            if not isinstance(payload, dict):
                raise CarbonLensError("CarbonLens 配置根节点必须是 JSON 对象")
            self._payload = payload
        return self._payload

    def _connections(self) -> dict[str, Any]:
        connections = self._load().get("connections")
        if not isinstance(connections, dict) or not connections:
            raise CarbonLensError("CarbonLens 配置中没有 connections")
        return connections

    def get(self, name: str) -> ConnectionConfig:
        if not name or name not in self._connections():
            raise CarbonLensError(f"未找到 Carbon 连接：{name or '<空>'}")
        raw = self._connections()[name]
        if not isinstance(raw, dict):
            raise CarbonLensError(f"连接 {name!r} 的配置必须是 JSON 对象")
        return ConnectionConfig.from_mapping(name, raw, self.path.parent)

    def public_list(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for name, raw in self._connections().items():
            if not isinstance(raw, dict):
                continue
            ssh = raw.get("ssh_config") or raw.get("ssh") or {}
            result.append(
                {
                    "name": str(name),
                    "transport": str(raw.get("carbon_transport") or "beeline"),
                    "connection_mode": str(raw.get("connection_mode") or "ssh"),
                    "host": str(ssh.get("hostname") or ""),
                    "beeline_connection": str(
                        raw.get("beeline_connection")
                        or raw.get("connection_name")
                        or "Carbon_Query_XDR"
                    ),
                }
            )
        return result
