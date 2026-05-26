#!/usr/bin/env python3
"""Fail when pytest-crap reports a CRAP score at or above a threshold."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coverage import CoverageData
from pytest_crap.calculator import calculate_crap


def covered_lines_for_file(coverage_file: Path, source_file: Path) -> set[int]:
    data = CoverageData(basename=str(coverage_file))
    data.read()

    source_file = source_file.resolve()
    for measured_file in data.measured_files():
        if Path(measured_file).resolve() == source_file:
            return set(data.lines(measured_file) or [])

    raise SystemExit(f"No coverage data found for {source_file}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_file", type=Path)
    parser.add_argument("--coverage-file", type=Path, default=Path(".coverage"))
    parser.add_argument("--max-crap", type=float, required=True)
    args = parser.parse_args()

    covered_lines = covered_lines_for_file(args.coverage_file, args.source_file)
    scores = calculate_crap(str(args.source_file), covered_lines)
    offenders = [score for score in scores if score.crap >= args.max_crap]

    if not offenders:
        print(f"All CRAP scores are below {args.max_crap:g} for {args.source_file}")
        return 0

    print(f"CRAP scores must be below {args.max_crap:g} for {args.source_file}")
    for score in sorted(offenders, key=lambda item: item.crap, reverse=True):
        print(
            f"{score.name}: CRAP={score.crap:.2f}, "
            f"CC={score.cc}, coverage={score.coverage_percent:.1f}%"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
