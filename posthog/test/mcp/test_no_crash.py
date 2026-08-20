"""``instrument()`` must never crash the host app — runs under both MCP majors.

The compatibility layer used to import ``mcp.server.fastmcp`` at module scope,
which raises ``ImportError`` on mcp>=2 and propagated straight out of
``instrument()`` into the host (the Python twin of posthog-js#4449, but a crash
instead of a silent no-op). These tests pin the graceful-degradation contract
on whichever major is installed.
"""

import pytest

from posthog.mcp import instrument
from posthog.test.mcp._helpers import MCP_MAJOR, FakeClient


async def test_unsupported_server_returns_noop_handle():
    handle = instrument(object(), FakeClient())
    # graceful no-op: capture and flush do nothing and do not raise
    await handle.capture("anything")
    await handle.flush()


async def test_unsupported_server_logs_instead_of_raising():
    from posthog.mcp import set_logger
    from posthog.mcp.types import MCPAnalyticsOptions

    lines = []
    try:
        instrument(object(), FakeClient(), MCPAnalyticsOptions(logger=lines.append))
    finally:
        set_logger(None)
    assert any("failed to instrument" in line.lower() for line in lines)


def test_supported_server_type_detected_on_installed_major():
    """The high-level server class of the installed major must be detected —
    the exact regression of posthog-js#4449 was the compatibility gate
    rejecting every server of the newer major."""
    from posthog.mcp import _compatibility as compat

    if MCP_MAJOR >= 2:
        from mcp.server.mcpserver import MCPServer

        assert compat.is_mcpserver(MCPServer("probe")) is True
        assert compat.is_fastmcp(MCPServer("probe")) is False
    else:
        from mcp.server.fastmcp import FastMCP

        assert compat.is_fastmcp(FastMCP("probe")) is True
        assert compat.is_mcpserver(FastMCP("probe")) is False


def test_low_level_server_detected_on_installed_major():
    from mcp.server.lowlevel import Server

    from posthog.mcp import _compatibility as compat

    assert compat.is_low_level_server(Server("probe")) is True
    assert compat.is_low_level_server(object()) is False


@pytest.mark.parametrize(
    ("installed", "should_warn"),
    [
        ("1.25.0", True),
        ("1.26.0", False),
        ("1.29.0", False),
        ("2.0.0", False),
        ("2.9.9", False),
        ("3.0.0", True),
    ],
)
def test_version_advisory_fires_only_outside_supported_range(
    monkeypatch, installed, should_warn
):
    import posthog.mcp as mcp_pkg
    from posthog.mcp import logger as mcp_logger

    lines = []
    monkeypatch.setattr(mcp_logger, "_active_logger", lines.append)
    monkeypatch.setattr(
        "importlib.metadata.version", lambda name: installed if name == "mcp" else "0"
    )

    mcp_pkg._warn_if_unsupported_mcp_version()

    warned = any("tested against" in line for line in lines)
    assert warned is should_warn
