#!/usr/bin/env python3
"""Enforce CRAP scores using executable-statement coverage."""

from __future__ import annotations

import argparse
from dataclasses import replace
import sys
from pathlib import Path

from coverage import Coverage
from pytest_crap.calculator import FunctionScore, calculate_crap


def scores_for_file(coverage: Coverage, source_file: Path) -> list[FunctionScore]:
    measured_files = {
        Path(filename).resolve() for filename in coverage.get_data().measured_files()
    }
    if source_file.resolve() not in measured_files:
        raise SystemExit(f"No coverage data found for {source_file.resolve()}")

    _, statements, excluded, missing, _ = coverage.analysis2(str(source_file))
    executable_lines = set(statements) - set(excluded)
    missing_lines = set(missing)
    scores = []
    for score in calculate_crap(str(source_file), executable_lines - missing_lines):
        function_lines = {
            line
            for line in executable_lines
            if score.start_line <= line <= score.end_line
        }
        fraction = (
            len(function_lines - missing_lines) / len(function_lines)
            if function_lines
            else 1.0
        )
        # pytest-crap's physical-line denominator penalizes comments and docstrings.
        scores.append(
            replace(
                score,
                coverage_percent=fraction * 100,
                crap=score.cc**2 * (1 - fraction) ** 3 + score.cc,
            )
        )
    return scores


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_file", type=Path)
    parser.add_argument("--coverage-file", type=Path, default=Path(".coverage"))
    parser.add_argument("--max-crap", type=float, required=True)
    args = parser.parse_args()

    if args.source_file.is_dir():
        source_files = sorted(
            path
            for path in args.source_file.rglob("*.py")
            if not {"test", "tests"}.intersection(
                path.relative_to(args.source_file).parts
            )
        )
    else:
        source_files = [args.source_file]
    if not source_files:
        raise SystemExit(f"No production Python files found in {args.source_file}")

    coverage = Coverage(data_file=str(args.coverage_file))
    coverage.load()
    scores = [
        score
        for source_file in source_files
        for score in scores_for_file(coverage, source_file)
    ]
    offenders = [score for score in scores if score.crap >= args.max_crap]
    print(
        f"Analyzed {len(scores)} functions in {len(source_files)} production files; "
        f"{len(offenders)} functions scored at or above {args.max_crap:g}"
    )

    if not offenders:
        print(f"All CRAP scores are below {args.max_crap:g} for {args.source_file}")
        return 0

    print(f"CRAP scores must be below {args.max_crap:g} for {args.source_file}")
    for score in sorted(offenders, key=lambda item: item.crap, reverse=True):
        print(
            f"{score.file_path}:{score.start_line} {score.name}: CRAP={score.crap:.2f}, "
            f"CC={score.cc}, coverage={score.coverage_percent:.1f}%"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
