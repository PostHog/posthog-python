"""Client identity read from a request's ``_meta`` (MCP 2026-07-28 revision).

The 2026-07-28 stateless revision drops the ``initialize`` handshake and the
``Mcp-Session-Id`` header, so client name/version and protocol version arrive in
every request's ``params._meta`` instead. These tests cover the reader, the
precedence rules against the session token, and an end-to-end pass through the
low-level adapter.
"""

import mcp.types as mcp_types

from posthog.mcp._client_identity import (
    META_CLIENT_INFO_KEY,
    META_PROTOCOL_VERSION_KEY,
    apply_meta_client_info,
    read_meta_client_info,
)
from posthog.mcp._instrumentation import resolve_session_and_client
from posthog.mcp.session_token import encode_session_id, SessionTokenPayload
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
    """A real CallToolRequest, so we exercise the pydantic `extra="allow"` path
    the SDK actually hands us rather than a hand-rolled stand-in."""
    params = {"name": "echo", "arguments": {"a": 1}}
    if meta is not None:
        params["_meta"] = meta
    return mcp_types.CallToolRequest.model_validate(
        {"method": "tools/call", "params": params}
    )


# --- read_meta_client_info ---------------------------------------------------


def test_reads_client_info_and_protocol_version_from_meta():
    info = read_meta_client_info(call_request(FULL_META))
    assert info.client_name == "codex"
    assert info.client_version == "1.2.3"
    assert info.protocol_version == "2026-07-28"


def test_returns_none_when_meta_absent():
    assert read_meta_client_info(call_request()) is None
    assert read_meta_client_info(None) is None
    assert read_meta_client_info({"method": "tools/call"}) is None


def test_returns_none_when_meta_has_no_recognized_keys():
    assert read_meta_client_info(call_request({"com.other/thing": 1})) is None


def test_ignores_empty_and_non_string_fields():
    info = read_meta_client_info(
        call_request(
            {
                META_CLIENT_INFO_KEY: {"name": "", "version": 42},
                META_PROTOCOL_VERSION_KEY: "",
            }
        )
    )
    assert info is None


def test_reads_a_partial_protocol_version_only():
    info = read_meta_client_info(
        call_request({META_PROTOCOL_VERSION_KEY: "2026-07-28"})
    )
    assert info.protocol_version == "2026-07-28"
    assert info.client_name is None
    assert info.client_version is None


def test_progress_token_alongside_client_info_is_ignored():
    """`progressToken` is a declared field, so it lands outside `model_extra` —
    make sure its presence doesn't shadow the reverse-DNS keys."""
    info = read_meta_client_info(call_request({**FULL_META, "progressToken": "p1"}))
    assert info.client_name == "codex"
    assert info.protocol_version == "2026-07-28"


def test_reads_from_a_list_tools_request():
    req = mcp_types.ListToolsRequest.model_validate(
        {"method": "tools/list", "params": {"_meta": FULL_META}}
    )
    assert read_meta_client_info(req).client_name == "codex"


def test_reads_from_plain_dict_shapes():
    """The reader takes whatever the call sites have on hand: a raw JSON-RPC dict
    (`_meta`), a `request_to_dict()` dump (which drops the alias to `meta`), or
    the meta mapping itself."""
    assert (
        read_meta_client_info({"params": {"_meta": FULL_META}}).client_name == "codex"
    )
    assert read_meta_client_info({"params": {"meta": FULL_META}}).client_name == "codex"
    assert read_meta_client_info(FULL_META).client_name == "codex"


def test_reads_through_a_request_context_like_object():
    class Ctx:
        def __init__(self, meta):
            self.meta = meta

    class Context:
        def __init__(self, meta):
            self.request_context = Ctx(meta)

    assert read_meta_client_info(Context(FULL_META)).client_name == "codex"


def test_never_raises_on_a_hostile_source():
    class Exploding:
        @property
        def meta(self):
            raise RuntimeError("off-request access")

    assert read_meta_client_info(Exploding()) is None


# --- precedence --------------------------------------------------------------


def test_meta_overrides_transport_derived_values():
    name, version, protocol = apply_meta_client_info(
        call_request(FULL_META), "legacy-client", "0.0.1", "2025-11-25"
    )
    assert (name, version, protocol) == ("codex", "1.2.3", "2026-07-28")


def test_absent_meta_leaves_existing_values_untouched():
    name, version, protocol = apply_meta_client_info(
        call_request(), "legacy-client", "0.0.1", "2025-11-25"
    )
    assert (name, version, protocol) == ("legacy-client", "0.0.1", "2025-11-25")


def test_partial_meta_only_overrides_what_it_carries():
    name, version, protocol = apply_meta_client_info(
        call_request({META_PROTOCOL_VERSION_KEY: "2026-07-28"}),
        "legacy-client",
        "0.0.1",
        "2025-11-25",
    )
    assert (name, version, protocol) == ("legacy-client", "0.0.1", "2026-07-28")


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
    a = apply_meta_client_info(
        call_request({META_CLIENT_INFO_KEY: {"name": "codex", "version": "1.0.0"}}),
        None,
        None,
        None,
    )
    b = apply_meta_client_info(
        call_request({META_CLIENT_INFO_KEY: {"name": "claude", "version": "2.0.0"}}),
        None,
        None,
        None,
    )
    assert a[0] == "codex"
    assert b[0] == "claude"


# --- end to end --------------------------------------------------------------


async def test_low_level_tool_call_stamps_meta_identity_on_events():
    """No initialize, no session token — the 2026-07-28 shape. Client identity
    must still reach the captured event."""
    from mcp.server.lowlevel import Server

    from posthog.mcp import instrument

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
    """The FastMCP call path has no request object — only a ``Context`` — so it
    reads ``_meta`` off ``context.request_context``."""
    from mcp.server.fastmcp import FastMCP

    from posthog.mcp import instrument

    class StubRequestContext:
        def __init__(self, meta):
            self.meta = meta

    class StubContext:
        def __init__(self, meta):
            self.request_context = StubRequestContext(meta)

    server = FastMCP("test-server")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    client = FakeClient()
    instrument(server, client)

    await server._tool_manager.call_tool(
        "add",
        {"a": 2, "b": 3, "context": "summing for the report"},
        context=StubContext(FULL_META),
    )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls
    props = calls[0]["properties"]
    assert props["$mcp_client_name"] == "codex"
    assert props["$mcp_client_version"] == "1.2.3"
    assert props["$mcp_protocol_version"] == "2026-07-28"
