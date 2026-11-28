from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from freezegun import freeze_time

from posthog.mcp import McpAnalytics
from posthog.mcp._instrumentation import start_tools_list_lifecycle
from posthog.mcp._internal import set_server_tracking_data
from posthog.mcp import request_headers
from posthog.mcp.session import derive_session_id_from_mcp_session, handshake_session
from posthog.test.mcp._helpers import (
    MCP_MAJOR,
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)
from posthog.test.mcp.test_client_conversation_session import (
    CHATGPT,
    CODEX,
    OTHER,
    FakeServer,
    fresh_data,
    http_extra,
    run_call,
    shared_http_server,
    start_call,
)


def _sessions(client, name="$mcp_tool_call"):
    return [
        (e["properties"]["$session_id"], e["properties"]["$mcp_session_source"])
        for e in _events(client, name)
    ]


@pytest.mark.parametrize("user_agent", [OTHER, CHATGPT, CODEX])
@pytest.mark.parametrize(
    "earlier_transport", [None, "transport-a"], ids=["first", "after a transport call"]
)
async def test_sessionless_http_calls_each_get_their_own_session(
    user_agent, earlier_transport
):
    client = FakeClient()
    _, data = shared_http_server(client, enable_conversation_id=False)
    if earlier_transport:
        await run_call(data, user_agent=OTHER, mcp_session_id=earlier_transport)

    for user in ("alice", "bob"):
        await run_call(data, user_agent=user_agent, user=user)
    await _flush()

    alice, bob = _sessions(client)[-2:]
    assert alice[0] != bob[0]
    assert {alice[1], bob[1]} == {"generated"}
    assert derive_session_id_from_mcp_session("transport-a") not in (alice[0], bob[0])


async def test_sessionless_http_listings_each_get_their_own_session():
    client = FakeClient()
    _, data = shared_http_server(client)

    for _ in range(2):
        lifecycle = await start_tools_list_lifecycle(
            data,
            request={"method": "tools/list", "params": {}},
            extra=http_extra(OTHER),
            mcp_session_id=None,
            token=None,
            client_name=None,
            client_version=None,
            protocol_version=None,
        )
        await lifecycle.record_result(
            names=["search_docs"], result={"tools": []}, duration_ms=1.0, is_empty=False
        )
    await _flush()

    first, second = _sessions(client, "$mcp_tools_list")
    assert first[0] != second[0]


@pytest.mark.parametrize("outcome", ["result", "error"])
async def test_every_event_of_a_long_call_keeps_its_arrival_session(outcome):
    client = FakeClient()
    key = FakeServer()
    data = fresh_data(client, enable_conversation_id=False)
    set_server_tracking_data(key, data)

    call = start_call(data)
    arrival = await call.prime_session()
    with call.in_tool(arrival):
        await McpAnalytics(key).capture("inside_tool")
        data.last_activity -= timedelta(minutes=40)
    if outcome == "result":
        await call.record_result({"content": []}, 1.0, conversation_id_delivered=False)
    else:
        await call.record_error(RuntimeError("boom"), 1.0)
    await _flush()

    assert _sessions(client) == [(arrival.session_id, "generated")]
    assert _sessions(client, "inside_tool") == [(arrival.session_id, "generated")]


@pytest.mark.parametrize(
    ("call_minutes", "idle_minutes", "rotated"),
    [(20, 29, False), (0, 29, False), (0, 30, True), (20, 30, True)],
    ids=[
        "idle counts from the call's end",
        "under 30 idle minutes keeps it",
        "30 idle minutes rotates",
        "30 idle minutes after a long call rotates",
    ],
)
async def test_a_one_client_connection_rotates_after_30_idle_minutes(
    call_minutes, idle_minutes, rotated
):
    client = FakeClient()
    data = fresh_data(client, enable_conversation_id=False)

    with freeze_time("2026-11-25 12:00:00", real_asyncio=True) as clock:
        first = start_call(data)
        await first.prime_session()
        clock.tick(timedelta(minutes=call_minutes))
        await first.record_result({"content": []}, 1.0, conversation_id_delivered=False)
        clock.tick(timedelta(minutes=idle_minutes))
        await run_call(data)
    await _flush()

    (first_session, _), (second_session, _) = _sessions(client)
    assert (first_session != second_session) is rotated


async def test_the_first_call_of_a_connection_keeps_the_session_its_handshake_opened():
    client = FakeClient()

    with freeze_time("2026-11-25 12:00:00", real_asyncio=True) as clock:
        data = fresh_data(client, enable_conversation_id=False)
        await start_tools_list_lifecycle(
            data,
            request={"method": "tools/list", "params": {}},
            extra={},
            mcp_session_id=None,
            token=None,
            client_name=None,
            client_version=None,
            protocol_version=None,
        )
        listed = data.session_id
        clock.tick(timedelta(minutes=31))
        await run_call(data)
    await _flush()

    assert _sessions(client) == [(listed, "generated")]


async def test_a_handshake_over_http_with_no_session_gets_its_own_and_leaves_the_shared_one():
    data = fresh_data(enable_conversation_id=False)
    shared = data.session_id

    resolved = await handshake_session(data, None, None, over_http=True)

    assert resolved.source == "request"
    assert resolved.session_id != shared
    assert data.session_id == shared


async def test_a_listing_does_not_keep_an_idle_connection_alive():
    client = FakeClient()
    data = fresh_data(client, enable_conversation_id=False)

    with freeze_time("2026-11-25 12:00:00", real_asyncio=True) as clock:
        await run_call(data)
        clock.tick(timedelta(minutes=29))
        await start_tools_list_lifecycle(
            data,
            request={"method": "tools/list", "params": {}},
            extra={},
            mcp_session_id=None,
            token=None,
            client_name=None,
            client_version=None,
            protocol_version=None,
        )
        clock.tick(timedelta(minutes=29))
        await run_call(data)
    await _flush()

    (first, _), (second, _) = _sessions(client)
    assert first != second


@pytest.mark.skipif(MCP_MAJOR < 2, reason="2026-07-28 requests need mcp 2.x")
async def test_a_2026_07_28_request_does_not_ask_for_the_session_middleware():
    from posthog.test.mcp._helpers_v2 import modern_headers, modern_meta
    from posthog.test.mcp.test_initialize_handshake import BASE_HEADERS, call_add, fleet

    client = FakeClient()
    headers = {**BASE_HEADERS, **modern_headers("tools/call", "add")}
    with mock.patch("posthog.mcp._instrumentation.warn") as warn:
        async with fleet(client, pods=1) as lb:
            await call_add(lb, headers, 1, _meta=modern_meta())
        await _flush()

    assert _sessions(client)[0][1] == "generated"
    warn.assert_not_called()


def _request_context(query=None, headers=None, client_params=None):
    return SimpleNamespace(
        request=SimpleNamespace(headers=headers or {}, query_params=query or {}),
        session=SimpleNamespace(client_params=client_params),
    )


HANDSHAKE = SimpleNamespace(protocolVersion="2025-06-18")
SSE_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
SSE_SPELLINGS = [
    "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
    "0F1E2D3C4B5A69788796A5B4C3D2E1F0",
    "{0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0}",
    "urn:uuid:0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
]


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (_request_context(headers={"mcp-session-id": "header-id"}), "header-id"),
        (_request_context({"session_id": SSE_ID}, client_params=HANDSHAKE), SSE_ID),
        (
            _request_context(
                {"session_id": SSE_ID},
                client_params=SimpleNamespace(protocol_version="2025-11-25"),
            ),
            SSE_ID,
        ),
        (
            _request_context(
                {"session_id": SSE_ID},
                client_params=SimpleNamespace(protocolVersion="2026-07-28"),
            ),
            SSE_ID,
        ),
        (_request_context({"session_id": SSE_ID}), None),
        (
            _request_context(
                {"session_id": SSE_ID},
                client_params=SimpleNamespace(protocol_version="2026-07-28"),
            ),
            None,
        ),
        *[
            (_request_context({"session_id": spelling}, client_params=HANDSHAKE), SSE_ID)
            for spelling in SSE_SPELLINGS
        ],
        (_request_context({"session_id": "not-a-uuid"}, client_params=HANDSHAKE), None),
        (SimpleNamespace(request=None, session=None), None),
        (None, None),
    ],
    ids=[
        "the Mcp-Session-Id header",
        "an SSE connection's query parameter",
        "an SSE connection's query parameter on mcp 2.x",
        "a 2026-07-28 request that mcp 1.x negotiated down",
        "a query parameter on a stateless request",
        "a query parameter on a 2026-07-28 request",
        *[f"the SSE spelling {spelling!r}" for spelling in SSE_SPELLINGS],
        "an SSE query parameter that is not a UUID",
        "stdio",
        "no context",
    ],
)
def test_the_transport_session_of_a_request(ctx, expected):
    assert request_headers.read_transport_session_id(ctx) == expected
