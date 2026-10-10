# Carbon-Lens live acceptance runbook

This runbook is for an explicitly authorized CarbonData environment. Default
checks are bounded and read-only. Do not paste passwords, keytabs, private
keys, raw business rows, or remote paths into the report.

## 1. Local source and preflight

Run from the repository root. On Windows, use the project virtual environment
and force the current checkout source:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode preflight
```

On Linux:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python scripts/live_acceptance.py --mode preflight
```

The runner records a baseline SHA, connection aliases, and only credential
presence metadata. It never records credential values.

## 2. Safe live gates

```powershell
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode smoke --connection carbon-prod
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode jobs --connection carbon-prod
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode restart --connection carbon-prod
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode exports --connection carbon-prod
.\.venv\Scripts\python.exe scripts\live_acceptance.py --mode benchmark --connection carbon-prod --repetitions 5
```

`restart` starts a persisted query and CSV export in process A, closes it, then
opens process B with the same SQLite job database and checks both original IDs
via status, list, and local file validation. `exports` validates both CSV and
JSON by default; use `--format csv` or `--format json` for an isolated retry.

All requested checks pass only with exit code `0`. Exit code `1` means an
actual product or acceptance failure. Exit code `2` means BLOCKED_ENV,
UNVERIFIED, or NOT_RUN. The runner reserves exit code `3` for an internal
runner error.

Each run writes `.acceptance-data/live-<run-id>/results.json`. The evidence is
machine-readable and contains check status, job IDs, bounded traces, SQL hashes,
columns, row counts, truncation flags, and file validation metadata. It does
not persist result rows.

## 3. Authorized business-data check

Only run this after receiving the table, SQL, expected output, and retention
scope from the data owner:

```powershell
.\.venv\Scripts\python.exe scripts\live_acceptance.py `
  --mode correctness `
  --connection carbon-prod `
  --sql-file .\authorized-readonly-test.sql `
  --allow-data `
  --limit 100
```

The SQL must be one read-only statement with an explicit bound. Without a
native expected-output file, the runner reports `UNVERIFIED`; a returned sample
is not proof of full-table completeness. Use `--expected-file` only for a
canonical, approved JSON-row digest.

## 4. Failure handling

1. Save `results.json` before retrying.
2. Reuse the same query/export ID while it is queued or running; do not submit
   the same SQL repeatedly.
3. Poll until `succeeded`, `failed`, or `cancelled`. A timeout retains the last
   status and trace.
4. Treat a stuck `RUNNING` or `CANCEL_REQUESTED` export as FAIL unless the
   environment itself is unavailable.
5. Re-run local tests with the checkout source selected:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
git diff --check
```

Do not inject faults into a shared cluster. Cancellation, corrupt-file, retry,
and cleanup-failure cases belong in deterministic local tests or an isolated
environment.
