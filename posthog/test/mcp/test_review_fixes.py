"""Regression tests for the PR review findings (Manoel's agent reviews):

- B: a tool that declares its own ``context`` keeps it while an injected
  ``conversation_id`` is stripped (the two are decoupled).
- E: ``tools/list`` captures ``$mcp_response`` + ``$mcp_duration_ms``, and a
  list handler that raises is captured as an errored ``$mcp_tools_list``.
- F: ``$mcp_initialize`` is emitted on a ``tools/list`` (a client may list but
  never call a tool).
- G: a low-level call handler that raises directly (not via the decorator that
  converts to ``isError``) is still captured.
- A: ``drain_pending_sync`` waits for background-loop futures (sync hosts).
"""

import asyncio

import pytest

import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server

from posthog.mcp import instrument
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)


def _call_request(name, arguments):
    return mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )


def _list_request():
    return mcp_types.ListToolsRequest(method="tools/list")


def make_lowlevel():
    server = Server("review-lowlevel")

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


# --- B: context-owning tool keeps context, conversation_id is stripped --------


async def test_tool_owning_context_keeps_it_and_strips_conversation_id():
    server = FastMCP("ctx-owner")

    @server.tool()
    def summarize(text: str, context: str) -> str:
        # The tool declares its OWN `context` param — it must reach the tool.
        return f"{text}|ctx={context}"

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=True))

    result = await server._tool_manager.call_tool(
        "summarize",
        {"text": "hi", "context": "my own context", "conversation_id": "conv-xyz"},
        convert_result=True,
    )
    await _flush()

    # The tool's own context flowed through (not stripped, not broken by the
    # injected conversation_id sitting alongside it).
    text_blocks = [
        c.text for c in result[0] if getattr(c, "type", None) == "text"
    ]
    assert any("ctx=my own context" in t for t in text_blocks)

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_conversation_id"] == "conv-xyz"


# --- E: tools/list response + duration, and failure capture -------------------


async def test_tools_list_captures_response_and_duration():
    server = make_lowlevel()
    client = FakeClient()
    instrument(server, client)

    await server.request_handlers[mcp_types.ListToolsRequest](_list_request())
    await _flush()

    listed = _events(client, "$mcp_tools_list")
    assert listed
    props = listed[0]["properties"]
    assert props["$mcp_response"] is not None
    assert "$mcp_duration_ms" in props
    assert props["$mcp_is_error"] is False


async def test_tools_list_handler_raise_is_captured():
    server = Server("list-raises")

    async def raising_list(req):
        raise RuntimeError("list boom")

    # Install a raising handler directly so instrument() wraps it as the original.
    server.request_handlers[mcp_types.ListToolsRequest] = raising_list

    client = FakeClient()
    instrument(server, client)

    with pytest.raises(RuntimeError):
        await server.request_handlers[mcp_types.ListToolsRequest](_list_request())
    await _flush()

    listed = _events(client, "$mcp_tools_list")
    assert listed and listed[0]["properties"]["$mcp_is_error"] is True
    assert _events(client, "$exception")


# --- F: $mcp_initialize emitted on tools/list ---------------------------------


async def test_initialize_emitted_on_tools_list_without_a_tool_call():
    server = make_lowlevel()
    client = FakeClient()
    instrument(server, client)

    # List tools, never call one.
    await server.request_handlers[mcp_types.ListToolsRequest](_list_request())
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 1


# --- G: low-level handler that raises directly is captured --------------------


async def test_lowlevel_direct_raise_is_captured():
    server = Server("call-raises")

    @server.list_tools()
    async def _lt():
        return [
            mcp_types.Tool(
                name="boom",
                description="b",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    async def raising_call(req):
        raise RuntimeError("direct raise")

    # Bypass the @server.call_tool() decorator (which converts raises to
    # isError) so the wrapped handler raises directly.
    server.request_handlers[mcp_types.CallToolRequest] = raising_call

    client = FakeClient()
    instrument(server, client)

    with pytest.raises(RuntimeError):
        await server.request_handlers[mcp_types.CallToolRequest](
            _call_request("boom", {"context": "trying"})
        )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    assert _events(client, "$exception")


# --- A: sync drain waits for background-loop futures --------------------------


def test_drain_pending_sync_waits_for_background_futures():
    # No running loop here (a plain sync test), so fire_and_forget schedules on the
    # shared background loop as a concurrent.futures.Future — which drain_pending()
    # used to skip. drain_pending_sync must block until it completes.
    import posthog.mcp.instrumentation as instr

    done = []

    async def slow_capture():
        await asyncio.sleep(0.05)
        done.append(1)

    instr.fire_and_forget(slow_capture())
    instr.drain_pending_sync(timeout=2)

    assert done == [1]


# --- D: PostHogMCP is usable without the official mcp SDK installed ------------


def test_posthogmcp_usable_without_mcp_sdk():
    # Block `import mcp` in a fresh interpreter and confirm PostHogMCP (the custom-
    # dispatcher client) still imports, while instrument() raises a clear install hint.
    import subprocess
    import sys

    code = (
        "import sys\n"
        "sys.modules['mcp'] = None\n"  # makes `import mcp` raise ImportError
        "from posthog.mcp import PostHogMCP, instrument\n"
        "c = PostHogMCP('phc_test')\n"
        "print('IMPORT_OK')\n"
        "try:\n"
        "    instrument(object(), c)\n"
        "    print('NO_RAISE')\n"
        "except ModuleNotFoundError as e:\n"
        "    print('RAISED' if 'mcp>=1.26' in str(e) else 'WRONG')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "IMPORT_OK" in out.stdout, out.stderr
    assert "RAISED" in out.stdout, out.stdout + out.stderr


# --- H: tracking is canonicalized to the underlying low-level server ----------


async def test_instrument_canonicalizes_wrapper_and_underlying_server():
    from posthog.mcp.internal import get_server_tracking_data

    server = FastMCP("canon")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    client = FakeClient()
    instrument(server, client)
    data1 = get_server_tracking_data(server._mcp_server)
    assert data1 is not None

    wrapped = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    # Instrumenting the underlying low-level server resolves to the same state.
    instrument(server._mcp_server, client)
    assert get_server_tracking_data(server._mcp_server) is data1
    assert server._mcp_server.request_handlers[mcp_types.CallToolRequest] is wrapped
