"""Process-lifecycle safeguards for local stdio MCP deployments."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
import time


class InstanceAlreadyRunning(RuntimeError):
    """Raised when another MCP process owns the same configuration lock."""


def _pid_exists(pid: int) -> bool:
    if pid <= 1:
        return True
    if os.name == "nt":
        # os.kill(pid, 0) can block for an invalid large PID on Windows.
        # OpenProcess is a bounded kernel query and distinguishes a missing
        # process from one that exists but is not queryable by this user.
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process = kernel32.OpenProcess(0x1000, False, int(pid))
            if process:
                kernel32.CloseHandle(process)
                return True
            error = ctypes.get_last_error()
            # ERROR_ACCESS_DENIED means the process exists but is not
            # queryable by this account; only invalid-handle/invalid-PID
            # errors prove that it is absent.
            return error not in {6, 87, 1168}
        except (AttributeError, OSError):
            # Keep a conservative fallback for unusual Windows runtimes.
            return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if exc.errno in (errno.ESRCH, errno.EINVAL) or winerror in (87, 1168):
            return False
        return True
    return True


class InstanceLock:
    """Hold an OS-level lock so one Agent has at most one MCP process."""

    def __init__(self, lock_path: Path, config_path: Path):
        self.lock_path = lock_path
        self.config_path = config_path
        self._handle = None

    @classmethod
    def for_config(
        cls,
        config_path: str | Path | None = None,
        parent_pid: int | None = None,
    ) -> "InstanceLock":
        configured = config_path or os.environ.get("CARBON_LENS_CONFIG") or "config.json"
        resolved_config = Path(configured).expanduser().resolve()
        owner_pid = int(parent_pid if parent_pid is not None else os.getppid())
        identity = f"{os.path.normcase(str(resolved_config))}\0{owner_pid}".encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()[:32]
        lock_path = Path(tempfile.gettempdir()) / f"carbon-lens-mcp-{digest}.lock"
        return cls(lock_path, resolved_config)

    @classmethod
    def for_http(cls, config_path: str | Path | None = None) -> "InstanceLock":
        """Return a stable lock for the long-lived HTTP service.

        Unlike stdio, an HTTP server must not derive ownership from its
        launching parent PID: clients may disconnect and the launcher may be
        short-lived while the server continues serving other clients.
        """
        configured = config_path or os.environ.get("CARBON_LENS_CONFIG") or "config.json"
        resolved_config = Path(configured).expanduser().resolve()
        identity = f"{os.path.normcase(str(resolved_config))}\0http".encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()[:32]
        lock_path = Path(tempfile.gettempdir()) / f"carbon-lens-http-{digest}.lock"
        return cls(lock_path, resolved_config)

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise InstanceAlreadyRunning(
                f"CarbonLens MCP is already running for config: {self.config_path}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


class ParentProcessMonitor:
    """Exit the MCP process when its launching Agent process disappears."""

    def __init__(self, parent_pid: int | None = None, interval_seconds: float = 2.0):
        self.parent_pid = int(parent_pid if parent_pid is not None else os.getppid())
        self.interval_seconds = max(0.5, float(interval_seconds))
        self._stop = Event()
        self._thread: Thread | None = None

    @property
    def enabled(self) -> bool:
        # Agent CLIs may launch stdio servers through a short-lived wrapper
        # process. In that layout os.getppid() is not the Agent session, so
        # treating the wrapper's exit as a client disconnect breaks transport.
        # Opt in only when the launcher has a stable parent PID.
        value = os.environ.get("CARBON_LENS_WATCH_PARENT", "0").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def start(self) -> None:
        if not self.enabled or self.parent_pid <= 1:
            return
        self._thread = Thread(
            target=self._watch,
            name="carbon-lens-parent-watch",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 0.5)
            self._thread = None

    def _watch(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if _pid_exists(self.parent_pid):
                continue
            sys.stderr.write(
                f"CarbonLens MCP parent process {self.parent_pid} exited; stopping MCP.\n"
            )
            sys.stderr.flush()
            os._exit(0)
