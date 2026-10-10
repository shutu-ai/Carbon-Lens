from carbon_lens.beeline import (
    BEGIN_MARKER,
    END_MARKER,
    build_export_script,
    build_export_job_script,
    build_script,
    parse_dsv,
    rows_as_dicts,
    validate_read_only,
)
from carbon_lens.config import ConnectionConfig
from pathlib import Path


def config() -> ConnectionConfig:
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
        export_dir=Path("."),
        default_timeout=60,
        max_rows=1000,
        max_output_bytes=1024,
        known_hosts="known_hosts",
        host_key_sha256="",
    )


def test_build_script_uses_fixed_beeline_flow():
    script = build_script(config(), "/tmp/query.sql")
    assert "source /opt/client/bigdata_env" in script
    assert "kinit -kt /opt/client/keytab/ossuser.keytab ossuser" in script
    assert "fiber-beeline Carbon_Query_XDR" in script
    assert "-f /tmp/query.sql" in script
    assert "su - root -c" in script
    assert "SQL" not in script


def test_build_export_script_redirects_csv_and_logs_remotely():
    script = build_export_script(
        config(),
        "/tmp/carbon-lens-export-test/query.sql",
        "/tmp/carbon-lens-export-test/result.csv",
        "/tmp/carbon-lens-export-test/result.log",
        remote_dir="/tmp/carbon-lens-export-test",
        ttl_seconds=1800,
    )
    assert "--outputformat=csv2" in script
    assert "> /tmp/carbon-lens-export-test/result.csv" in script
    assert "2> /tmp/carbon-lens-export-test/result.log" in script
    assert "nohup sh -c" in script
    assert "sleep 1800" in script
    assert "su - root -c" in script


def test_build_export_job_script_persists_status_and_supports_cancel():
    script = build_export_job_script(
        config(),
        "/tmp/carbon-lens-export-test/query.sql",
        "/tmp/carbon-lens-export-test/result.csv",
        "/tmp/carbon-lens-export-test/result.log",
        "/tmp/carbon-lens-export-test/status",
        "/tmp/carbon-lens-export-test/pid",
        "/tmp/carbon-lens-export-test/cancel",
        remote_dir="/tmp/carbon-lens-export-test",
        ttl_seconds=1800,
        timeout_seconds=600,
    )
    assert "RUNNING > /tmp/carbon-lens-export-test/status" in script
    assert "SUCCEEDED > /tmp/carbon-lens-export-test/status" in script
    assert "CANCELLED > /tmp/carbon-lens-export-test/status" in script
    assert "sleep 600" in script
    assert "-f /tmp/carbon-lens-export-test/cancel" in script


def test_parse_dsv_is_bounded():
    output = f"noise\n{BEGIN_MARKER}\na|b\n1|2\n3|4\n{END_MARKER}\n"
    columns, rows, truncated = parse_dsv(output, max_rows=1)
    assert columns == ["a", "b"]
    assert rows == [["1", "2"]]
    assert truncated is True


def test_validate_read_only_rejects_writes():
    validate_read_only("-- comment\nSELECT 1")
    try:
        validate_read_only("DELETE FROM users")
    except Exception as exc:
        assert "read-only SQL" in str(exc)
    else:
        raise AssertionError("write SQL was accepted")


def test_parse_dsv_preserves_empty_values_and_all_duplicate_columns():
    output = f"{BEGIN_MARKER}\nid|id|ID|\n1||3|\n|| |\n{END_MARKER}\n"
    columns, rows, truncated = parse_dsv(output, max_rows=10)
    assert columns == ["id", "id", "ID", ""]
    assert rows == [["1", "", "3", ""], ["", "", " ", ""]]
    assert truncated is False
    assert rows_as_dicts(columns, rows) == [
        {"id": "1", "id__2": "", "ID__3": "3", "column_4": ""},
        {"id": "", "id__2": "", "ID__3": " ", "column_4": ""},
    ]


def test_validate_read_only_rejects_multiple_statements_and_allows_read_only_cte():
    validate_read_only("WITH sample AS (SELECT 1) SELECT * FROM sample")
    try:
        validate_read_only("SELECT 1; DELETE FROM users")
    except Exception as exc:
        assert "one SQL statement" in str(exc)
    else:
        raise AssertionError("multiple SQL statements were accepted")
