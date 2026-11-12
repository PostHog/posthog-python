"""The v2 ownership lookup must not consume host request cancellation."""

import asyncio
from types import SimpleNamespace

import pytest

from posthog.mcp._instrument_v2 import _standalone_injected_parameters
from posthog.mcp._internal import MCPAnalyticsData
from posthog.mcp.types import MCPAnalyticsOptions


async def test_cancellation_during_standalone_schema_lookup_propagates():
    pytest.importorskip("fastmcp")
    calls = []

    async def list_tools():
        calls.append("list")
        raise asyncio.CancelledError()

    async def get_tool(*args, **kwargs):
        calls.append("get")

    server = SimpleNamespace(list_tools=list_tools, get_tool=get_tool)
    data = MCPAnalyticsData(options=MCPAnalyticsOptions())
    with pytest.raises(asyncio.CancelledError):
        await _standalone_injected_parameters(server, data, "search", None)

    assert calls == ["list"]
    assert data.tool_model_parameter_injected == {}
