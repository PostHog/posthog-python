import pytest

from posthog.mcp import PostHogMCP
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    MCP_MAJOR,
    FakeClient,
    events_named,
    flush_background,
)


def test_model_capture_is_enabled_for_custom_dispatchers():
    for kwargs, enabled in [({}, True), ({"capture_model": False}, False)]:
        client = PostHogMCP("test", disabled=True, **kwargs)
        tools = client.prepare_tool_list(
            [{"name": "echo", "inputSchema": {"type": "object", "properties": {}}}]
        )
        assert ("llm_model" in tools[0]["inputSchema"]["properties"]) is enabled
        client.shutdown()


@pytest.mark.skipif(MCP_MAJOR < 2, reason="v2 low-level handler API")
async def test_default_capture_on_fresh_lowlevel_instances():
    from posthog.mcp import instrument
    from posthog.test.mcp.test_v2_lowlevel import make_server, _call_tool, _list_tools

    client = FakeClient()

    def fresh():
        server = make_server()
        instrument(server, client)
        return server

    listing = await _list_tools(fresh())
    assert [tool.name for tool in listing.tools] == ["add"]
    assert {"context", "llm_model", "conversation_id"} <= set(
        listing.tools[0].input_schema["properties"]
    )
    await _call_tool(
        fresh(), "add", {"a": 1, "b": 2, "context": "intent", "llm_model": "model-a"}
    )
    await flush_background()
    first = events_named(client, "$mcp_tool_call")[0]["properties"]
    await _call_tool(
        fresh(),
        "add",
        {
            "a": 2,
            "b": 3,
            "context": "intent",
            "llm_model": "model-a",
            "conversation_id": first["$mcp_conversation_id"],
        },
    )
    await flush_background()
    calls = events_named(client, "$mcp_tool_call")
    assert len(calls) == 2
    assert first["$mcp_llm_model"] == "model-a"
    assert first["$session_id"] == calls[1]["properties"]["$session_id"]
    assert len(events_named(client, "$mcp_tools_list")) == 1


@pytest.mark.skipif(MCP_MAJOR >= 2, reason="v1 low-level handler API")
@pytest.mark.parametrize("enabled", [True, False])
async def test_v1_cold_capture_and_opt_out(enabled):
    import mcp.types as types
    from posthog.mcp import instrument
    from posthog.test.mcp.test_lowlevel import make_server, _call_request

    client = FakeClient()
    server = make_server()
    options = (
        None
        if enabled
        else MCPAnalyticsOptions(capture_model=False, enable_conversation_id=False)
    )
    instrument(server, client, options)
    request = _call_request("echo", {"msg": "ok", "llm_model": "model-a"})
    result = await server.request_handlers[types.CallToolRequest](request)
    await flush_background()
    calls = events_named(client, "$mcp_tool_call")
    assert len(calls) == 1
    assert not calls[0]["properties"]["$mcp_is_error"]
    assert calls[0]["properties"].get("$mcp_llm_model") == (
        "model-a" if enabled else None
    )
    assert len(result.root.content) == (2 if enabled else 1)
    assert len(events_named(client, "$mcp_tools_list")) == 0
    assert request.params.arguments == {"msg": "ok", "llm_model": "model-a"}
