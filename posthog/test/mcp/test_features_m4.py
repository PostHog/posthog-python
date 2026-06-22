"""Tests for M4 parity features: get_more_tools (missing capability) + conversation_id."""

import asyncio

import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server

from posthog.mcp import PostHogMCP, get_more_tools_result, instrument
from posthog.mcp.types import MCPAnalyticsOptions


class FakeClient:
    def __init__(self):
        self.events = []

    def capture(
        self,
        event,
        distinct_id=None,
        properties=None,
        timestamp=None,
        uuid=None,
        **kwargs,
    ):
        self.events.append(
            {"event": event, "distinct_id": distinct_id, "properties": properties or {}}
        )
        return None


async def _flush():
    import posthog.mcp.instrumentation as instr

    for _ in range(10):
        await asyncio.sleep(0)
        pending = [t for t in list(instr._BACKGROUND_TASKS) if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def _events(client, name):
    return [e for e in client.events if e["event"] == name]


def make_fastmcp():
    server = FastMCP("m4-fastmcp")

    @server.tool()
    def add(a: int, b: int) -> str:
        return f"sum is {a + b}"

    return server


def make_lowlevel():
    server = Server("m4-lowlevel")

    @server.list_tools()
    async def list_tools():
        return [
            mcp_types.Tool(
                name="echo",
                description="Echo",
                inputSchema={
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text=str(arguments.get("msg")))]

    return server


def _call_request(name, arguments):
    return mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )


# --- get_more_tools / missing capability -------------------------------------


async def test_fastmcp_report_missing_advertises_and_captures():
    server = make_fastmcp()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=True))

    list_handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    result = await list_handler(mcp_types.ListToolsRequest(method="tools/list"))
    tool_names = [t.name for t in result.root.tools]
    assert "get_more_tools" in tool_names

    canned = await server._tool_manager.call_tool(
        "get_more_tools", {"context": "wanted a tool to export results to CSV"}
    )
    await _flush()

    assert "noted your feedback" in canned[0].text
    missing = _events(client, "$mcp_missing_capability")
    assert (
        missing
        and missing[0]["properties"]["$mcp_intent"]
        == "wanted a tool to export results to CSV"
    )
    # a get_more_tools call is NOT a normal tool call
    assert _events(client, "$mcp_tool_call") == []


async def test_lowlevel_report_missing_advertises_and_captures():
    server = make_lowlevel()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=True))

    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = await list_handler(mcp_types.ListToolsRequest(method="tools/list"))
    assert "get_more_tools" in [t.name for t in result.root.tools]

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    out = await call_handler(
        _call_request("get_more_tools", {"context": "need a scheduling tool"})
    )
    await _flush()

    assert out.root.isError is False
    assert "noted your feedback" in out.root.content[0].text
    assert _events(client, "$mcp_missing_capability")


# --- conversation_id ---------------------------------------------------------


async def test_fastmcp_conversation_id_captured():
    server = make_fastmcp()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=True))

    await server._tool_manager.call_tool(
        "add",
        {"a": 1, "b": 2, "context": "summing for the report"},
        convert_result=True,
    )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"].get("$mcp_conversation_id")  # minted


async def test_lowlevel_conversation_id_captured_and_prompt_back():
    server = make_lowlevel()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=True))

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    out = await call_handler(_call_request("echo", {"msg": "hi", "context": "echoing"}))
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    conv_id = calls[0]["properties"].get("$mcp_conversation_id")
    assert conv_id
    # prompt-back appended to the result so the agent echoes the id
    texts = [c.text for c in out.root.content if getattr(c, "type", None) == "text"]
    assert any(f"conversation_id={conv_id}" in t for t in texts)


async def test_conversation_id_reused_when_supplied():
    server = make_lowlevel()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=True))

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    await call_handler(
        _call_request(
            "echo", {"msg": "hi", "conversation_id": "conv-123", "context": "x"}
        )
    )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls[0]["properties"]["$mcp_conversation_id"] == "conv-123"


# --- PostHogMCP --------------------------------------------------------------


def test_posthog_mcp_prepare_tool_list_report_missing():
    client = PostHogMCP("phc_test", host="https://us.i.posthog.com")
    tools = [{"name": "search", "inputSchema": {"type": "object", "properties": {}}}]
    prepared = client.prepare_tool_list(tools, report_missing=True)
    assert any(t["name"] == "get_more_tools" for t in prepared)


def test_get_more_tools_result_shape():
    result = get_more_tools_result()
    assert result["content"][0]["type"] == "text"
    assert "noted your feedback" in result["content"][0]["text"]
