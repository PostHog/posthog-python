"""Tests for the PostHogMCP custom-dispatcher client (Milestone 3)."""

import asyncio

from posthog.mcp import PostHogMCP


def make_client():
    client = PostHogMCP("phc_test", host="https://us.i.posthog.com")
    captured = []
    # Intercept the inherited Client.capture so nothing is sent over the network.
    client.capture = lambda event, **kwargs: captured.append({"event": event, **kwargs})
    return client, captured


async def _flush():
    import posthog.mcp.instrumentation as instr

    for _ in range(10):
        await asyncio.sleep(0)
        pending = [t for t in list(instr._BACKGROUND_TASKS) if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def _events(captured, name):
    return [e for e in captured if e["event"] == name]


async def test_capture_tool_call_success():
    client, captured = make_client()
    client.capture_tool_call(
        "search_docs",
        intent="finding the install guide",
        intent_source="context_parameter",
        duration_ms=42,
        distinct_id="user_1",
        groups={"organization": "org_1"},
    )
    await _flush()

    calls = _events(captured, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "search_docs"
    assert props["$mcp_intent"] == "finding the install guide"
    assert props["$mcp_is_error"] is False
    assert props["$mcp_duration_ms"] == 42
    assert props["$groups"] == {"organization": "org_1"}
    assert calls[0]["distinct_id"] == "user_1"


async def test_capture_tool_call_error_fans_out_exception():
    client, captured = make_client()
    client.capture_tool_call(
        "broken", is_error=True, error=RuntimeError("kaboom"), distinct_id="u"
    )
    await _flush()

    assert _events(captured, "$mcp_tool_call")[0]["properties"]["$mcp_is_error"] is True
    exc = _events(captured, "$exception")
    assert exc and exc[0]["properties"]["$exception_list"][0]["value"] == "kaboom"


async def test_capture_initialize_and_tools_list():
    client, captured = make_client()
    client.capture_initialize(
        client_name="claude-code", client_version="1.2.3", distinct_id="u"
    )
    client.capture_tools_list(tool_names=["a", "b"], distinct_id="u")
    await _flush()

    init = _events(captured, "$mcp_initialize")
    assert init and init[0]["properties"]["$mcp_client_name"] == "claude-code"
    listed = _events(captured, "$mcp_tools_list")
    assert listed and listed[0]["properties"]["$mcp_listed_tool_names"] == ["a", "b"]


async def test_capture_missing_capability():
    client, captured = make_client()
    client.capture_missing_capability(
        context="wanted a tool to export to CSV", distinct_id="u"
    )
    await _flush()

    missing = _events(captured, "$mcp_missing_capability")
    assert (
        missing
        and missing[0]["properties"]["$mcp_intent"] == "wanted a tool to export to CSV"
    )


def test_prepare_tool_call_extracts_intent_and_strips_context():
    client, _ = make_client()
    prepared = client.prepare_tool_call(
        "search", {"q": "x", "context": "looking up the answer"}
    )
    assert prepared.intent == "looking up the answer"
    assert prepared.intent_source == "context_parameter"
    assert prepared.args == {"q": "x"}
    assert prepared.is_missing_capability is False

    prepared_missing = client.prepare_tool_call(
        "get_more_tools", {"context": "need something else"}
    )
    assert prepared_missing.is_missing_capability is True


def test_prepare_tool_list_injects_context_into_dicts():
    client, _ = make_client()
    tools = [
        {
            "name": "search",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]
    prepared = client.prepare_tool_list(tools)
    assert "context" in prepared[0]["inputSchema"]["properties"]
    # original tool dict is untouched
    assert "context" not in tools[0]["inputSchema"]["properties"]


def test_prepare_tool_list_can_be_disabled():
    client, _ = make_client()
    tools = [{"name": "search", "inputSchema": {"type": "object", "properties": {}}}]
    prepared = client.prepare_tool_list(tools, context=False)
    assert "context" not in prepared[0]["inputSchema"]["properties"]
