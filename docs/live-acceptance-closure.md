# Live acceptance closure

Date: 2026-10-10  
Baseline SHA: `ba00df8c5e7b9943e23047f0bb77ac8b96a21ce1`  
Connection tested: `carbon-prod`

## Closure matrix

| Requirement | Status | Evidence / gap |
| --- | --- | --- |
| C0 baseline, config, smoke | PASS | Preflight exit 0; live `SELECT 1` returned one untruncated row. |
| C1 async CSV/JSON end-to-end | PASS | `.acceptance-data/live-20261010-195940-ec756bff/results.json`; both detached formats reached `succeeded`, downloaded over SFTP, and passed local file validation. |
| C2 terminal/cancel/timeout/retry | PARTIAL | Local deterministic timeout/cancel/retry coverage passes. Shared live fault injection was intentionally not run; stale export remains a live defect. |
| C3 cross-process restart | PASS | Runner process A/B restart evidence: `live-20261010-193523-f3ba93b0`. |
| C4 runner credibility | PASS | `restart`, exit codes 0/1/2, last-status trace, file validation, and envelope parsing are covered; 28 tests pass. |
| C5 Carbon-Lens vs native Beeline reconciliation | UNVERIFIED | No approved native expected-output file was supplied. |
| C6 live performance benchmark | UNVERIFIED | Constant-query measurements recorded; no approved baseline, SLA, or representative concurrent workload. |
| C7 tests, docs, handoff | PASS | Docs, runner, runbook, closure matrix, and local verification complete. |

## Decision

The bounded live gate is **PASS**. The overall closure remains **PARTIAL** only
because C5 has no approved native expected-output file and C6 has no approved
performance baseline/SLA. No business-data correctness claim is made from the
authorized bounded sample.

## Reproducible evidence

- Async query PASS: `.acceptance-data/live-20261010-193443-f3704705/results.json`
- Restart PASS: `.acceptance-data/live-20261010-193523-f3ba93b0/results.json`
- Export PASS with CSV/JSON file validation: `.acceptance-data/live-20261010-195940-ec756bff/results.json`
- Historical stale export: `ce_37f07c4ff6ac4c28b1d091e74be5a0f7`, remained `cancel_requested`; no duplicate resubmission was made.

The evidence directories are intentionally ignored and are not committed.
