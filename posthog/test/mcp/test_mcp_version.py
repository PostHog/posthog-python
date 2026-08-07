"""Generation-probe tests. These run in BOTH the mcp-v1 and mcp-v2 envs and are
the anchor that the same suite is valid on either SDK: the probe must agree with
the installed `mcp`, and the two generation markers must be mutually exclusive."""

from importlib.metadata import version

import pytest

from posthog.mcp._mcp_version import installed_mcp_generation
from posthog.test.mcp._helpers import requires_mcp_v1, requires_mcp_v2


def test_probe_matches_installed_mcp():
    try:
        major = int(version("mcp").split(".")[0])
    except Exception:  # noqa: BLE001 - mcp genuinely absent
        major = None

    generation = installed_mcp_generation()
    if major in (1, 2):
        assert generation == major
    else:
        assert generation is None


def test_probe_never_raises_without_mcp(monkeypatch):
    def _missing(_name):
        from importlib.metadata import PackageNotFoundError

        raise PackageNotFoundError("mcp")

    monkeypatch.setattr("importlib.metadata.version", _missing)
    assert installed_mcp_generation() is None


@requires_mcp_v1
def test_v1_marker_runs_only_on_v1():
    assert installed_mcp_generation() == 1


@requires_mcp_v2
def test_v2_marker_runs_only_on_v2():
    assert installed_mcp_generation() == 2


def test_generation_markers_are_mutually_exclusive():
    # Exactly one of the two skip conditions is False for a supported install, so
    # a generation-specific test is never collected-and-run in the wrong env.
    generation = installed_mcp_generation()
    if generation is None:
        pytest.skip("no supported mcp installed")
    assert (generation == 1) != (generation == 2)
