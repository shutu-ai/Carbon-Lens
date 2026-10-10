# CarbonLens

CarbonLens is a Python MCP server for bounded, read-only CarbonData analysis
and CSV/JSON export through SSH and `fiber-beeline`.

The first version intentionally exposes no arbitrary shell tool and no write
SQL tool. Credentials stay in the server-side JSON configuration.

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
`carbon_export_status`, `carbon_export_list`, and `carbon_export_cancel`.

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
Use `carbon_query_list` and
`carbon_query_cancel` to manage jobs. Task metadata is stored in the local
`.carbon-lens-jobs.sqlite3` database, while the SQL runs in a detached job on
the Carbon host. A completed result is retained locally for 30 days; the remote job
directory has a two-day safety TTL. Tasks can be inspected after an MCP restart
with the same `query_id`.
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

## Validate locally

```bash
python -m pytest
python -m compileall -q src tests
```

The tests do not contact a live Carbon cluster; they validate command
construction, result parsing, and read-only guardrails.
