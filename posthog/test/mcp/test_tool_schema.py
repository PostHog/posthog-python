import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from posthog.mcp._internal import MCPAnalyticsData
from posthog.mcp._tool_schema import resolve_model_ownership
from posthog.mcp.types import MCPAnalyticsOptions


def data():
    return MCPAnalyticsData(options=MCPAnalyticsOptions())


@pytest.mark.parametrize("owned", [True, False])
async def test_paginated_catalog_caches_confirmed_model_ownership(owned):
    schema = {
        "type": "object",
        "properties": {"llm_model": {"type": "string"}} if owned else {},
    }
    tool = SimpleNamespace(name="echo", input_schema=schema)
    listing = AsyncMock(
        side_effect=[
            SimpleNamespace(tools=[], next_cursor="next"),
            SimpleNamespace(tools=[tool]),
        ]
    )
    state = data()
    for _ in range(2):
        assert await resolve_model_ownership(state, "echo", listing) is not owned
    assert [call.args for call in listing.call_args_list] == [(None,), ("next",)]


async def test_missing_tool_is_retried_when_it_appears():
    tool = SimpleNamespace(name="echo", input_schema={"type": "object"})
    listing = AsyncMock(
        side_effect=[SimpleNamespace(tools=[]), SimpleNamespace(tools=[tool])]
    )
    state = data()
    assert await resolve_model_ownership(state, "echo", listing) is False
    assert "echo" not in state.tool_model_parameter_injected
    assert await resolve_model_ownership(state, "echo", listing) is True
    assert listing.call_count == 2


@pytest.mark.parametrize(
    "mode,expected_calls",
    [("cycle", 2), ("endless", 16), ("malformed", 1), ("error", 1)],
)
async def test_bounded_catalog_failures(mode, expected_calls):
    calls = []

    async def listing(cursor):
        calls.append(cursor)
        if mode == "error":
            raise ValueError("unavailable")
        if mode == "malformed":
            return None
        return SimpleNamespace(
            tools=[], next_cursor="same" if mode == "cycle" else str(len(calls))
        )

    state = data()
    assert await resolve_model_ownership(state, "echo", listing) is False
    assert "echo" not in state.tool_model_parameter_injected
    assert len(calls) == expected_calls


async def test_slow_listing_does_not_block_dispatch():
    cancelled = asyncio.Event()

    async def listing(cursor):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    state = data()
    assert await resolve_model_ownership(state, "echo", listing) is False
    assert "echo" not in state.tool_model_parameter_injected
    assert cancelled.is_set()


async def test_opt_out_does_not_invoke_catalog():
    state = MCPAnalyticsData(options=MCPAnalyticsOptions(capture_model=False))
    listing = AsyncMock()
    assert await resolve_model_ownership(state, "echo", listing) is False
    listing.assert_not_called()
