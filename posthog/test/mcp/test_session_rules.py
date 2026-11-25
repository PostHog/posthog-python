from datetime import timedelta
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from posthog.mcp import McpAnalytics, PostHogMCP
from posthog.mcp._instrumentation import start_tools_list_lifecycle
from posthog.mcp._internal import set_server_tracking_data
from posthog.mcp import request_headers
from posthog.mcp.session import derive_session_id_from_mcp_session
from posthog.mcp.session_token import SessionTokenPayload, encode_session_id
from posthog.test.mcp._helpers import (
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


def _request_context(query=None, headers=None, client_params=None):
    return SimpleNamespace(
        request=SimpleNamespace(headers=headers or {}, query_params=query or {}),
        session=SimpleNamespace(client_params=client_params),
    )


HANDSHAKE = SimpleNamespace(protocolVersion="2025-06-18")
SSE_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


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
        (_request_context({"session_id": SSE_ID}), None),
        (
            _request_context(
                {"session_id": SSE_ID},
                client_params=SimpleNamespace(protocol_version="2026-07-28"),
            ),
            None,
        ),
        (SimpleNamespace(request=None, session=None), None),
        (None, None),
    ],
    ids=[
        "the Mcp-Session-Id header",
        "an SSE connection's query parameter",
        "an SSE connection's query parameter on mcp 2.x",
        "a query parameter on a stateless request",
        "a query parameter on a 2026-07-28 request",
        "stdio",
        "no context",
    ],
)
def test_the_transport_session_of_a_request(ctx, expected):
    assert request_headers.read_transport_session_id(ctx) == expected


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        ("transport-a", derive_session_id_from_mcp_session("transport-a")),
        (encode_session_id(SessionTokenPayload(session_id="ses_token")), "ses_token"),
        (None, None),
    ],
    ids=["a raw transport id", "a PostHog session token", "none"],
)
def test_the_manual_call_step_derives_the_transport_session(session_id, expected):
    posthog_mcp = PostHogMCP("phc_test", send=False, enable_conversation_id=False)

    call = posthog_mcp.prepare_tool_call("search_docs", {}, session_id=session_id)

    assert call.session_id == expected
