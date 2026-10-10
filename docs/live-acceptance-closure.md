# Live acceptance closure

Date: 2026-10-10  
Baseline SHA: `8471570cc8e78a3d1ebdd841501f212242639eb7`
Connection tested: `carbon-prod`

## Closure matrix

| Requirement | Status | Evidence / gap |
| --- | --- | --- |
| C0 baseline, config, smoke | PASS | Preflight exit 0; live `SELECT 1` returned one untruncated row. |
| C1 async CSV/JSON end-to-end | PASS | `.acceptance-data/live-20261010-195940-ec756bff/results.json`; both detached formats reached `succeeded`, downloaded over SFTP, and passed local file validation. |
| C2 terminal/cancel/timeout/retry | PASS | Deterministic coverage now includes terminal-vs-timeout races, cancellation uncertainty, short connection recovery without SQL resubmission, SFTP retry, missing/corrupt/empty files, idempotency, and cleanup warnings. Shared live fault injection was intentionally not run. |
| C3 cross-process restart | PASS | `.acceptance-data/live-20261010-201624-d3e9386d/results.json`; query and CSV export both survived process A/B restart, were terminal `succeeded`, appeared in list results, and the export file passed validation. |
| C4 runner credibility | PASS | `restart`, exit codes 0/1/2, last-status trace, file validation, and envelope parsing are covered; 35 tests pass. |
| C5 Carbon-Lens vs native Beeline reconciliation | UNVERIFIED | No approved native expected-output file was supplied. |
| C6 live performance benchmark | UNVERIFIED | Constant-query measurements recorded; no approved baseline, SLA, or representative concurrent workload. |
| C7 tests, docs, handoff | PASS | Docs, runner, runbook, closure matrix, and local verification complete. |

## Decision

The bounded async export gate is **PASS**. Overall closure remains **PARTIAL**
only because C5 has no approved native expected-output file and C6 has no
approved performance baseline/SLA. No business-data correctness claim is made
from the authorized bounded sample.

## Reproducible evidence

- Async query PASS: `.acceptance-data/live-20261010-193443-f3704705/results.json`
- Restart PASS: `.acceptance-data/live-20261010-193523-f3ba93b0/results.json`
- Export PASS with CSV/JSON file validation: `.acceptance-data/live-20261010-195940-ec756bff/results.json`
- Restart query + export PASS: `.acceptance-data/live-20261010-201624-d3e9386d/results.json`
- Historical stale export: `ce_37f07c4ff6ac4c28b1d091e74be5a0f7`, remained `cancel_requested`; no duplicate resubmission was made.

The evidence directories are intentionally ignored and are not committed.
