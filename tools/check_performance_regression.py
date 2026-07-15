#!/usr/bin/env python3
"""Fail when aggregate benchmark throughput regresses beyond the release budget."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def aggregate_throughput(rows: list[dict[str, str]], variant: str) -> float:
    selected = [row for row in rows if row.get("variant") == variant and row.get("status") == "ok"]
    if not selected:
        raise ValueError(f"no successful benchmark rows for variant {variant!r}")
    pages = sum(int(row["pages"]) for row in selected)
    duration = sum(float(row["duration_s"]) for row in selected)
    if pages <= 0 or duration <= 0:
        raise ValueError(f"invalid pages/duration for variant {variant!r}")
    return pages / duration


def regression_percent(baseline: float, candidate: float) -> float:
    if baseline <= 0 or candidate <= 0:
        raise ValueError("throughput values must be positive")
    return max(0.0, (baseline - candidate) / baseline * 100.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_csv", type=Path)
    parser.add_argument("--baseline-variant", default="baseline")
    parser.add_argument("--candidate-variant", default="current")
    parser.add_argument("--max-regression-percent", type=float, default=10.0)
    args = parser.parse_args()

    with args.results_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    baseline = aggregate_throughput(rows, args.baseline_variant)
    candidate = aggregate_throughput(rows, args.candidate_variant)
    regression = regression_percent(baseline, candidate)
    print(
        f"baseline={baseline:.3f} pages/s candidate={candidate:.3f} pages/s "
        f"regression={regression:.2f}% budget={args.max_regression_percent:.2f}%"
    )
    return 1 if regression > args.max_regression_percent else 0


if __name__ == "__main__":
    raise SystemExit(main())
