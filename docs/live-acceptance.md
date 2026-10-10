# Carbon-Lens live acceptance report

Date: 2026-10-10
Baseline before this closure round: `ba00df8c5e7b9943e23047f0bb77ac8b96a21ce1`
Connection: `carbon-prod` (SSH + Beeline)

## Current conclusion

Overall status: **PARTIAL**.

The live connection, bounded synchronous query, asynchronous query persistence,
cross-process query recovery, and detached CSV/JSON exports pass. The export
fix keeps Beeline in the detached wrapper's foreground and validates the local
SFTP result atomically. Business reconciliation and representative performance
remain separate `UNVERIFIED` gates.

## Evidence matrix

| Gate | Status | Evidence |
| --- | --- | --- |
| Local preflight | PASS | `scripts/live_acceptance.py --mode preflight` returned exit code 0. |
| MCP/SSH/Beeline smoke | PASS | `SELECT 1 AS acceptance_value`, 1 row, `truncated=false`, about 10.3 s. |
| Async query | PASS | `.acceptance-data/live-20261010-193443-f3704705/results.json`, exit code 0. |
| Cross-process restart | PASS | `.acceptance-data/live-20261010-193523-f3ba93b0/results.json`, exit code 0. |
| Detached CSV/JSON export | PASS | `.acceptance-data/live-20261010-195940-ec756bff/results.json`; both formats succeeded and passed local file validation. |
| Synchronous CSV export control | PASS | `carbon_export`, one bounded row, readable CSV metadata returned. |
| Authorized table sample | UNVERIFIED | `sdr_nethouse.cloudphone_sales_users_all`, explicit `LIMIT 100`; no native expected output or full-count proof. |
| Live performance | UNVERIFIED | Constant-query timings are recorded, but no approved baseline/SLA exists. |
| Local regression | PASS | 28 tests passed; compileall and `git diff --check` passed. |

The authorized sample was used only with bounded read-only SQL. Its rows and
credentials are intentionally not reproduced here.

## Changes made in this closure round

- Stale running/cancel-requested query jobs now hit the local deadline instead
  of remaining immortal.
- Detached export scripts no longer receive a nested `su` wrapper and run
  Beeline in the detached wrapper's foreground with stdin bound to `/dev/null`.
- Export timeout diagnostics read only a bounded remote log prefix and never
  let oversized logs replace the original timeout result.
- The live runner now supports `restart`, real exit codes, persisted evidence,
  status traces, export file validation, and text/structured MCP envelopes.

## Reproduction

Use the repository virtual environment and force the checkout source onto
`PYTHONPATH` so an unrelated editable install cannot be tested accidentally:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode preflight
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode jobs --connection carbon-prod
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode restart --connection carbon-prod
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode exports --connection carbon-prod
```

Evidence is written under `.acceptance-data/`, which is ignored by Git.

## Remaining non-blocking gates

No async export blocker remains for the bounded live gate. Business-data
correctness still needs an approved native expected-output file, and performance
still needs an approved baseline/SLA and representative workload.
