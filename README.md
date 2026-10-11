# CarbonLens

CarbonLens is a Python MCP server for bounded CarbonData analysis, CSV/JSON
export, and opt-in managed intermediate-table execution through SSH and
`fiber-beeline`.

Arbitrary shell access is never exposed. Write operations are disabled by
default and, when explicitly enabled, are restricted to CarbonLens-registered
workspace tables. Credentials stay in the server-side JSON configuration.

## Install

```bash
cd carbon-lens
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

On Windows, replace `source .venv/bin/activate` with
`.venv\\Scripts\\Activate.ps1`.

## Configure

Copy `config.example.json` to `config.json`, then set:

```bash
export CARBON_LENS_CONFIG=/absolute/path/config.json
```

The SSH target must have the Carbon client, `bigdata_env`, `kinit`, keytab, and
`fiber-beeline`. Password authentication does not require `private_key`.
`known_hosts` is created automatically after the first successful SSH login
(TOFU). Later logins validate the saved host key and reject a changed key. For
high-security environments, set `host_key_sha256` so the first key must match
the expected fingerprint before it is saved. For the `paas → su - root` flow, set
`run_as_user` to `root`. If `su` requires a password, set
`CARBON_LENS_ROOT_PASSWORD`; otherwise leave it unset.

The example uses environment variables for passwords. A local `config.json`
may instead contain `ssh_config.password` and `root_password`; keep that file
private because it contains credentials and is ignored by Git.

## Run as a stdio MCP server

```bash
carbon-lens
```

From a source checkout, the equivalent command is:

```bash
python run_mcp.py
```

Configure Codex to launch that command as a stdio MCP server. Keep
`CARBON_LENS_CONFIG` in the MCP process environment. The available tools are
`carbon_list_connections`, `carbon_query`, `carbon_query_start`,
`carbon_query_status`, `carbon_query_list`, `carbon_query_cancel`,
`carbon_describe`, `carbon_export`, `carbon_export_start`,
`carbon_export_status`, `carbon_export_list`, `carbon_export_cancel`,
`carbon_execute`, `carbon_execute_start`, `carbon_execute_status`,
`carbon_execute_list`, `carbon_execute_cancel`, `carbon_workspace_list`,
`carbon_workspace_create_start`, `carbon_workspace_create`,
`carbon_workspace_schema_status`, and `carbon_workspace_schema_create`.

Query and export commands use a 10-minute default timeout. Each call may set
`timeout_seconds` up to 7200 seconds (2 hours); the configured connection value
in `timeout_seconds` controls the default when the tool argument is omitted.
The configured `max_rows` controls the effective row cap; it can be set up to
1,000,000 rows.
The default `max_output_bytes` is 100 MiB (104,857,600 bytes), and it can be
configured up to 512 MiB.

For queries that may run longer than the MCP client's request limit, call
`carbon_query_start` with the same `connection`, `sql`, `limit`, and
`timeout_seconds` arguments. It returns a `query_id` immediately. Poll
`carbon_query_status` with that ID until `status` is `succeeded`, `failed`, or
`cancelled`;
the completed result is returned under `result`. Status also reports the SQL,
effective limits, elapsed time, returned row count, and the first three rows.
`total_row_count` remains null unless the original SQL itself returns a total;
to expose one, alias the value as `total_row_count` or
`__carbon_lens_total_row_count`. CarbonLens does not issue an extra `COUNT(*)`.
Result values remain source text when no schema type metadata is available
(`type_info_unavailable = true`). Duplicate and empty columns are preserved
with deterministic keys such as `id`, `id__2`, and `column_3`; the response
also retains `original_columns` and `column_name_map`. Results report
`returned_row_count`, `has_more`, and `truncated`; a CSV export that reaches
its cap reports `truncated = "unknown"` because the cap does not prove source
exhaustion.
Use `carbon_query_list` and
`carbon_query_cancel` to manage jobs. Task metadata is stored in the local
`.carbon-lens-jobs.sqlite3` database, while the SQL runs in a detached job on
the Carbon host. A completed result is retained locally for 30 days; the remote job
directory has a two-day safety TTL. Tasks can be inspected after an MCP restart
with the same `query_id`. The SQLite database stores task metadata and a small
result summary; completed query rows are written to an adjacent atomic JSON
result file so list/status metadata does not repeatedly serialize a large
result into SQLite.
Set `CARBON_LENS_JOB_DB` to choose another local SQLite path; otherwise the
database is created beside the configured `config.json`.

CSV exports run `fiber-beeline` on the remote host and redirect its output to
a remote temporary CSV file. CarbonLens downloads that file over SFTP and
removes the remote SQL, CSV, and log files afterward, including files created
under `su - root`. Each export uses an isolated temporary directory with a
configurable 30-minute TTL; stale export directories are cleaned before later
exports. JSON export remains the bounded in-memory path.

For a long-running file export, call `carbon_export_start`. The remote Carbon
host generates the CSV result in a temporary directory. When
`carbon_export_status` observes that the remote job has completed, CarbonLens
downloads the file through SFTP into the configured `export_dir` and only then
reports `status = succeeded`. A failed download remains retryable through the
same status call and does not rerun the SQL. Use `carbon_export_list` to view
export history and `carbon_export_cancel` to request remote cancellation.

## Managed intermediate tables

Write execution is fail-closed unless a connection contains an explicit
configuration such as:

```json
"write_workspace": {
  "enabled": false,
  "schema": "carbon_lens",
  "table_prefix": "cl_tmp_"
}
```

Only one SQL statement from the supported CTAS/INSERT/DROP subset is accepted.
The service rejects writes to original tables, unknown workspace tables,
external locations, multi-statement input, and unsupported SQL before opening a
remote execution session. `carbon_workspace_create_start` generates a fresh
workspace target for a read-only `SELECT`; `carbon_execute_start` is used for
approved `INSERT INTO`, `INSERT OVERWRITE TABLE`, and `DROP TABLE` operations.
For CTAS, CarbonLens injects a random ownership token into a Carbon
`TBLPROPERTIES` value and verifies it through `DESC FORMATTED` before the table
becomes active. A missing or mismatched token is `OWNERSHIP_UNVERIFIED` and is
never treated as a successful ownership check.

Each operation returns an `execution_id` and is persisted in the same SQLite
job database as the existing background tasks. An idempotency key can be
reused to observe the original execution; reusing it with a different request
returns `IDEMPOTENCY_CONFLICT`. CTAS execution metadata and its table
reservation are committed in one SQLite transaction before remote submission.
If remote completion cannot be proven, the execution becomes `unknown` /
`EXECUTION_UNKNOWN`, an affected managed target is quarantined when necessary,
and CarbonLens never resubmits the write automatically. Use
`carbon_execute_status` and `carbon_workspace_list` to reconcile the original
task and managed-table registration.

Schema preparation is also MCP-only. `carbon_workspace_schema_status` checks
the configured schema with a read-only `SHOW DATABASES`; it never accepts an
arbitrary schema name. `carbon_workspace_schema_create` is plan-only by
default and accepts no SQL; only `confirm=true` can execute the generated
`CREATE DATABASE IF NOT EXISTS <configured_schema>`. It does not create tables
or modify any other schema; the table-write switch remains independent.

## Validate locally

```bash
python -m pytest
python -m compileall -q src tests
```

The tests do not contact a live Carbon cluster; they validate command
construction, result parsing, and read-only guardrails.
