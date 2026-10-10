# Managed write capability boundary

The twelve existing query/describe/export MCP tools, their names, configuration
variables, and behavior remain compatible. This release additionally exposes
managed write tools, but the write workspace is disabled by default.

The supported write subset is `CREATE TABLE <workspace>.<new_table> AS SELECT`,
`INSERT INTO`, `INSERT OVERWRITE TABLE`, and `DROP TABLE` for tables registered
by CarbonLens. `UPDATE`, `DELETE`, `MERGE`, `ALTER`, `TRUNCATE`, external
locations, multi-statement SQL, and arbitrary shell execution remain rejected.

## Reused transport and lifecycle

- `ConnectionConfig` and `connect()` provide the SSH/Kerberos transport.
- The remote job scripts persist status, PID, error, cancellation, and TTL
  files.
- SQLite job metadata provides restart-safe task identity and registration.
- `build_query_job_launch_command()` separates detached execution from the MCP
  request channel.

These capabilities are used only after server-side policy, workspace
configuration, managed-table ownership, idempotency, and lifecycle checks pass.

## Execution contract

`carbon_execute` and `carbon_execute_start` accept one approved write statement
and return command metadata rather than a query row set:

```json
{
  "execution_id": "cx_...",
  "operation": "create_table_as_select",
  "status": "succeeded",
  "connection": "...",
  "target_table": "carbon_lens_workspace.cl_tmp_...",
  "affected_row_count": null,
  "affected_row_count_known": false,
  "elapsed_ms": 12345,
  "error_kind": null
}
```

The execution manager reuses the queued → starting → running → terminal
lifecycle, persists a submission/idempotency key before remote submission, and
never automatically resubmits after timeout or disconnect.

`carbon_workspace_create_start` generates a unique target and creates a
reservation before remote CTAS. `carbon_workspace_list` exposes only registry
records; INSERT and DROP require an `active` record and remote object
verification. CTAS becomes active only after post-create verification; DROP
becomes dropped only after post-drop verification. Any unknown result requires
reconciliation of the original execution ID and never triggers a compensating
write.
