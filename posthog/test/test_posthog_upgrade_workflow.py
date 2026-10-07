"""Exercise the inline downstream-upgrade selector without GitHub writes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/posthog-upgrade.yml"
TITLE_PREFIX = "chore(deps): update posthoganalytics to "
FALLBACK = "posthoganalytics-upgrade-7.64.1"
RUNNING = "Running tests on this pull request"
WAITING = "waiting to start tests"


def step_source(name):
    return (
        WORKFLOW.read_text()
        .split(f"      - name: {name}\n", 1)[1]
        .split("      - name:", 1)[0]
    )


def selector_script():
    return textwrap.dedent(
        step_source("Generate pull request details").split("        run: |\n", 1)[1]
    )


def pr(number=1, version="7.63.0", branch="posthoganalytics-7.63.0", **kwargs):
    return {
        "number": number,
        "title": TITLE_PREFIX + version,
        "headRefName": branch,
        "headRepositoryOwner": {"login": "PostHog"},
        "isCrossRepository": False,
        "createdAt": number,
        **kwargs,
    }


def comments(*statuses):
    return [[{"user": {"login": "trunk-io[bot]"}, "body": s} for s in statuses]]


# Execute the workflow's jq filters, not an alternate implementation of them.
# The fake provider sorts only when the request explicitly asks it to.
GH_MOCK = r"""
import json
import os
import subprocess
import sys

args = sys.argv[1:]
with open(os.environ["MOCK_CALLS"], "a") as file:
    file.write(json.dumps(args) + "\n")
fixture = json.loads(os.environ["MOCK_FIXTURE"])
if args[:2] == ["pr", "list"]:
    assert args[args.index("--repo") + 1] == "PostHog/posthog"
    assert args[args.index("--state") + 1] == "open"
    records = fixture["prs"]
    if "--head" in args:
        if fixture.get("fallback_fail"):
            sys.exit(1)
        head = args[args.index("--head") + 1]
        records = [p for p in records if p["headRefName"] == head]
    else:
        if fixture.get("list_fail"):
            sys.exit(1)
        assert args.index("--search") < args.index("--limit")
        search = args[args.index("--search") + 1]
        assert '"chore(deps): update posthoganalytics to " in:title' in search
        if "sort:created-desc" in search:
            records = sorted(records, key=lambda p: p["createdAt"], reverse=True)
    records = records[:int(args[args.index("--limit") + 1])]
elif args[0] == "api":
    assert "--paginate" in args and "--slurp" in args
    number = args[1].split("/")[4]
    if int(number) in fixture.get("api_fail", []):
        sys.exit(1)
    records = fixture.get("comments", {}).get(number, [[]])
else:
    raise AssertionError("Unexpected GitHub call: " + repr(args))
result = subprocess.run(
    ["jq", "-r", args[args.index("--jq") + 1]],
    input=json.dumps(records), text=True, capture_output=True, check=True,
)
sys.stdout.write(result.stdout)
"""

UV_MOCK = """
import os
import sys

assert sys.argv[1:6] == ["run", "--no-project", "--with", "packaging==25.0", "python"]
os.execv(sys.executable, [sys.executable, *sys.argv[6:]])
"""


@pytest.fixture
def select(tmp_path):
    missing = [tool for tool in ("bash", "jq") if shutil.which(tool) is None]
    if missing:
        pytest.skip(
            f"Install {', '.join(missing)} and make it available on PATH "
            "to run the workflow shell-entry tests."
        )

    for name, source in [("gh", GH_MOCK), ("uv", UV_MOCK)]:
        executable = tmp_path / name
        executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o755)

    def run(prs=(), version="7.64.1", **fixture):
        output = tmp_path / "output"
        output.write_text("")
        calls = tmp_path / "calls"
        calls.write_text("")
        env = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "PACKAGE_NAME": "posthoganalytics",
            "PACKAGE_VERSION": version,
            "GITHUB_OUTPUT": str(output),
            "MOCK_CALLS": str(calls),
            "MOCK_FIXTURE": json.dumps({"prs": prs, **fixture}),
        }
        result = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-e",
                "-o",
                "pipefail",
                "-c",
                selector_script(),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        requests = [json.loads(line) for line in calls.read_text().splitlines()]
        # Every scenario must use explicit chronological search, including skips.
        assert "sort:created-desc" in requests[0][requests[0].index("--search") + 1]
        return result, output.read_text(), requests

    return run


def assert_branch(result, branch, version="7.64.1"):
    process, output, _ = result
    assert process.returncode == 0, process.stderr
    assert "skip=false\n" in output
    assert f"branch_name={branch}\n" in output
    assert f"title={TITLE_PREFIX}{version}\n" in output
    assert f"PostHog Python SDK version {version} has been released." in output
    assert "`posthoganalytics` and `posthog`" in output


def assert_skip(result):
    process, output, _ = result
    assert process.returncode == 0, process.stderr
    assert output == "skip=true\n"


def assert_failure(result):
    process, output, _ = result
    assert process.returncode != 0
    assert output == ""


@pytest.mark.parametrize(
    "prs,branch",
    [
        ([], "posthoganalytics-upgrade"),
        ([pr()], "posthoganalytics-7.63.0"),
        ([pr(branch="posthoganalytics-upgrade")], "posthoganalytics-upgrade"),
        ([pr(headRepositoryOwner={"login": "someone"})], "posthoganalytics-upgrade"),
        ([pr(title="chore(deps): update other to 1.0.0")], "posthoganalytics-upgrade"),
    ],
    ids=["no-pr", "legacy-branch", "stable-branch", "fork", "other-package"],
)
def test_branch_selection(select, prs, branch):
    assert_branch(select(prs), branch)


@pytest.mark.parametrize("status", [RUNNING, WAITING])
def test_queued_selected_pr_uses_version_branch(select, status):
    assert_branch(select([pr()], comments={"1": comments(status)}), FALLBACK)


def test_unknown_selected_queue_status_checks_fallback(select):
    result = select([pr()], api_fail=[1])
    assert_branch(result, FALLBACK)
    assert any("--head" in call for call in result[2])


def test_pr_lookup_failure_fails_safely(select):
    assert_failure(select(list_fail=True))


@pytest.mark.parametrize(
    "incoming,current,skip",
    [
        ("7.10", "7.9", False),
        ("7.9", "7.10", True),
        ("7.64.1", "7.64.1", False),
        ("7.64.1.0", "7.64.1", False),
        ("7.64.1", "7.64.1rc1", False),
        ("7.64.1rc1", "7.64.1", True),
        ("7.64.1rc2", "7.64.1rc1", False),
        ("7.64.1rc1", "7.64.1rc2", True),
    ],
)
def test_pep440_version_ordering(select, incoming, current, skip):
    result = select([pr(version=current)], version=incoming)
    if skip:
        assert_skip(result)
    else:
        assert_branch(result, "posthoganalytics-7.63.0", incoming)


@pytest.mark.parametrize(
    "incoming,current", [("7.64.1", "unknown"), ("unknown", "7.64.1"), ("7.64.1", "")]
)
def test_unparseable_comparison_fails_safely(select, incoming, current):
    assert_failure(select([pr(version=current)], version=incoming))


def test_newer_run_then_older_rerun_preserves_shared_branch_version(select):
    existing = pr(branch="posthoganalytics-upgrade")
    assert_branch(select([existing]), existing["headRefName"])
    existing["title"] = TITLE_PREFIX + "7.64.1"  # Metadata after the newer run.
    assert_skip(select([existing], version="7.64.0"))
    assert existing["title"] == TITLE_PREFIX + "7.64.1"


def test_different_versions_share_reusable_branch(select):
    existing = pr(branch="posthoganalytics-upgrade")
    for version in ["7.64.0", "7.64.1"]:
        assert_branch(
            select([existing], version=version), existing["headRefName"], version
        )


def test_older_run_skips_even_if_newer_selected_pr_is_queued(select):
    assert_skip(select([pr(version="7.64.2")], comments={"1": comments(RUNNING)}))


@pytest.mark.parametrize("unknown", [False, True], ids=["queued", "unknown"])
def test_same_version_fallback_is_protected(select, unknown):
    result = select(
        [pr(version="7.64.1", branch=FALLBACK)],
        comments={"1": comments(RUNNING)},
        api_fail=[1] if unknown else [],
    )
    assert_skip(result)
    assert any("--head" in call for call in result[2])


@pytest.mark.parametrize("unknown", [False, True], ids=["queued", "unknown"])
def test_older_fallback_collision_is_independently_protected(select, unknown):
    result = select(
        [pr(branch=FALLBACK), pr(2, version="7.64.1", branch="newest")],
        comments={"1": comments(RUNNING), "2": comments(WAITING)},
        api_fail=[1] if unknown else [],
    )
    assert_skip(result)
    apis = [call[1] for call in result[2] if call[0] == "api"]
    assert apis == [
        "repos/PostHog/posthog/issues/2/comments?per_page=100",
        "repos/PostHog/posthog/issues/1/comments?per_page=100",
    ]


def test_fallback_lookup_failure_skips(select):
    assert_skip(select([pr()], comments={"1": comments(RUNNING)}, fallback_fail=True))


def test_unqueued_fallback_is_reused(select):
    assert_branch(
        select(
            [pr(branch=FALLBACK), pr(2, version="7.64.1", branch="newest")],
            comments={
                "1": comments(RUNNING, "Removed from queue"),
                "2": comments(RUNNING),
            },
        ),
        FALLBACK,
    )


@pytest.mark.parametrize("current", ["7.64.2", "invalid", ""])
def test_fallback_destination_version_is_checked(select, current):
    result = select(
        [pr(version=current, branch=FALLBACK), pr(2, branch="newest")],
        comments={"2": comments(RUNNING)},
    )
    if current == "7.64.2":
        assert_skip(result)
    else:
        assert_failure(result)


def test_fallback_with_unrecognized_title_fails_safely(select):
    assert_failure(
        select(
            [pr(branch=FALLBACK, title="Another change"), pr(2, branch="newest")],
            comments={"2": comments(RUNNING)},
        )
    )


def test_fallback_lookup_ignores_cross_repository_pr(select):
    assert_branch(
        select(
            [pr(branch=FALLBACK, isCrossRepository=True), pr(2, branch="newest")],
            comments={"1": comments(RUNNING), "2": comments(RUNNING)},
        ),
        FALLBACK,
    )


def test_search_orders_before_newest_only_queue_selection(select):
    result = select([pr(1, branch="older"), pr(2, branch="newest")])
    assert_branch(result, "newest")
    assert [call[1] for call in result[2] if call[0] == "api"] == [
        "repos/PostHog/posthog/issues/2/comments?per_page=100"
    ]


@pytest.mark.parametrize("cleared", [False, True])
def test_queue_comments_are_paginated_and_latest_trunk_status_wins(select, cleared):
    pages = comments(RUNNING)
    pages[0].extend([{"user": {"login": "someone"}, "body": RUNNING}] * 99)
    pages += comments("Removed from queue" if cleared else WAITING)
    assert_branch(
        select([pr()], comments={"1": pages}),
        "posthoganalytics-7.63.0" if cleared else FALLBACK,
    )


def test_other_commenters_do_not_determine_queue_status(select):
    assert_branch(
        select(
            [pr()], comments={"1": [[{"user": {"login": "someone"}, "body": RUNNING}]]}
        ),
        "posthoganalytics-7.63.0",
    )


def test_writers_share_non_cancelling_package_concurrency():
    source = WORKFLOW.read_text()
    concurrency = source.split("\nconcurrency:\n", 1)[1].split("\njobs:", 1)[0]
    assert (
        "group: posthog-upgrade-${{ github.event.inputs.package_name }}" in concurrency
    )
    assert "cancel-in-progress: false" in concurrency


def test_remote_mutations_are_gated_and_use_selector_outputs():
    for name in [
        "Create main repo pull request",
        "Update pull request metadata",
        "Assign reviewers",
    ]:
        assert "if: steps.generate-pr-details.outputs.skip == 'false'" in step_source(
            name
        )
    create = step_source("Create main repo pull request")
    for field, output in [
        ("branch", "branch_name"),
        ("title", "title"),
        ("body", "body"),
    ]:
        assert (
            f"{field}: ${{{{ steps.generate-pr-details.outputs.{output} }}}}" in create
        )
    metadata = step_source("Update pull request metadata")
    assert "PR_TITLE: ${{ steps.generate-pr-details.outputs.title }}" in metadata
    assert "PR_BODY: ${{ steps.generate-pr-details.outputs.body }}" in metadata
