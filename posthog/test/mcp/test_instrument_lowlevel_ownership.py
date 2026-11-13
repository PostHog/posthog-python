"""Ownership inference stays safe when a host exposes only a function registry."""

from types import SimpleNamespace

import pytest

from posthog.mcp._instrument_lowlevel import (
    _standalone_ownership,
    registered_tool_exists,
)
from posthog.mcp._internal import MCPAnalyticsData
from posthog.mcp.types import MCPAnalyticsOptions


def _application_arguments(context, conversation_id, llm_model):
    pass


class _UninspectableFunction:
    @property
    def __signature__(self):
        raise ValueError("signature unavailable")

    def __call__(self):
        pass


@pytest.mark.parametrize(
    ("tool", "expected_strip", "expected_model"),
    [
        (None, {"context", "conversation_id"}, None),
        (SimpleNamespace(), {"context", "conversation_id", "llm_model"}, True),
        (SimpleNamespace(fn=_application_arguments), set(), False),
        (
            SimpleNamespace(fn=_UninspectableFunction()),
            {"context", "conversation_id", "llm_model"},
            True,
        ),
    ],
)
async def test_signature_only_registry_preserves_application_arguments(
    tool, expected_strip, expected_model
):
    async def get_tool(name):
        assert name == "search"
        return tool

    server = SimpleNamespace(get_tool=get_tool, middleware=[])
    data = MCPAnalyticsData(options=MCPAnalyticsOptions())

    strip, model_owned, schema = await _standalone_ownership(
        data, server, "search", None
    )

    assert strip == expected_strip
    assert model_owned is expected_model
    assert schema is None


async def test_registry_outage_is_not_evidence_of_an_unknown_tool():
    fastmcp = pytest.importorskip("fastmcp")
    server = fastmcp.FastMCP("registry-outage")

    async def unavailable(name):
        raise RuntimeError("registry temporarily unavailable")

    server.get_tool = unavailable

    assert await registered_tool_exists(server, "search") is None
    assert await registered_tool_exists(None, "search") is None
