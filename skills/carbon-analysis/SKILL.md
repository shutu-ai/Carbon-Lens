---
name: carbon-analysis
description: Analyze or export CarbonData through the CarbonLens MCP tools when the user asks for Carbon table discovery, read-only SQL analysis, or bounded CSV/JSON exports.
metadata:
  short-description: Query and analyze CarbonData safely through CarbonLens
---

# CarbonData analysis workflow

Use CarbonLens MCP tools for live CarbonData access. Do not ask the user to paste
SSH passwords, private keys, keytabs, or other credentials into the conversation.

## Workflow

1. Call `carbon_list_connections` and select the named connection. Never infer a
   connection from a hostname when multiple connections are available.
2. If the table is unknown, use `carbon_query` with bounded `SHOW DATABASES`,
   `SHOW TABLES`, or `DESCRIBE` SQL. Prefer `carbon_describe` when the table name
   is already known.
3. Send exactly one read-only SQL statement per tool call. CarbonLens rejects
   multiple statements and write operations, while allowing read-only CTEs.
4. For analysis, run a bounded sample query with an explicit SQL `LIMIT` and a
   suitable tool `limit`. `carbon_query` caps returned rows at the smaller of
   the requested limit and the connection's configured `max_rows` (up to the
   one-million-row server cap); it can also
   truncate output, so inspect and report its `truncated` field.
5. For queries that may exceed the MCP client's request duration, use
   `carbon_query_start` with the same bounded SQL and poll
   `carbon_query_status` using the returned `query_id`. Continue until the
   status is `succeeded`, `failed`, or `cancelled`; do not submit the same long
   query again while the first job is still running.
6. Use `carbon_query_list` and `carbon_query_cancel` for local background job
   management. These tools do not issue additional Carbon SQL. Background
 tasks are persisted locally and run in detached remote job directories, so
   status can be recovered after an MCP restart. A successful
   status includes the SQL, elapsed time, returned row count, and a three-row
   preview. Completed local task records and saved results are retained for 30
   days. `total_row_count` is null unless the original SQL provides it;
   never add an automatic `COUNT(*)` query.
7. State the connection, table, time filters, effective row limit, returned row
   count, and whether the result was truncated before interpreting results.
   `total_row_count` is unknown unless the query itself returns an explicitly
   aliased total. Duplicate or empty result columns are preserved with
   deterministic keys such as `id`, `id__2`, and `column_3`; inspect
   `original_columns` and `column_name_map` when the mapping matters.
8. Use `carbon_export` only when the user requests an export. It supports
   `format` `csv` or `json`, plus `filename`, `limit`, and `timeout_seconds`.
   The effective row cap is the smaller of the requested `limit` and configured
   `max_rows`. For `SELECT` exports, include an explicit SQL `LIMIT` as well as
   the tool limit. Do not assume the tool limit bounds `SHOW`, `DESCRIBE`, or
   other non-`SELECT` output.
9. For large or potentially slow file exports, use `carbon_export_start` and
   poll `carbon_export_status` with the returned `export_id`. When the remote
   Carbon export finishes, the status call automatically downloads the file by
   SFTP into `export_dir`; only a completed local download is reported as
   `succeeded`. A download failure remains retryable through the same
   `export_id` and does not rerun the SQL. Use `carbon_export_list` for export
   history and `carbon_export_cancel` to request remote cancellation.
10. Explain where the exported file will be available. In stdio mode CSV is
   generated on the Carbon host and transferred by SFTP to the MCP server's
   configured `export_dir`; JSON is written there from a bounded in-memory
   query. In Streamable HTTP mode, a successful status may include
   `download_available` and a server-generated `download_url`; fetch that URL
   with the same bearer token when configured. The URL is tied to the
   `export_id`, streams the existing file, and does not rerun SQL. If no trusted
   `public_base_url` is configured, use the known MCP server origin plus
   `/exports/{export_id}/download` rather than inventing a host from request
   headers. Do not claim a server-side path is already on the user's device.
11. For JSON exports, report `truncated` when true and do not present a capped
   result as complete. CSV exports report `truncated: "unknown"` when the
   bounded result reaches the effective limit, because an exact cap cannot
   prove that no additional source rows exist. Always report the applied row
   cap and do not treat an unknown result as complete.

## Response display format

Keep the MCP result as structured data internally, then render a concise
Chinese response for the user. Do not paste the complete JSON object unless the
user asks for raw output.

### Common rules

- Start with a short result heading such as `查询完成`、`任务已提交`、`任务仍在运行`、`查询失败` or `导出完成`.
- Put the connection name, table name when known, effective row limit, elapsed time, returned row count, and `truncated` state in a compact metadata list.
- Always show the complete `query_id` in code formatting. Never replace a task ID with an ellipsis in a value the user may need to reuse.
- Show SQL in a fenced `sql` block when the query is relevant. Preserve the SQL exactly as returned or submitted.
- Render rows as a Markdown table. Use `NULL` for JSON null values, escape `|`, and replace embedded newlines with spaces.
- By default show the first 10 rows. If the user explicitly asks for a smaller bounded result, show all returned rows when that does not create an unnecessarily large response. Do not repeat a large dataset just because it is present in `result.rows`.
- Treat `returned_row_count` as the number actually returned. Treat `total_row_count: null` as `总行数：未知`; never infer a total from the preview or issue a `COUNT(*)` query.
- When `truncated` is true, say `结果已截断，当前只展示返回上限内的数据`.
- Do not expose credentials, keytabs, private paths, remote SQL file paths, or raw SSH diagnostics unless the user is troubleshooting the server.

### Tool-specific rendering

#### `carbon_list_connections`

Render a table with `name`, `transport`, `connection_mode`, `host`, and
`beeline_connection`. Do not show an empty result as a blank table; say
`没有配置 Carbon 连接`.

#### `carbon_query`

Use this order:

1. `查询完成`
2. Connection, effective limit, returned row count, elapsed time, and truncation state.
3. SQL code block.
4. Returned columns and rows as a table.

For zero rows, keep the columns and say `查询成功，但没有返回数据`.

#### `carbon_query_start`

Use `任务已提交` and show `query_id`, connection, effective limit,
effective timeout, and current status. Tell the user to call
`carbon_query_status` with that exact ID. Do not claim that the SQL has
finished when the status is `queued` or `running`.

#### `carbon_query_status`

- `queued`, `starting`, or `running`: show the ID, status, elapsed time, effective timeout,
  and a short instruction to poll again. Do not show an empty result table.
- `cancel_requested`: say `已提交取消请求，等待远程任务确认` and keep polling
  until the status becomes `cancelled` or another terminal state.
- `succeeded`: use `查询完成`, show SQL and execution metadata, then render
  `preview_rows` by default. Use `result.rows` only when the user asks for the
  returned data or the preview is insufficient.
- `failed`: use `查询失败` and show the returned error without inventing a
  retry or changing the SQL.
- `cancelled`: use `查询已取消` and show the elapsed time if available. Do not
  present rows as a successful result.

#### `carbon_query_list`

Show the total `count` and `returned_count`, followed by a compact table with
the complete `query_id`, `status`, `connection`, `created_at`, `finished_at`, `elapsed_ms`,
and `returned_row_count`. Do not put the full SQL into every row; show the SQL
only when the user asks for a specific task. Mention that the list is local
job metadata and does not issue Carbon SQL.

To inspect one task, call `carbon_query_status` with its complete
`query_id`. The response contains the original `sql`; a successful task also
contains `result.columns`, `result.rows`, `returned_row_count`, `preview_rows`,
and `truncated`. If only a shortened ID is visible, do not guess the rest;
retrieve the full ID from `carbon_query_list` or ask the user to provide it.

#### `carbon_query_cancel`

Show the task ID and resulting status. Distinguish clearly between
`cancel_requested` and `cancelled`; the former means the remote task has not
yet confirmed termination.

#### `carbon_describe`

Render the returned schema rows as a table, normally using columns such as
`col_name`, `data_type`, and `comment`. Include the connection and table name
in the heading. Do not describe a schema row as business data.

#### `carbon_export`

Use `导出完成` and show format, server-side file path, columns, row count,
elapsed time, and the applied limit/truncation state. Explain that the path is
on the MCP server and the tool does not download the file to the user's local
computer.

#### `carbon_export_start` / `carbon_export_status`

Use `carbon_export_start` for large exports and show the complete `export_id`,
connection, format, effective limit, effective timeout, and initial status.
Poll `carbon_export_status` until `succeeded`, `failed`, or `cancelled`.
While the remote file is being transferred, report `status = downloading` and
`download_status = running`. On success, show `local_path`, file size, columns,
row count, and the applied limit. If `download_status = failed`, poll the same
`export_id` again to retry the SFTP transfer; do not start another SQL job.

#### `carbon_export_list`

Show the total `count` and `returned_count`, followed by a compact table with
the complete `export_id`, status, download status, connection, format,
created_at, finished_at, local path, file size, and row count. Mention that the
list is local export metadata and does not issue Carbon SQL.

#### `carbon_export_cancel`

Show the complete `export_id` and resulting status. Distinguish
`cancel_requested` from `cancelled`; the former means the remote export has not
yet confirmed termination.

### Error format

For tool errors, use:

```text
查询失败
- 原因：<错误信息>
- 建议：<only a concrete next step supported by the error>
```

Do not turn an authentication, timeout, validation, or missing-table error
into a generic empty-result message.

## Managed intermediate-table writes

The analysis/query/export tools remain read-only. Do not route a write through
`carbon_query`, `carbon_query_start`, `carbon_export`, or arbitrary shell/SFTP
commands. When the user explicitly requests a managed intermediate table, use
only these server-side policy-checked tools:

- `carbon_workspace_create_start` or `carbon_workspace_create` for a generated
  CTAS target from a `SELECT`/`WITH ... SELECT` source.
- `carbon_execute_start` or `carbon_execute` for the supported
  `CREATE TABLE <workspace>.<generated> AS SELECT`, `INSERT INTO`,
  `INSERT OVERWRITE TABLE`, and managed `DROP TABLE` statements.
- `carbon_execute_status`, `carbon_execute_list`, and `carbon_execute_cancel`
  for lifecycle observation and cancellation requests.
- `carbon_workspace_list` to inspect only tables registered by CarbonLens.

The workspace must be explicitly enabled in configuration and must name a
dedicated schema and table prefix. Original, third-party, unregistered, or
ambiguous targets are rejected before remote submission. CTAS receives a random
`carbon_lens_ownership_token` in Carbon `TBLPROPERTIES`; CarbonLens reads it
back with `DESC FORMATTED` before activation and before later writes. A matching
name or prefix without a matching token is not ownership proof. A target is
writable only after its persisted reservation reaches `active`; a reservation
in `reserved`, `creating`, `create_unknown`, `insert_unknown`, or any failed
state is not writable.

Always provide a stable `idempotency_key` for retriable client requests. A
repeated key returns the original execution; a conflicting SQL, connection, or
target is rejected. If SSH startup, remote status, or final identity
verification is uncertain, the result is `unknown` with
`reconciliation_required`. An uncertain INSERT quarantines its managed target
as `insert_unknown`; do not write to it until reconciliation. Do not resubmit
the write or infer success from a client timeout. Cancellation is a request,
not a database rollback; poll the same execution until the remote state is
authoritative.

Use virtual schema/table names in examples and never include credentials,
keytabs, private keys, raw SSH diagnostics, or production table contents. Do
not claim real-write acceptance unless an explicitly approved dedicated
CarbonLens workspace was used; otherwise report `BLOCKED_ENV`.

## Safety

- For analysis and export, use only read-only SQL: `SELECT`, `SHOW`,
  `DESCRIBE`, `DESC`, `EXPLAIN`, or `USE`.
- For writes, use only the managed tools above. Never attempt `UPDATE`,
  `DELETE`, `ALTER`, `TRUNCATE`, `RENAME`, `CREATE OR REPLACE`, external
  `LOCATION`, multi-statement SQL, or arbitrary shell commands.
- Keep queries bounded by time filters, explicit SQL `LIMIT` for `SELECT`, tool
  row limits, and the configured timeout. Never send multiple SQL statements in
  one call, even if the first statement is read-only.
