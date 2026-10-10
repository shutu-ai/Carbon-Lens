from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import uuid
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "live_acceptance.py"
SPEC = importlib.util.spec_from_file_location("live_acceptance", MODULE_PATH)
assert SPEC and SPEC.loader
live_acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_acceptance)


def test_validate_export_file_checks_csv_shape():
    root = MODULE_PATH.parents[1] / (".test-live-" + uuid.uuid4().hex)
    root.mkdir()
    try:
        path = root / "result.csv"
        path.write_text("id,name\n1,one\n", encoding="utf-8")
        result = live_acceptance.validate_export_file({"path": str(path)}, "csv")
        assert result["columns"] == ["id", "name"]
        assert result["row_count"] == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_validate_export_file_checks_json_objects():
    root = MODULE_PATH.parents[1] / (".test-live-" + uuid.uuid4().hex)
    root.mkdir()
    try:
        path = root / "result.json"
        path.write_text(json.dumps([{"id": 1, "name": "one"}]), encoding="utf-8")
        result = live_acceptance.validate_export_file({"local_path": str(path)}, "json")
        assert result["columns"] == ["id", "name"]
        assert result["row_count"] == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_finalize_returns_real_exit_codes():
    assert live_acceptance.finalize({"checks": [{"status": "PASS"}]})["exit_code"] == 0
    assert live_acceptance.finalize({"checks": [{"status": "FAIL"}]})["exit_code"] == 1
    assert live_acceptance.finalize({"checks": [{"status": "BLOCKED_ENV"}]})["exit_code"] == 2
    assert live_acceptance.finalize({"checks": [{"status": "UNVERIFIED"}]})["exit_code"] == 2


def test_result_payload_unwraps_text_result_envelope():
    result = SimpleNamespace(
        structured_content=None,
        content=[SimpleNamespace(text=json.dumps({"result": {"status": "succeeded"}}))],
    )

    assert live_acceptance.result_payload(result) == {"status": "succeeded"}


def test_result_payload_keeps_task_status_with_nested_result():
    result = SimpleNamespace(
        structured_content={"status": "succeeded", "result": {"rows": [{"id": 1}]}},
        content=[],
    )

    assert live_acceptance.result_payload(result)["status"] == "succeeded"
