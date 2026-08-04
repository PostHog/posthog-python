"""End-to-end tests for the mcp 2.x (2026-07-28 spec) middleware adapter.

Drives a real `MCPServer` over the SDK's in-memory transport so the adapter is
exercised through the official `ServerMiddleware` seam exactly as production
traffic hits it: `server/discover`, `tools/list`, and `tools/call` all flow
through `instrument()`'s attached middleware. Runs only in the mcp-v2 env."""

import pytest

from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    requires_mcp_v2,
)

pytestmark = requires_mcp_v2

# `mcp.server.mcpserver` is the mcp 2.x high-level server module; it doesn't exist
# in mcp 1.x, so importing it would crash collection there. Skip the module before
# that import runs (the marker above additionally documents intent).
pytest.importorskip("mcp.server.mcpserver")

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.client._memory import InMemoryTransport  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402

from mcp_types import InputRequiredResult  # noqa: E402

from posthog.mcp import instrument  # noqa: E402
from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity  # noqa: E402


def make_server(name="v2-probe"):
    server = MCPServer(name)

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    @server.tool()
    def boom() -> int:
        raise ValueError("kaboom")

    return server


async def drive(
    server, *, modern=True, calls=(("add", {"a": 2, "b": 3}),), list_tools=True
):
    """Connect an in-memory client, run a modern (server/discover) or legacy
    (initialize) handshake, then list tools and make the given tool calls.
    Returns the collected client-side results in call order."""
    results = []
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            if modern:
                await session.discover()
            else:
                await session.initialize()
            if list_tools:
                await session.list_tools()
            for name, args in calls:
                results.append(await session.call_tool(name, args))
    return results


async def test_tool_call_captured_with_envelope_identity():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await drive(server, calls=(("add", {"a": 2, "b": 3}),))

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "add"
    assert props["$mcp_is_error"] is False
    # Client identity + protocol version come from the per-request _meta envelope.
    assert props["$mcp_client_name"] == "mcp"
    assert props["$mcp_protocol_version"] == "2026-07-28"


async def test_tool_error_result_captured_as_error():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await drive(server, calls=(("boom", {}),))

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    exceptions = _events(client, "$exception")
    assert exceptions, "an isError tool result should also emit $exception"


async def test_tools_list_captured_with_names():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await drive(server, calls=())

    listed = _events(client, "$mcp_tools_list")
    assert listed
    names = listed[0]["properties"]["$mcp_listed_tool_names"]
    assert "add" in names and "boom" in names


async def test_initialize_emitted_once_per_session():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await drive(server, calls=(("add", {"a": 1, "b": 1}), ("add", {"a": 2, "b": 2})))

    assert len(_events(client, "$mcp_initialize")) == 1
    assert len(_events(client, "$mcp_tool_call")) == 2


async def test_identify_flows_through_v2_adapter():
    server = make_server()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            identify=UserIdentity(distinct_id="user-42", properties={"plan": "pro"})
        ),
    )

    await drive(server, calls=(("add", {"a": 1, "b": 1}),))

    identifies = _events(client, "$identify")
    assert identifies and identifies[0]["distinct_id"] == "user-42"
    # The tool call is attributed to the identified user, not the anonymous session.
    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["distinct_id"] == "user-42"


async def test_mrtr_input_required_stamps_result_type_not_error():
    # An MRTR interim result (`resultType: "input_required"`) is a continuation,
    # not a failure: it must NOT be flagged as an error, and it stamps
    # `$mcp_result_type` so downstream can segment MRTR calls. Full round-trip
    # stitching is out of scope.
    server = MCPServer("mrtr")

    @server.tool()
    def needs_input() -> InputRequiredResult:
        return InputRequiredResult(input_requests={}, request_state="s1")

    client = FakeClient()
    instrument(server, client)

    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.discover()
            await session.call_tool("needs_input", {}, allow_input_required=True)

    calls = _events(client, "$mcp_tool_call")
    assert calls
    props = calls[0]["properties"]
    assert props["$mcp_is_error"] is False
    assert props["$mcp_result_type"] == "input_required"


@pytest.mark.parametrize(
    "identify, expected_source",
    [
        (UserIdentity(distinct_id="u1"), "derived"),
        (None, "generated"),
    ],
)
async def test_session_id_source_on_every_event(identify, expected_source):
    # v2 traffic has no token and no Mcp-Session-Id header, so an identified
    # request derives a stable session and an anonymous one gets a generated one.
    # Every $mcp_* event (and $identify) carries the provenance.
    server = make_server()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(identify=identify) if identify else None,
    )

    await drive(server, calls=(("add", {"a": 1, "b": 1}),))

    names = ["$mcp_initialize", "$mcp_tools_list", "$mcp_tool_call"]
    if identify:
        names.append("$identify")
    for name in names:
        events = _events(client, name)
        assert events, f"expected a {name} event"
        assert events[0]["properties"]["$mcp_session_id_source"] == expected_source


async def test_legacy_negotiation_on_v2_sdk_still_captures():
    # A client that runs the classic `initialize` handshake against the v2 SDK
    # negotiates an older protocol: the _meta envelope is absent, so client info
    # must be recovered from the initialize params instead. The adapter must
    # still capture the tool call (client name may be backfilled or absent, but
    # the event must fire and not be an error).
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await drive(server, modern=False, calls=(("add", {"a": 4, "b": 5}),))

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is False
