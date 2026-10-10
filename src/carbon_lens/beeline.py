"""Safe Carbon Beeline command construction and bounded result parsing."""

from __future__ import annotations

import csv
import io
import re
import shlex
from pathlib import Path
from threading import Event
from typing import Iterable

from .config import CarbonLensError, ConnectionConfig

BEGIN_MARKER = "@@CARBON_LENS_BEGIN@@"
END_MARKER = "@@CARBON_LENS_END@@"
QUERY_RUNNING = "RUNNING"
QUERY_SUCCEEDED = "SUCCEEDED"
QUERY_FAILED = "FAILED"
QUERY_CANCELLED = "CANCELLED"
EXPORT_RUNNING = QUERY_RUNNING
EXPORT_SUCCEEDED = QUERY_SUCCEEDED
EXPORT_FAILED = QUERY_FAILED
EXPORT_CANCELLED = QUERY_CANCELLED
READ_ONLY = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "USE"}


def _first_keyword(sql: str) -> str:
    without_comments = re.sub(r"(?s)^\s*(?:(?:--[^\n]*\n)|(?:/\*.*?\*/))*", "", sql)
    match = re.match(r"([A-Za-z]+)", without_comments)
    return match.group(1).upper() if match else ""


def validate_read_only(sql: str) -> None:
    if not sql or not sql.strip():
        raise CarbonLensError("SQL cannot be empty")
    if "\x00" in sql:
        raise CarbonLensError("SQL contains a NUL character")
    keyword = _first_keyword(sql)
    if keyword not in READ_ONLY:
        raise CarbonLensError(
            f"CarbonLens only allows read-only SQL (SELECT/SHOW/DESCRIBE/EXPLAIN/USE); received: {keyword or '<unknown>'}"
        )


def build_script(config: ConnectionConfig, remote_sql_path: str) -> str:
    """Build the exact remote shell flow without interpolating raw SQL."""
    source = shlex.quote(config.env_script)
    keytab = shlex.quote(config.keytab)
    principal = shlex.quote(config.principal)
    beeline = shlex.quote(config.beeline_path)
    alias = shlex.quote(config.beeline_connection)
    sql_file = shlex.quote(remote_sql_path)
    script = "\n".join(
        [
            "#!/bin/bash",
            "export TERM=dumb",
            "set -e",
            "source " + source + " >/dev/null 2>&1",
            "kinit -kt " + keytab + " " + principal,
            "printf '%s\\n' " + shlex.quote(BEGIN_MARKER),
            beeline
            + " "
            + alias
            + " --showHeader=true --silent=true --outputformat=dsv --delimiterForDSV='|' -f "
            + sql_file,
            "printf '%s\\n' " + shlex.quote(END_MARKER),
        ]
    )
    if config.run_as_user:
        return "su - " + shlex.quote(config.run_as_user) + " -c " + shlex.quote(script)
    return script


def build_query_job_script(
    config: ConnectionConfig,
    remote_sql_path: str,
    remote_output_path: str,
    remote_status_path: str,
    remote_error_path: str,
    remote_pid_path: str,
    remote_dir: str,
    remote_cancel_path: str = "",
    ttl_seconds: int = 2 * 24 * 60 * 60,
    timeout_seconds: int = 7200,
) -> str:
    """Build a detached query script whose state and result survive SSH reconnects."""
    source = shlex.quote(config.env_script)
    keytab = shlex.quote(config.keytab)
    principal = shlex.quote(config.principal)
    beeline = shlex.quote(config.beeline_path)
    alias = shlex.quote(config.beeline_connection)
    sql_file = shlex.quote(remote_sql_path)
    output_file = shlex.quote(remote_output_path)
    status_file = shlex.quote(remote_status_path)
    error_file = shlex.quote(remote_error_path)
    pid_file = shlex.quote(remote_pid_path)
    cancel_file = shlex.quote(remote_cancel_path or "/dev/null/nonexistent-carbon-lens-cancel")
    cleanup_dir = shlex.quote(remote_dir)
    ttl = max(3600, int(ttl_seconds))
    query_timeout = max(1, int(timeout_seconds))
    command = (
        beeline
        + " "
        + alias
        + " --showHeader=true --silent=true --outputformat=dsv --delimiterForDSV='|' -f "
        + sql_file
        + " >> "
        + output_file
        + " 2> "
        + error_file
    )
    return "\n".join(
        [
            "#!/bin/bash",
            "export TERM=dumb",
            "umask 022",
            "child_pid=''",
            "watchdog_pid=''",
            "cancel_monitor_pid=''",
            "fail() {",
            "  if [ -n \"${watchdog_pid:-}\" ]; then kill \"$watchdog_pid\" 2>/dev/null || true; fi",
            "  printf '%s\\n' FAILED > " + status_file,
            "  printf '%s\\n' \"$2\" >> " + error_file,
            "  exit \"$1\"",
            "}",
            "cancelled() {",
            "  if [ -n \"${child_pid:-}\" ]; then kill -TERM \"$child_pid\" 2>/dev/null || true; fi",
            "  if [ -n \"${child_pid:-}\" ]; then sleep 2; kill -KILL \"$child_pid\" 2>/dev/null || true; fi",
            "  if [ -n \"${watchdog_pid:-}\" ]; then kill \"$watchdog_pid\" 2>/dev/null || true; fi",
            "  if [ -n \"${cancel_monitor_pid:-}\" ]; then kill \"$cancel_monitor_pid\" 2>/dev/null || true; fi",
            "  printf '%s\\n' CANCELLED > " + status_file,
            "  printf '%s\\n' 'remote query cancelled' >> " + error_file,
            "  nohup sh -c "
            + shlex.quote("sleep 10; rm -rf -- " + cleanup_dir)
            + " >/dev/null 2>&1 < /dev/null &",
            "  exit 143",
            "}",
            "trap cancelled TERM INT HUP",
            "printf '%s\\n' \"$$\" > " + pid_file,
            "printf '%s\\n' RUNNING > " + status_file,
            "nohup sh -c "
            + shlex.quote("sleep " + str(ttl) + "; rm -rf -- " + cleanup_dir)
            + " >/dev/null 2>&1 < /dev/null &",
            "source " + source + " >/dev/null 2>&1 || fail 20 'environment script failed'",
            "kinit -kt " + keytab + " " + principal + " >> " + error_file + " 2>&1 || fail 21 'kinit failed'",
            "printf '%s\\n' " + shlex.quote(BEGIN_MARKER) + " > " + output_file,
            command + " &",
            "child_pid=$!",
            "( sleep "
            + str(query_timeout)
            + "; if kill -0 \"$child_pid\" 2>/dev/null; then kill -TERM \"$child_pid\" 2>/dev/null || true; sleep 30; if kill -0 \"$child_pid\" 2>/dev/null; then kill -KILL \"$child_pid\" 2>/dev/null || true; printf '%s\\n' 'remote query timeout' >> "
            + error_file
            + "; fi; fi ) >/dev/null 2>&1 &",
            "watchdog_pid=$!",
            "( while kill -0 \"$child_pid\" 2>/dev/null; do if [ -f "
            + cancel_file
            + " ]; then kill -TERM \"$$\" 2>/dev/null || true; break; fi; sleep 1; done ) >/dev/null 2>&1 &",
            "cancel_monitor_pid=$!",
            "wait \"$child_pid\"",
            "status=$?",
            "child_pid=''",
            "if [ -n \"${watchdog_pid:-}\" ]; then kill \"$watchdog_pid\" 2>/dev/null || true; fi",
            "if [ -n \"${cancel_monitor_pid:-}\" ]; then kill \"$cancel_monitor_pid\" 2>/dev/null || true; fi",
            "if [ \"$status\" -eq 0 ]; then",
            "  printf '%s\\n' " + shlex.quote(END_MARKER) + " >> " + output_file,
            "  printf '%s\\n' SUCCEEDED > " + status_file,
            "else",
            "  printf '%s\\n' FAILED > " + status_file,
            "fi",
            "chmod 644 " + output_file + " " + status_file + " " + error_file + " " + pid_file + " 2>/dev/null || true",
            "exit \"$status\"",
        ]
    )


def build_query_job_launch_command(config: ConnectionConfig, script: str) -> str:
    """Launch the query script independently of the SSH request channel."""
    detached = (
        "nohup /bin/bash -c "
        + shlex.quote(script)
        + " >/dev/null 2>&1 < /dev/null & echo $!"
    )
    if config.run_as_user:
        return "su - " + shlex.quote(config.run_as_user) + " -c " + shlex.quote(detached)
    return detached


def build_query_job_cancel_command(config: ConnectionConfig, remote_pid_path: str) -> str:
    """Build a best-effort remote process termination command."""
    pid_file = shlex.quote(remote_pid_path)
    command = " ".join(
        [
            "pid=$(cat",
            pid_file,
            "2>/dev/null || true);",
            "if [ -n \"$pid\" ]; then",
            "kill -TERM -- \"$pid\" 2>/dev/null || true;",
            "sleep 5;",
            "kill -KILL -- \"$pid\" 2>/dev/null || true;",
            "fi",
        ]
    )
    if config.run_as_user:
        return "su - " + shlex.quote(config.run_as_user) + " -c " + shlex.quote(command)
    return command


def build_query_job_cleanup_command(config: ConnectionConfig, remote_dir: str) -> str:
    command = "rm -rf -- " + shlex.quote(remote_dir)
    if config.run_as_user:
        return "su - " + shlex.quote(config.run_as_user) + " -c " + shlex.quote(command)
    return command


def build_export_script(
    config: ConnectionConfig,
    remote_sql_path: str,
    remote_output_path: str,
    remote_log_path: str,
    remote_dir: str = "",
    ttl_seconds: int = 1800,
) -> str:
    """Build a remote file export command without buffering rows in Python."""
    source = shlex.quote(config.env_script)
    keytab = shlex.quote(config.keytab)
    principal = shlex.quote(config.principal)
    beeline = shlex.quote(config.beeline_path)
    alias = shlex.quote(config.beeline_connection)
    sql_file = shlex.quote(remote_sql_path)
    output_file = shlex.quote(remote_output_path)
    log_file = shlex.quote(remote_log_path)
    export_dir = shlex.quote(remote_dir or str(Path(remote_output_path).parent))
    ttl = max(300, int(ttl_seconds))
    command = (
        beeline
        + " "
        + alias
        + " --showHeader=true --silent=true --outputformat=csv2 -f "
        + sql_file
        + " > "
        + output_file
        + " 2> "
        + log_file
    )
    script = "\n".join(
        [
            "#!/bin/bash",
            "export TERM=dumb",
            "nohup sh -c "
            + shlex.quote("sleep " + str(ttl) + "; rm -rf -- " + export_dir)
            + " >/dev/null 2>&1 < /dev/null &",
            "source " + source + " >/dev/null 2>&1 || exit 20",
            "kinit -kt " + keytab + " " + principal + " || exit 21",
            command,
            "status=$?",
            "chmod 644 " + output_file + " " + log_file + " 2>/dev/null || true",
            "if [ \"$status\" -ne 0 ]; then cat " + log_file + " >&2 2>/dev/null || true; fi",
            "exit \"$status\"",
        ]
    )
    if config.run_as_user:
        return "su - " + shlex.quote(config.run_as_user) + " -c " + shlex.quote(script)
    return script


def build_export_job_script(
    config: ConnectionConfig,
    remote_sql_path: str,
    remote_output_path: str,
    remote_log_path: str,
    remote_status_path: str,
    remote_pid_path: str,
    remote_cancel_path: str,
    remote_dir: str = "",
    ttl_seconds: int = 2 * 24 * 60 * 60,
    timeout_seconds: int = 7200,
) -> str:
    """Build a detached CSV export job with durable status files."""
    source = shlex.quote(config.env_script)
    keytab = shlex.quote(config.keytab)
    principal = shlex.quote(config.principal)
    beeline = shlex.quote(config.beeline_path)
    alias = shlex.quote(config.beeline_connection)
    sql_file = shlex.quote(remote_sql_path)
    output_file = shlex.quote(remote_output_path)
    log_file = shlex.quote(remote_log_path)
    status_file = shlex.quote(remote_status_path)
    pid_file = shlex.quote(remote_pid_path)
    cancel_file = shlex.quote(remote_cancel_path)
    cleanup_dir = shlex.quote(remote_dir or str(Path(remote_output_path).parent))
    ttl = max(300, int(ttl_seconds))
    export_timeout = max(1, int(timeout_seconds))
    command = (
        beeline
        + " "
        + alias
        + " --showHeader=true --silent=true --outputformat=csv2 -f "
        + sql_file
        + " > "
        + output_file
        + " 2> "
        + log_file
    )
    script = "\n".join(
        [
            "#!/bin/bash",
            "export TERM=dumb",
            "umask 022",
            "child_pid=''",
            "watchdog_pid=''",
            "cancel_monitor_pid=''",
            "fail() {",
            "  if [ -n \"${watchdog_pid:-}\" ]; then kill \"$watchdog_pid\" 2>/dev/null || true; fi",
            "  if [ -n \"${cancel_monitor_pid:-}\" ]; then kill \"$cancel_monitor_pid\" 2>/dev/null || true; fi",
            "  printf '%s\\n' FAILED > " + status_file,
            "  printf '%s\\n' \"$2\" >> " + log_file,
            "  exit \"$1\"",
            "}",
            "cancelled() {",
            "  if [ -n \"${child_pid:-}\" ]; then kill -TERM \"$child_pid\" 2>/dev/null || true; fi",
            "  if [ -n \"${child_pid:-}\" ]; then sleep 2; kill -KILL \"$child_pid\" 2>/dev/null || true; fi",
            "  if [ -n \"${watchdog_pid:-}\" ]; then kill \"$watchdog_pid\" 2>/dev/null || true; fi",
            "  if [ -n \"${cancel_monitor_pid:-}\" ]; then kill \"$cancel_monitor_pid\" 2>/dev/null || true; fi",
            "  printf '%s\\n' CANCELLED > " + status_file,
            "  printf '%s\\n' 'remote export cancelled' >> " + log_file,
            "  exit 143",
            "}",
            "trap cancelled TERM INT HUP",
            "printf '%s\\n' \"$$\" > " + pid_file,
            "printf '%s\\n' RUNNING > " + status_file,
            "nohup sh -c "
            + shlex.quote("sleep " + str(ttl) + "; rm -rf -- " + cleanup_dir)
            + " >/dev/null 2>&1 < /dev/null &",
            "source " + source + " >/dev/null 2>&1 || fail 20 'environment script failed'",
            "kinit -kt " + keytab + " " + principal + " >> " + log_file + " 2>&1 || fail 21 'kinit failed'",
            command + " &",
            "child_pid=$!",
            "( sleep "
            + str(export_timeout)
            + "; if kill -0 \"$child_pid\" 2>/dev/null; then kill -TERM \"$child_pid\" 2>/dev/null || true; sleep 30; if kill -0 \"$child_pid\" 2>/dev/null; then kill -KILL \"$child_pid\" 2>/dev/null || true; fi; printf '%s\\n' 'remote export timeout' >> "
            + log_file
            + "; fi; fi ) >/dev/null 2>&1 &",
            "watchdog_pid=$!",
            "( while kill -0 \"$child_pid\" 2>/dev/null; do if [ -f "
            + cancel_file
            + " ]; then kill -TERM \"$$\" 2>/dev/null || true; break; fi; sleep 1; done ) >/dev/null 2>&1 &",
            "cancel_monitor_pid=$!",
            "wait \"$child_pid\"",
            "status=$?",
            "child_pid=''",
            "if [ -n \"${watchdog_pid:-}\" ]; then kill \"$watchdog_pid\" 2>/dev/null || true; fi",
            "if [ -n \"${cancel_monitor_pid:-}\" ]; then kill \"$cancel_monitor_pid\" 2>/dev/null || true; fi",
            "if [ \"$status\" -eq 0 ]; then",
            "  chmod 644 " + output_file + " " + log_file + " 2>/dev/null || true",
            "  printf '%s\\n' SUCCEEDED > " + status_file,
            "else",
            "  printf '%s\\n' FAILED > " + status_file,
            "fi",
            "exit \"$status\"",
        ]
    )
    if config.run_as_user:
        return "su - " + shlex.quote(config.run_as_user) + " -c " + shlex.quote(script)
    return script


def _bounded_text(
    lines: Iterable[str],
    max_output_bytes: int,
    cancel_event: Event | None = None,
) -> str:
    chunks: list[str] = []
    total = 0
    for line in lines:
        if cancel_event is not None and cancel_event.is_set():
            raise CarbonLensError("Carbon query cancelled")
        total += len(line.encode("utf-8", errors="replace"))
        if total > max_output_bytes:
            raise CarbonLensError(f"Carbon output exceeds the {max_output_bytes}-byte limit")
        chunks.append(line)
    return "".join(chunks)


def parse_dsv(output: str, max_rows: int) -> tuple[list[str], list[list[str]], bool]:
    try:
        begin = output.index(BEGIN_MARKER) + len(BEGIN_MARKER)
        end = output.index(END_MARKER, begin)
    except ValueError as exc:
        raise CarbonLensError("fiber-beeline output does not contain valid result markers") from exc

    payload = output[begin:end].strip("\r\n ")
    if not payload:
        return [], [], False
    rows = [row for row in csv.reader(io.StringIO(payload), delimiter="|") if any(cell.strip() for cell in row)]
    if not rows:
        return [], [], False
    columns = rows[0]
    data = rows[1:]
    truncated = len(data) > max_rows
    data = data[:max_rows]
    for row in data:
        if len(row) != len(columns):
            raise CarbonLensError("fiber-beeline returned rows with a different column count")
    return columns, data, truncated
