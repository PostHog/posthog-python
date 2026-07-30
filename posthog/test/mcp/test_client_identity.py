"""Client identity read from a request's ``_meta`` (MCP 2026-07-28 revision).

The 2026-07-28 stateless revision drops the ``initialize`` handshake and the
``Mcp-Session-Id`` header, so the client name/version and protocol version
arrive in every request's ``params._meta`` instead. These tests cover the
precedence rules against the session token and end-to-end passes through both
adapters.
"""

import mcp.types as mcp_types

from posthog.mcp import instrument
from posthog.mcp._client_identity import (
    META_CLIENT_INFO_KEY,
    META_PROTOCOL_VERSION_KEY,
    apply_meta_client_info,
)
from posthog.mcp._instrumentation import resolve_session_and_client
from posthog.mcp.session_token import SessionTokenPayload, encode_session_id
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)

FULL_META = {
    META_CLIENT_INFO_KEY: {"name": "codex", "version": "1.2.3"},
    META_PROTOCOL_VERSION_KEY: "2026-07-28",
}


def call_request(meta=None):
    """A real ``CallToolRequest``, so we exercise the pydantic ``extra="allow"``
    path the SDK actually hands us rather than a hand-rolled stand-in."""
    params = {"name": "echo", "arguments": {"a": 1}}
    if meta is not None:
        params["_meta"] = meta
    return mcp_types.CallToolRequest.model_validate(
        {"method": "tools/call", "params": params}
    )


def apply(meta, name=None, version=None, protocol=None):
    return apply_meta_client_info(call_request(meta), name, version, protocol)


# --- reading -----------------------------------------------------------------


def test_reads_client_info_and_protocol_version_from_meta():
    assert apply(FULL_META) == ("codex", "1.2.3", "2026-07-28")


def test_ignores_a_request_without_meta():
    assert apply(None, "legacy", "0.0.1", "2025-11-25") == (
        "legacy",
        "0.0.1",
        "2025-11-25",
    )


def test_ignores_meta_without_the_recognized_keys():
    assert apply({"com.other/thing": 1}) == (None, None, None)


def test_ignores_empty_and_non_string_fields():
    """A client sending `""` or a non-string must not blank out a good value."""
    meta = {
        META_CLIENT_INFO_KEY: {"name": "", "version": 42},
        META_PROTOCOL_VERSION_KEY: "",
    }
    assert apply(meta, "legacy", "0.0.1", "2025-11-25") == (
        "legacy",
        "0.0.1",
        "2025-11-25",
    )


def test_only_overrides_the_fields_the_request_carries():
    assert apply(
        {META_PROTOCOL_VERSION_KEY: "2026-07-28"}, "legacy", "0.0.1", "2025-11-25"
    ) == ("legacy", "0.0.1", "2026-07-28")


def test_progress_token_alongside_client_info_is_ignored():
    """`progressToken` is a declared field, so it lands outside `model_extra` —
    make sure its presence doesn't shadow the reverse-DNS keys."""
    assert apply({**FULL_META, "progressToken": "p1"}) == (
        "codex",
        "1.2.3",
        "2026-07-28",
    )


def test_reads_from_a_list_tools_request():
    req = mcp_types.ListToolsRequest.model_validate(
        {"method": "tools/list", "params": {"_meta": FULL_META}}
    )
    assert apply_meta_client_info(req, None, None, None)[0] == "codex"


def test_reads_from_a_request_context():
    """The FastMCP call path passes a ``RequestContext``, which carries the meta
    directly rather than nesting it under ``params``."""

    class Ctx:
        meta = mcp_types.RequestParams.Meta.model_validate(FULL_META)

    assert apply_meta_client_info(Ctx(), None, None, None) == (
        "codex",
        "1.2.3",
        "2026-07-28",
    )


def test_never_raises_on_an_unusable_source():
    assert apply_meta_client_info(None, "legacy", None, None) == ("legacy", None, None)
    assert apply_meta_client_info(object(), "legacy", None, None) == (
        "legacy",
        None,
        None,
    )


# --- precedence --------------------------------------------------------------


def test_meta_wins_over_the_session_token():
    """A stale token from an earlier session must not shadow the identity the
    client is asserting on this request."""
    token = encode_session_id(
        SessionTokenPayload(
            session_id="ses_abc",
            client_name="stale-client",
            client_version="0.0.1",
            protocol_version="2025-11-25",
        )
    )
    decoded, name, version, protocol = resolve_session_and_client(
        token, None, None, None, meta_source=call_request(FULL_META)
    )
    assert decoded is not None and decoded.session_id == "ses_abc"
    assert (name, version, protocol) == ("codex", "1.2.3", "2026-07-28")


def test_token_still_backfills_when_meta_is_absent():
    token = encode_session_id(
        SessionTokenPayload(
            session_id="ses_abc",
            client_name="claude",
            client_version="2.0.0",
            protocol_version="2025-11-25",
        )
    )
    _, name, version, protocol = resolve_session_and_client(
        token, None, None, None, meta_source=call_request()
    )
    assert (name, version, protocol) == ("claude", "2.0.0", "2025-11-25")


def test_two_concurrent_requests_do_not_cross_attribute():
    """Identity is resolved per request, so one client's `_meta` can never leak
    into a sibling request on the same multiplexed server."""
    a = apply({META_CLIENT_INFO_KEY: {"name": "codex", "version": "1.0.0"}})
    b = apply({META_CLIENT_INFO_KEY: {"name": "claude", "version": "2.0.0"}})
    assert a[0] == "codex"
    assert b[0] == "claude"


# --- end to end --------------------------------------------------------------


async def test_low_level_tool_call_stamps_meta_identity_on_events():
    """No initialize, no session token — the 2026-07-28 shape. Client identity
    must still reach the captured event."""
    from mcp.server.lowlevel import Server

    server = Server("test-server")

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        return [mcp_types.TextContent(type="text", text="ok")]

    client = FakeClient()
    instrument(server, client)

    handler = server.request_handlers[mcp_types.CallToolRequest]
    await handler(call_request(FULL_META))
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls
    props = calls[0]["properties"]
    assert props["$mcp_client_name"] == "codex"
    assert props["$mcp_client_version"] == "1.2.3"
    assert props["$mcp_protocol_version"] == "2026-07-28"

    # The synthesized initialize for the same session carries it too.
    init = _events(client, "$mcp_initialize")
    assert init
    assert init[0]["properties"]["$mcp_client_name"] == "codex"


async def test_fastmcp_tool_call_stamps_meta_identity_from_request_context():
    """The FastMCP call path has no request object — only a ``Context``."""
    from mcp.server.fastmcp import FastMCP

    class StubContext:
        """Stands in for a FastMCP ``Context``; only ``request_context.meta`` is
        read on this path."""

        class request_context:  # noqa: N801 - mirrors the attribute it fakes
            meta = mcp_types.RequestParams.Meta.model_validate(FULL_META)

    server = FastMCP("test-server")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    client = FakeClient()
    instrument(server, client)

    await server._tool_manager.call_tool(
        "add",
        {"a": 2, "b": 3, "context": "summing for the report"},
        context=StubContext(),
    )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls
    props = calls[0]["properties"]
    assert props["$mcp_client_name"] == "codex"
    assert props["$mcp_client_version"] == "1.2.3"
    assert props["$mcp_protocol_version"] == "2026-07-28"


async def test_fastmcp_tool_call_survives_a_context_used_off_request():
    """`Context.request_context` raises off-request; that must not surface as a
    tool-call failure."""
    from mcp.server.fastmcp import FastMCP

    class ExplodingContext:
        @property
        def request_context(self):
            raise ValueError("Context is not available outside of a request")

    server = FastMCP("test-server")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    client = FakeClient()
    instrument(server, client)

    result = await server._tool_manager.call_tool(
        "add", {"a": 2, "b": 3}, context=ExplodingContext()
    )
    await _flush()

    assert result == 5
    assert _events(client, "$mcp_tool_call")
