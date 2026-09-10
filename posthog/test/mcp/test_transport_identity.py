"""``$mcp_client_user_agent`` / ``$mcp_vendor_client`` — which *product* called.

``clientInfo.name`` only says which client library is calling: Anthropic reports
``claude-code`` from the CLI, the Agent SDK, the VS Code extension and the
desktop app alike, so `$mcp_client_name` collapses every surface into one
bucket and the harness breakdown reads 100% "Other". The distinguishing detail
lives in the User-Agent parenthetical and vendor headers. Captured raw and
classified at query time. Parity with ``@posthog/mcp``.
Runs under both MCP SDK majors.
"""

from types import SimpleNamespace

from posthog.mcp import PostHogMCP
from posthog.mcp._transport_identity import stamp_transport_identity
from posthog.mcp.constants import PostHogMCPAnalyticsProperty as P
from posthog.test.mcp._helpers import (
    MCP_MAJOR,
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)

UA = "claude-code/2.1.0 (cli)"


def _extra(headers):
    return {
        "session_id": None,
        "ctx": SimpleNamespace(request=SimpleNamespace(headers=headers)),
    }


# --- reading the headers --------------------------------------------------------


def test_stamps_both_headers_onto_the_event():
    event = {}
    stamp_transport_identity(
        event, _extra({"user-agent": UA, "x-anthropic-client": "desktop"})
    )

    assert event["client_user_agent"] == UA
    assert event["vendor_client"] == "desktop"


def test_header_case_does_not_matter():
    event = {}
    stamp_transport_identity(event, _extra({"User-Agent": UA}))

    assert event["client_user_agent"] == UA


def test_each_header_is_independent():
    """Captured as two separate signals, not merged, so a query-time resolver
    can prefer whichever the vendor keeps stable."""
    ua_only, vendor_only = {}, {}
    stamp_transport_identity(ua_only, _extra({"user-agent": UA}))
    stamp_transport_identity(vendor_only, _extra({"x-anthropic-client": "desktop"}))

    assert ua_only == {"client_user_agent": UA}
    assert vendor_only == {"vendor_client": "desktop"}


def test_stdio_and_header_less_transports_stamp_nothing():
    for extra in (
        {"session_id": None, "ctx": SimpleNamespace(request=None)},  # stdio
        {"session_id": None},  # no ctx at all
        None,
    ):
        event = {}
        stamp_transport_identity(event, extra)
        assert event == {}


def test_a_hostile_header_object_cannot_break_a_tool_call():
    class Exploding:
        def items(self):
            raise RuntimeError("nope")

    event = {}
    stamp_transport_identity(event, _extra(Exploding()))
    assert event == {}


# --- end to end -----------------------------------------------------------------


async def test_instrumented_server_captures_the_surface():
    if MCP_MAJOR >= 2:
        from mcp.server.mcpserver import MCPServer as Server
    else:
        from mcp.server.fastmcp import FastMCP as Server

    from posthog.mcp import instrument

    server = Server("ua-e2e")

    @server.tool()
    def echo(msg: str) -> str:
        return msg

    client = FakeClient()
    instrument(server, client)

    context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(
                headers={"user-agent": UA, "x-anthropic-client": "cli"}
            ),
            session=SimpleNamespace(client_params=None),
        )
    )
    await server._tool_manager.call_tool(
        "echo", {"msg": "hi", "context": "surface attribution"}, context=context
    )
    await _flush()

    props = _events(client, "$mcp_tool_call")[0]["properties"]
    assert props[P.CLIENT_USER_AGENT] == UA
    assert props[P.VENDOR_CLIENT] == "cli"


async def test_custom_dispatchers_can_pass_their_own():
    """A hand-rolled dispatcher holds its own request object, so it passes the
    headers explicitly rather than us digging them out."""
    captured = []
    client = PostHogMCP("phc_test")
    client.capture = lambda event, **kw: captured.append({"event": event, **kw})

    client.capture_tool_call("add", client_user_agent=UA, vendor_client="desktop")
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert props[P.CLIENT_USER_AGENT] == UA
    assert props[P.VENDOR_CLIENT] == "desktop"


async def test_absent_headers_leave_events_byte_identical():
    """stdio servers must emit exactly what they emitted before this feature."""
    captured = []
    client = PostHogMCP("phc_test")
    client.capture = lambda event, **kw: captured.append({"event": event, **kw})

    client.capture_tool_call("add")
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert P.CLIENT_USER_AGENT not in props
    assert P.VENDOR_CLIENT not in props


async def test_a_huge_header_cannot_inflate_the_event():
    captured = []
    client = PostHogMCP("phc_test")
    client.capture = lambda event, **kw: captured.append({"event": event, **kw})

    client.capture_tool_call("add", client_user_agent="x" * 10_000)
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert len(props[P.CLIENT_USER_AGENT]) < 1000


async def test_real_v1_request_headers_reach_the_event():
    """Surface attribution over a *real* v1 HTTP request.

    The unit tests above drive a hand-built mapping; this drives Starlette's own
    ``Headers`` object through a real FastMCP streamable-HTTP app — which is
    where the feature has to work, and where every MCP client today still lives.
    """
    import pytest

    pytest.importorskip("starlette.testclient")
    pytest.importorskip("mcp.server.fastmcp")  # v1-only server; skipped under mcp>=2
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.testclient import TestClient

    from posthog.mcp import instrument

    server = FastMCP(
        "ua-wire-v1",
        stateless_http=True,
        json_response=True,
        # TestClient sends Host: testserver; allow it past DNS-rebinding protection.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    client = FakeClient()
    instrument(server, client)

    with TestClient(server.streamable_http_app()) as http:
        http.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": UA,
                "X-Anthropic-Client": "cli",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "add",
                    "arguments": {"a": 1, "b": 2, "context": "real v1 header check"},
                },
            },
        )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls, "the tool call was not captured at all"
    props = calls[0]["properties"]
    assert props[P.CLIENT_USER_AGENT] == UA
    assert props[P.VENDOR_CLIENT] == "cli"
