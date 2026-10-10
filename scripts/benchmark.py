"""Deterministic local benchmark for bounded Carbon-Lens result handling."""

from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carbon_lens.beeline import BEGIN_MARKER, END_MARKER, parse_dsv, rows_as_dicts


def fixture(rows: int) -> str:
    output = io.StringIO()
    writer = csv.writer(output, delimiter="|")
    output.write(f"{BEGIN_MARKER}\n")
    writer.writerow(["id", "id", "text", "empty", "literal"])
    for index in range(rows + 1):
        writer.writerow([index, index * 2, 'quote, "pipe|" 中文', "", "NULL"])
    output.write(f"{END_MARKER}\n")
    return output.getvalue()


def measure(rows: int, repeat: int) -> dict[str, object]:
    data = fixture(rows)
    legacy_times: list[float] = []
    current_times: list[float] = []
    legacy_peak = 0
    current_peak = 0
    json_bytes = 0
    parsed_rows = 0
    for _ in range(repeat):
        tracemalloc.start()
        started = time.perf_counter()
        columns, values, truncated = parse_dsv(data, rows)
        legacy = [dict(zip(columns, row)) for row in values]
        legacy_times.append((time.perf_counter() - started) * 1000)
        _, legacy_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del legacy

        tracemalloc.start()
        started = time.perf_counter()
        columns, values, truncated = parse_dsv(data, rows)
        current = rows_as_dicts(columns, values)
        encoded = json.dumps(current, ensure_ascii=False).encode("utf-8")
        current_times.append((time.perf_counter() - started) * 1000)
        _, current_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        json_bytes = len(encoded)
        parsed_rows = len(current)
    return {
        "rows_requested": rows,
        "rows_returned": parsed_rows,
        "detected_truncated": truncated,
        "legacy_median_ms": round(statistics.median(legacy_times), 3),
        "legacy_max_ms": round(max(legacy_times), 3),
        "current_median_ms": round(statistics.median(current_times), 3),
        "current_max_ms": round(max(current_times), 3),
        "legacy_peak_kib": round(legacy_peak / 1024, 1),
        "current_peak_kib": round(current_peak / 1024, 1),
        "json_bytes": json_bytes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", default="100,1000,10000")
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    results = [measure(int(value), args.repeat) for value in args.rows.split(",") if value.strip()]
    print(json.dumps({"repeat": args.repeat, "cases": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
