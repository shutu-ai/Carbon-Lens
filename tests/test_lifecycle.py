from __future__ import annotations

import os
from pathlib import Path

from carbon_lens.lifecycle import InstanceLock, _pid_exists


def test_instance_lock_acquire_and_release():
    lock_path = Path(__file__).resolve().parents[1] / ".lifecycle-test.lock"
    lock = InstanceLock(lock_path, Path("config.json"))
    try:
        lock.acquire()
        assert lock._handle is not None
        assert lock_path.is_file()
    finally:
        lock.release()
        lock_path.unlink(missing_ok=True)


def test_instance_lock_for_config_is_stable():
    first = InstanceLock.for_config("config.json", parent_pid=1234)
    second = InstanceLock.for_config(Path("config.json").resolve(), parent_pid=1234)
    assert first.lock_path == second.lock_path


def test_instance_lock_is_distinct_per_parent_agent():
    first = InstanceLock.for_config("config.json", parent_pid=1234)
    second = InstanceLock.for_config("config.json", parent_pid=5678)
    assert first.lock_path != second.lock_path


def test_pid_exists_for_current_process():
    assert _pid_exists(os.getpid())
    assert not _pid_exists(2**31 - 1)
