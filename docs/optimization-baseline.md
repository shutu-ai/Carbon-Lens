# Carbon-Lens optimization baseline

## Scope

This baseline records the local implementation available before the current
optimization pass. No live Carbon cluster or production credentials are used.
The authoritative behavior is the current worktree and the mock SSH/Beeline
tests.

## Reproduced issues

| Area | Evidence before the pass | Risk |
|---|---|---|
| Windows lifecycle | `os.kill(pid, 0)` hung the invalid large-PID test | MCP shutdown/health thread could stall |
| Result columns | `dict(zip(columns, row))` overwrote duplicate names | Silent data loss for `SELECT a.id, b.id` |
| Empty result cells | all-empty DSV rows were filtered out | Empty strings could disappear |
| Limits | synchronous results exposed only `row_count`/`truncated` | Unknown totals could be mistaken for complete data |
| Query jobs | complete result JSON was stored in SQLite | Large status/list database cost and serialization |
| Cancel races | local `cancel_requested` could win over remote success | False cancellation |
| CSV exports | `truncated` was always `false` | A capped export could look complete |

## Baseline commands

```text
python -m pytest -q
python -m compileall -q src tests
```

Before the pass, the suite reached 12 tests and then hung in
`test_pid_exists_for_current_process`; therefore that run was not a passing
baseline. After the pass, the expanded suite completes with 21 passed tests.

## Measurement method

Run `python scripts/benchmark.py --rows 100,1000,10000 --repeat 5` from the
repository root. The harness uses deterministic in-memory DSV/CSV fixtures and
reports median, maximum, and peak Python memory for the legacy mapping path and
the corrected path. It does not claim SSH or Carbon cluster performance.

The production-sensitive measures that still require a permitted test cluster
are SSH authentication, Kerberos initialization, Beeline execution, remote
SFTP throughput, and remote filesystem cleanup. These remain `UNVERIFIED`.
