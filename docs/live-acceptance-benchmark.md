# Live acceptance benchmark

Date: 2026-10-10  
Connection: `carbon-prod`  
SQL: `SELECT 1 AS acceptance_value`  
Scope: bounded smoke only; this is not a business-data SLA.

## Recorded measurements

| Run | Mode | Repetitions | Result |
| --- | --- | ---: | --- |
| `live-20261010-193443-f3704705` | async query | 1 | succeeded, 1 row, 9.134 s remote elapsed |
| direct live smoke | synchronous query | 1 | succeeded, 1 row, about 10.3 s |
| prior authorized sample | sync query | 1 | `LIMIT 100`, about 12.2 s |
| prior authorized sample | async query | 1 | `LIMIT 100`, 20.521 s, 100 rows |
| `live-20261010-195940-ec756bff` | constant query benchmark | 3 | median 9.499 s, P95 9.499 s, max 10.904 s |
| `live-20261010-195940-ec756bff` | async CSV export | 1 | succeeded, 1 row, 10.933 s remote elapsed |
| `live-20261010-195940-ec756bff` | async JSON export | 1 | succeeded, 1 row, 11.541 s remote elapsed |

These are observations, not a baseline comparison. No concurrency, cache
control, representative table scan, SLA, or native-Beeline comparison was
provided, so C6 remains `UNVERIFIED`.

## Reproduce

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
.\.venv\Scripts\python.exe scripts\live_acceptance.py `
  --mode benchmark `
  --connection carbon-prod `
  --repetitions 5
```

The runner reports median, P95, maximum, and individual wall-clock samples in
the ignored `.acceptance-data/` evidence directory. It issues only the bounded
constant query and stores metadata rather than result rows.
