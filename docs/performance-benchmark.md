# Carbon-Lens performance benchmark

## Reproducible local benchmark

```text
python scripts/benchmark.py --rows 100,1000,10000 --repeat 5
```

The benchmark uses the same bounded DSV parser and result mapping used by the
service. It compares the old `dict(zip(columns, row))` behavior with the
current duplicate-safe mapping, and measures streamed CSV-to-JSON conversion.
Each case uses deterministic UTF-8 data containing commas, quotes, pipes,
Unicode, empty strings, and the literal text `NULL`; values are never coerced
to guessed numeric or NULL types.

## Reported metrics

- median and maximum elapsed milliseconds;
- peak Python allocation from `tracemalloc`;
- output row count and JSON byte size;
- requested row cap and whether parsing detected an additional row.

The benchmark intentionally does not use a real Carbon connection. It cannot
prove network latency, Kerberos cost, Beeline startup cost, remote scan size,
or SFTP throughput. Those measurements require an approved Carbon test table
and must be repeated with identical connection, SQL, time filter, limit, and
sample count before/after optimization.

## Recorded local run

Environment: Windows, Python 3.14.7, five repetitions, 2026-10-10.

| requested rows | legacy median ms | corrected median ms | legacy peak KiB | corrected peak KiB |
|---:|---:|---:|---:|---:|
| 100 | 0.908 | 2.398 | 67.6 | 84.6 |
| 1,000 | 10.123 | 25.854 | 577.2 | 980.3 |
| 10,000 | 109.139 | 266.042 | 5,792.3 | 10,040.2 |

The corrected path is intentionally more expensive in this synthetic case
because it retains every duplicate field instead of silently overwriting one.
These numbers are a correctness trade-off measurement, not a claimed speedup;
the optimization is accepted for data integrity and bounded persistence, while
remote performance remains `UNVERIFIED`.

## Interpretation rules

`returned_row_count` is the number materialized locally. `total_row_count` is
unknown unless returned by the SQL itself. A CSV export that reaches its
effective cap reports `truncated = "unknown"`; it is not presented as a
complete source extract.
