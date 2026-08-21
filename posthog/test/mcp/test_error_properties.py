"""``$mcp_error_message`` / ``$mcp_error_type`` on the primary event.

Without these the failures view has nothing to show for a Python-backed
server: the reason a call failed lived only on the ``$exception`` sibling,
which ``enable_exception_autocapture=False`` switches off entirely. Parity with
``@posthog/mcp``, which reads both off the same ``$exception_list``.
Runs under both MCP SDK majors.
"""

from posthog.mcp import PostHogMCP
from posthog.mcp.constants import PostHogMCPAnalyticsProperty as P
from posthog.test.mcp._helpers import (
    events_named as _events,
    flush_background as _flush,
)


def make_client(**kwargs):
    client = PostHogMCP("phc_test", **kwargs)
    captured = []
    # Intercept the inherited Client.capture so nothing is sent over the network.
    client.capture = lambda event, **kw: captured.append({"event": event, **kw})
    return client, captured


async def test_failed_call_carries_message_and_type():
    client, captured = make_client()
    client.capture_tool_call("add", is_error=True, error=ValueError("bad input"))
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert props[P.IS_ERROR] is True
    assert props[P.ERROR_MESSAGE] == "bad input"
    assert props[P.ERROR_TYPE] == "ValueError"


async def test_explicit_error_type_beats_the_thrown_class():
    """A custom dispatcher can pass a coarse category that means something to
    the product, where the exception class name usually doesn't."""
    client, captured = make_client()
    client.capture_tool_call(
        "add", is_error=True, error=ValueError("bad input"), error_type="validation"
    )
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert props[P.ERROR_TYPE] == "validation"
    assert props[P.ERROR_MESSAGE] == "bad input"


async def test_string_error_still_yields_both():
    client, captured = make_client()
    client.capture_tool_call("add", is_error=True, error="upstream timed out")
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert props[P.ERROR_MESSAGE] == "upstream timed out"
    assert props[P.ERROR_TYPE] == "Error"


async def test_successful_call_carries_neither():
    client, captured = make_client()
    client.capture_tool_call("add", response={"ok": True})
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert props[P.IS_ERROR] is False
    assert P.ERROR_MESSAGE not in props
    assert P.ERROR_TYPE not in props


async def test_the_exception_sibling_still_agrees():
    """Both surfaces read the same ``$exception_list``, so they can't disagree."""
    client, captured = make_client()
    client.capture_tool_call("add", is_error=True, error=RuntimeError("boom"))
    await _flush()

    call = _events(captured, "$mcp_tool_call")[0]["properties"]
    first = _events(captured, "$exception")[0]["properties"]["$exception_list"][0]
    assert call[P.ERROR_MESSAGE] == first["value"]
    assert call[P.ERROR_TYPE] == first["type"]


async def test_message_survives_the_sibling_being_disabled():
    """The whole point: with autocapture off there is no ``$exception`` event,
    so the primary event is the only place the failure reason can live."""
    client, captured = make_client(mcp_exception_autocapture=False)
    client.capture_tool_call("add", is_error=True, error=ValueError("still here"))
    await _flush()

    assert _events(captured, "$exception") == []
    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert props[P.ERROR_MESSAGE] == "still here"
    assert props[P.ERROR_TYPE] == "ValueError"


async def test_long_messages_are_bounded():
    client, captured = make_client()
    client.capture_tool_call("add", is_error=True, error=ValueError("x" * 5000))
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    message = props[P.ERROR_MESSAGE]
    # Truncation runs before the mapping, so the scalar inherits the existing
    # cap (2048 + the "..." marker) rather than adding an unbounded field.
    assert len(message) < 5000
    assert message.endswith("...")
    # ...and it is literally the same string the $exception sibling carries.
    first = _events(captured, "$exception")[0]["properties"]["$exception_list"][0]
    assert message == first["value"]


async def test_tools_list_failures_carry_them_too():
    client, captured = make_client()
    client.capture_tools_list(is_error=True, error=RuntimeError("listing failed"))
    await _flush()

    props = _events(captured, "$mcp_tools_list")[0]["properties"]
    assert props[P.ERROR_MESSAGE] == "listing failed"
    assert props[P.ERROR_TYPE] == "RuntimeError"


async def test_instrumented_server_failure_carries_them():
    """The same properties must land for a wrapped server, not just the manual
    dispatcher — that is the path most customers are on."""
    from posthog.test.mcp._helpers import MCP_MAJOR, FakeClient

    if MCP_MAJOR >= 2:
        from mcp.server.mcpserver import MCPServer as Server
    else:
        from mcp.server.fastmcp import FastMCP as Server

    from posthog.mcp import instrument

    server = Server("err-e2e")

    @server.tool()
    def boom() -> str:
        raise ValueError("explode")

    client = FakeClient()
    instrument(server, client)

    try:
        await server._tool_manager.call_tool("boom", {"context": "expected failure"})
    except Exception:
        pass
    await _flush()

    props = _events(client, "$mcp_tool_call")[0]["properties"]
    assert props[P.IS_ERROR] is True
    assert props[P.ERROR_TYPE]
    assert "explode" in props[P.ERROR_MESSAGE]


async def test_a_secret_in_the_message_is_redacted_on_both_surfaces():
    """An exception message is free text a server wrote, so it can carry the
    credential that caused the failure. It must be redacted before it leaves —
    on the new scalar *and* on the `$exception` sibling, which had been shipping
    it raw since before this property existed."""
    client, captured = make_client()
    client.capture_tool_call(
        "add",
        is_error=True,
        error=ValueError(
            "auth failed for token phc_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        ),
    )
    await _flush()

    message = _events(captured, "$mcp_tool_call")[0]["properties"][P.ERROR_MESSAGE]
    sibling = _events(captured, "$exception")[0]["properties"]["$exception_list"][0]

    assert "phc_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in message
    assert "phc_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in sibling["value"]
    # the frame's other fields are untouched — this is redaction, not deletion
    assert sibling["type"] == "ValueError"


async def test_a_non_posthog_credential_is_redacted_too():
    """The PostHog-token pattern only knows phc_/phx_. A failure message can
    carry someone else's key, so credential-looking words go through the SDK's
    own detector (entropy, known formats, PEM) — per word, so the diagnostic
    text around them survives."""
    client, captured = make_client()
    client.capture_tool_call(
        "add",
        is_error=True,
        error=ValueError("auth failed for sk-proj-abc123XYZ789defGHI456jklMNO012pqr"),
    )
    await _flush()

    message = _events(captured, "$mcp_tool_call")[0]["properties"][P.ERROR_MESSAGE]
    assert "sk-proj-" not in message
    assert message.startswith("auth failed for")  # the useful part survives


async def test_ordinary_error_text_is_left_alone():
    """The redactor must not eat normal failure messages."""
    client, captured = make_client()
    client.capture_tool_call(
        "add",
        is_error=True,
        error=RuntimeError("revenue warehouse unreachable (period=q3)"),
    )
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert props[P.ERROR_MESSAGE] == "revenue warehouse unreachable (period=q3)"
