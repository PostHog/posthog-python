import pytest

from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    MCP_MAJOR,
    FakeClient,
    events_named,
    flush_background,
)


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
    received = []
    original = server.request_handlers[types.CallToolRequest]

    async def spy(req):
        received.append(dict(req.params.arguments or {}))
        return await original(req)

    server.request_handlers[types.CallToolRequest] = spy
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
    # A cold raw instance reads the self-reported model but strips nothing: it
    # cannot prove it owns the argument, and raw handlers ignore extra keys.
    assert received == [{"msg": "ok", "llm_model": "model-a"}]


@pytest.mark.parametrize("source", ["transport", "token"])
@pytest.mark.parametrize("echoed", [False, True])
async def test_carried_session_is_preserved_until_agent_echoes(source, echoed):
    from posthog.mcp._internal import MCPAnalyticsData
    from posthog.mcp._instrumentation import start_tool_call_lifecycle
    from posthog.mcp.session import (
        derive_session_id_from_conversation,
        derive_session_id_from_mcp_session,
    )
    from posthog.mcp.session_token import SessionTokenPayload

    handle = "019fd2b0-4444-7444-8444-444444444444"
    call = start_tool_call_lifecycle(
        MCPAnalyticsData(options=MCPAnalyticsOptions()),
        name="echo",
        arguments={"conversation_id": handle} if echoed else {},
        request_meta=None,
        allow_self_reported_model=True,
        mcp_session_id="transport-session" if source == "transport" else None,
        token=SessionTokenPayload(session_id="ses_carried")
        if source == "token"
        else None,
        client_name=None,
        client_version=None,
        protocol_version=None,
        extra={},
    )
    expected = (
        derive_session_id_from_conversation(handle)
        if echoed
        else derive_session_id_from_mcp_session("transport-session")
        if source == "transport"
        else "ses_carried"
    )
    assert not call.minted_conversation_id
    assert call.conversation_id == (handle if echoed else None)
    assert call.virtual_result_texts("ok") == ["ok"]
    assert await call.prepare_session(call.conversation_id) == expected
