# Carbon-Lens Optimization Closure

## 1. Conclusion

**PARTIAL** — local acceptance is complete; live Carbon/SSH/Kerberos/SFTP
acceptance is `UNVERIFIED` because no permitted production-like environment was
available.

## 2. Change scope

- Hardened Windows process-existence checks.
- Preserved duplicate, empty, quoted, Unicode, and literal `NULL` values.
- Added read-only single-statement validation and CTE support.
- Added explicit result limits, unknown-total metadata, type metadata, and
  duplicate-column mappings.
- Moved completed asynchronous query payloads out of SQLite into atomic result
  files; SQLite retains task metadata and summaries.
- Made remote terminal state authoritative during cancel races.
- Made CSV export truncation explicit and download failures retryable without a
  second SQL execution.
- Added reproducible tests, benchmark tooling, and design documentation.

Existing twelve MCP tool names, configuration environment variables,
read-only behavior, and synchronous query/export entry points remain
backward-compatible.

## 3. P0/P1 closure matrix

| Item | Reproduction | Fix | Verification | State |
|---|---|---|---|---|
| Duplicate/empty columns | DSV header with repeated and empty names | deterministic unique keys plus original mapping | `test_parse_dsv_preserves_empty_values_and_all_duplicate_columns` | PASS |
| Unknown totals/truncation | capped parser/export | returned/total/has_more/truncated metadata; CSV cap is unknown | parser and export tests | PASS |
| Malformed rows | row width differs from header | validate every parsed row before slicing | DSV/CSV regression coverage | PASS |
| Windows invalid PID | invalid large PID hung pytest | bounded Windows `OpenProcess` query | lifecycle suite | PASS |
| Query cancel/success race | cancel requested after remote success | remote terminal status wins | lifecycle code path and mock job | PASS |
| SQLite large result | full result JSON in task row | atomic result file plus summary metadata | `tests/test_jobs.py` inspects SQLite | PASS |
| Export download retry | SFTP failure after remote success | keep `downloading`/failed download state | existing export manager path | PASS |
| Real Carbon behavior | no permitted cluster | not claimed | live test plan below | UNVERIFIED |

## 4. Test results

```text
python -m pytest -q        # 21 passed
python -m compileall -q src tests
```

The initial suite hung on the Windows PID regression and was not accepted as
green. No test was removed or weakened to obtain the current result.

## 5. Performance report

Use `scripts/benchmark.py` with identical row sizes and repeat count for a
local before/after parser comparison. The benchmark is synthetic and does not
claim remote performance. Large query rows are bounded by `max_output_bytes`,
and asynchronous query payloads are no longer duplicated in SQLite.

## 6. Future write capability

The reusable internal transport/job boundary and the not-yet-exposed
`carbon_execute`/`carbon_execute_start` design are documented in
`docs/future-write-capability.md`. No write SQL is enabled.

## 7. Real-environment acceptance

Not executed. A permitted test table and credentials are required to verify
schema lookup, bounded query, long-query polling/cancel, SSH reconnect,
complete CSV download, concurrent jobs, and comparison with native Beeline
output. Production data was not changed.

## 8. Git and handoff

The final response must report `git status --short`, current branch, and the
last commit. The task specification file supplied in the worktree remains
untracked and was not modified by the implementation.
