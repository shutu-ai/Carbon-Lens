# Managed write capability boundary

The existing query/describe/export MCP tools, their names, configuration
variables, and behavior remain compatible. This release additionally exposes
managed write tools and MCP-only workspace schema preparation, but the write
workspace is disabled by default.

The supported write subset is `CREATE TABLE <workspace>.<new_table> AS SELECT`,
`INSERT INTO`, `INSERT OVERWRITE TABLE`, and `DROP TABLE` for tables registered
by CarbonLens. `UPDATE`, `DELETE`, `MERGE`, `ALTER`, `TRUNCATE`, external
locations, multi-statement SQL, and arbitrary shell execution remain rejected.

Workspace schema creation is a separate, tightly scoped MCP operation. The
status tool checks only the configured schema. The create tool accepts no raw
SQL or schema argument, is plan-only unless `confirm=true`, and generates only
`CREATE DATABASE IF NOT EXISTS <configured_schema>`. Table-write enablement is
independent; CTAS/INSERT/DROP remain blocked until `write_workspace.enabled`
is true.

## Reused transport and lifecycle

- `ConnectionConfig` and `connect()` provide the SSH/Kerberos transport.
- The remote job scripts persist status, PID, error, cancellation, and TTL
  files.
- SQLite job metadata provides restart-safe task identity and registration.
- `build_query_job_launch_command()` separates detached execution from the MCP
  request channel.

These capabilities are used only after server-side policy, workspace
configuration, managed-table ownership, idempotency, and lifecycle checks pass.

## Ownership identity and local atomicity

Every CarbonLens CTAS generates an unpredictable ownership token and injects it
as the `carbon_lens_ownership_token` Carbon `TBLPROPERTIES` value. The token is
stored with the table reservation and checked with `DESC FORMATTED` before CTAS
activation and before later INSERT operations. A missing metadata response,
missing token, or mismatch returns `OWNERSHIP_UNVERIFIED`; a matching name or
`cl_tmp_` prefix alone is never sufficient. DROP is accepted only after the
same pre-drop identity check and a post-drop absence check.

The execution row and CTAS reservation are committed in one SQLite transaction
before remote submission. A local transaction failure rolls back both records
and never starts a remote job. If an INSERT becomes uncertain, its managed
target enters `insert_unknown` and further writes are blocked until manual
reconciliation; no compensating or automatic retry is issued.

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
