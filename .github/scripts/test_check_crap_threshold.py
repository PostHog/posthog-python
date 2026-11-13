"""Regression tests for executable-line CRAP scoring."""

from pathlib import Path
import subprocess
import sys

from coverage import CoverageData
import pytest


SCRIPT_PATH = Path(__file__).with_name("check_crap_threshold.py")
FUNCTION = """def choose(value):
    if value == 1:
        return 'one'
    if value == 2:
        return 'two'
    if value == 3:
        return 'three'
    if value == 4:
        return 'four'
    return 'other'

for value in range(5):
    choose(value)
"""


def collect_coverage(tmp_path, source):
    source_file = (tmp_path / "example.py").resolve()
    source_file.write_text(source)
    data_file = tmp_path / ".coverage"
    config_file = tmp_path / ".coveragerc"
    config_file.write_text("[run]\n")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            f"--rcfile={config_file}",
            f"--data-file={data_file}",
            str(source_file),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return source_file, data_file


def check(source_file, data_file, threshold):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(source_file),
            "--coverage-file",
            str(data_file),
            "--max-crap",
            str(threshold),
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "source",
    [
        FUNCTION,
        FUNCTION.replace(
            "    if value == 1:", "    # Explanation\n\n" * 30 + "    if value == 1:"
        ),
        FUNCTION.replace(
            "    if value == 1:",
            '    """Description.\n'
            + "    Details.\n" * 30
            + '    """\n    if value == 1:',
        ),
        FUNCTION.replace("def choose(value):", "def choose(\n    value,\n):"),
    ],
    ids=["plain", "comments-and-blanks", "docstring", "multiline-signature"],
)
def test_fully_covered_function_is_unaffected_by_non_executable_lines(tmp_path, source):
    source_file, data_file = collect_coverage(tmp_path, source)
    result = check(source_file, data_file, 10)
    assert result.returncode == 0, result.stdout + result.stderr
    at_complexity_floor = check(source_file, data_file, 5)
    assert at_complexity_floor.returncode == 1
    assert "CRAP=5.00, CC=5, coverage=100.0%" in at_complexity_floor.stdout


def test_uncovered_executable_branch_still_increases_score(tmp_path):
    source_file, data_file = collect_coverage(
        tmp_path,
        "def choose(value):\n    if value:\n        return 1\n    return 0\n\nchoose(True)\n",
    )
    result = check(source_file, data_file, 2.0625)
    assert result.returncode == 1
    assert "CRAP=2.06, CC=2, coverage=75.0%" in result.stdout
    assert check(source_file, data_file, 2.0626).returncode == 0


def test_coverage_exclusions_are_not_missing_statements(tmp_path):
    source_file, data_file = collect_coverage(
        tmp_path,
        "def choose(value):\n    if value:  # pragma: no cover\n        return 1\n    return 0\n\nchoose(False)\n",
    )
    result = check(source_file, data_file, 2)
    assert result.returncode == 1
    assert "CRAP=2.00, CC=2, coverage=100.0%" in result.stdout


def test_directory_includes_unexecuted_production_and_excludes_test_helpers(tmp_path):
    package = tmp_path / "posthog"
    source_file = package / "ai" / "converter.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 0\n"
    )
    for directory in ("test", "tests"):
        helper = package / directory / "ai" / "utils.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("not valid Python")
    data_file = tmp_path / ".coverage"
    data = CoverageData(basename=str(data_file))
    data.add_lines({str(source_file.resolve()): set()})
    data.write()
    result = check(package, data_file, 6)
    assert result.returncode == 1
    assert "1 functions in 1 production files" in result.stdout
    assert "CRAP=6.00, CC=2, coverage=0.0%" in result.stdout
    assert str(source_file) in result.stdout


def test_missing_coverage_does_not_pass(tmp_path):
    source_file = tmp_path / "example.py"
    source_file.write_text("def choose():\n    return 1\n")
    result = check(source_file, tmp_path / ".coverage", 10)
    assert result.returncode != 0
    assert "No coverage data found" in result.stderr


def test_empty_source_directory_does_not_pass(tmp_path):
    result = check(tmp_path, tmp_path / ".coverage", 10)
    assert result.returncode != 0
    assert "No production Python files found" in result.stderr
