# Future write capability boundary

The current public surface remains read-only. The twelve existing MCP tools,
their names, configuration variables, and query/export behavior are retained.
No `CREATE`, `INSERT`, `UPDATE`, `DELETE`, `ALTER`, or `DROP` tool is exposed in
this release.

## Already reusable internally

- `ConnectionConfig` and `connect()` provide the SSH/Kerberos transport.
- The remote job scripts already persist status, PID, error, cancellation, and
  TTL files.
- SQLite job metadata and atomic local result files provide restart-safe task
  identity and result publication.
- `build_query_job_launch_command()` separates a detached remote execution from
  the MCP request channel.

These are transport and lifecycle capabilities, not permission to execute
writes.

## Future adapter boundary

A future `carbon_execute` adapter should accept a typed operation request and
return a command result such as:

```text
status, affected_row_count, elapsed_ms, error_kind, remote_log_reference
```

It must not assume `columns + rows` for command statements. A future
`carbon_execute_start` should reuse the existing queued → starting → running →
terminal lifecycle, persist a submission/idempotency key before remote
submission, and never automatically resubmit after timeout or disconnect.

`CREATE TABLE AS SELECT`, `INSERT INTO`, `INSERT OVERWRITE`, and intermediate
table cleanup need explicit operation types and server-side policy decisions.
They are deliberately not implemented or enabled here. Any retry must first
reconcile the original remote submission and its durable status.
