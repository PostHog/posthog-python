"""Client conversation ids, per the spec's "Session identity" requirement: a
recognized client's own id in a tool call's ``_meta`` sets the session, ahead of
any handle or transport session. Every call lands on fresh server state, as on a
per-request stateless server. Session ids below are the spec's vectors, which
posthog-js reproduces too. Runs under both MCP majors."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from posthog.mcp import McpAnalytics, PostHogMCP
from posthog.mcp._client_conversation import (
    CLIENT_CONVERSATION_ROWS,
    ClientConversation,
    read_client_conversation,
    recognize_client,
)
from posthog.mcp._instrumentation import prepare_request, start_tool_call_lifecycle
from posthog.mcp._internal import MCPAnalyticsData, set_server_tracking_data
from posthog.mcp._posthog_events import build_posthog_capture_events
from posthog.mcp._sink import McpEventSink
from posthog.mcp.session import (
    derive_session_id_from_client_conversation,
    derive_session_id_from_mcp_session,
    new_session_id,
    resolve_session_id_with_source,
)
from posthog.mcp.session_token import SessionTokenPayload
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)
from posthog.test.mcp.test_initialize_handshake import BASE_HEADERS, KINDS, fleet, rpc

CHATGPT = "openai-mcp/1.0.0 (ChatGPT)"
CODEX = "codex-mcp-client/0.50.0"
OTHER = "example-client/1.0"
CHATGPT_ID = "v1/conv-example-0001"
CHATGPT_META = {"openai/session": CHATGPT_ID}
CHATGPT_SESSION = "ses_2f5d35b7108d17c807766f2f769a1e8c"
CODEX_ID = "019A4F2E-1C3D-7B8A-9E0F-1A2B3C4D5E6F"
CODEX_SESSION = "ses_0fdf126b8e2c1f9689ca12f3dd3383e6"
VSCODE_ID = "3f1c2b7e-8d4a-4c6b-9a1e-5f2d7c8b9a0e"
VSCODE_SESSION = "ses_0571acb894e6753d33e37e5c2b327c59"
HANDLE = "0190f0e8-7a6b-7c3d-9e4f-5a6b7c8d9e0f"
HANDLE_SESSION = "ses_6df45f0102a182bcd5e8dd5dad6c65a0"
VSCODE_CLIENT_INFO = {
    "io.modelcontextprotocol/clientInfo": {"name": "Visual Studio Code"},
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
}


class FakeServer:
    """A weak-referenceable stand-in for the server key tracking state hangs off."""


def http_extra(user_agent=None, **headers):
    if user_agent:
        headers["user-agent"] = user_agent
    return {"ctx": SimpleNamespace(request=SimpleNamespace(headers=headers))}


def fresh_data(client=None, enable_conversation_id=True):
    return MCPAnalyticsData(
        options=MCPAnalyticsOptions(enable_conversation_id=enable_conversation_id),
        sink=McpEventSink(client or FakeClient()),
        session_id=new_session_id(),
    )


def start_call(
    data,
    *,
    user_agent=None,
    meta=None,
    arguments=None,
    client_name=None,
    mcp_session_id=None,
    token=None,
):
    return start_tool_call_lifecycle(
        data,
        name="search_docs",
        arguments=arguments or {},
        request_meta=meta,
        allow_self_reported_model=True,
        mcp_session_id=mcp_session_id,
        token=token,
        client_name=client_name,
        client_version=None,
        protocol_version=None,
        extra=http_extra(user_agent),
    )


async def call_on_fresh_pod(enable_conversation_id=True, **call_input):
    client = FakeClient()
    call = start_call(fresh_data(client, enable_conversation_id), **call_input)
    await call.record_result(
        {"content": []},
        1.0,
        conversation_id_delivered=bool(call.minted_conversation_id),
    )
    await _flush()
    (event,) = _events(client, "$mcp_tool_call")
    return call, event["properties"]


@pytest.mark.parametrize(
    ("user_agent", "client_name", "key"),
    [
        ("openai-mcp/1.0.0 (ChatGPT)", None, "openai/session"),
        ("openai-mcp/1.0.0 ( chatgpt; web)", None, "openai/session"),
        ("openai-mcp/1.0.0 (Codex)", None, None),
        ("openai-mcp/1.0.0 (ChatGPT-Agent)", None, None),
        ("Mozilla/5.0 openai-mcp/1.0.0 (ChatGPT)", None, None),
        ("CODEX-MCP-CLIENT/0.50.0", None, "sessionId"),
        (None, "codex-mcp-client", "sessionId"),
        ("Visual Studio Code/1.105.0", None, "vscode.conversationId"),
        (None, "visual studio code - insiders", "vscode.conversationId"),
        (CHATGPT, "codex-mcp-client", "openai/session"),
        (OTHER, None, None),
    ],
)
def test_recognize_client(user_agent, client_name, key):
    row = recognize_client(user_agent, client_name)
    assert (row.key if row else None) == key


@pytest.mark.parametrize(
    ("row", "meta", "expected"),
    [
        (0, {"openai/session": "x" * 128}, "x" * 128),
        (0, {"openai/session": " ~"}, " ~"),
        (0, {"openai/session": "x" * 129}, None),
        (0, {"openai/session": ""}, None),
        (0, {"openai/session": "chat-é"}, None),
        (0, {"openai/session": "tab\there"}, None),
        (0, {"openai/session": 42}, None),
        (0, "openai/session", None),
        (1, {"sessionId": CODEX_ID}, CODEX_ID.lower()),
        (1, {"sessionId": "00000000-0000-0000-0000-000000000000"}, None),
        (1, {"sessionId": "not-a-uuid"}, None),
        (2, {"vscode.conversationId": "chat-1"}, None),
    ],
)
def test_read_client_conversation(row, meta, expected):
    conversation = read_client_conversation(CLIENT_CONVERSATION_ROWS[row], meta)
    assert (conversation.id if conversation else None) == expected


@pytest.mark.parametrize(
    ("key", "value", "session_id"),
    [
        ("openai/session", CHATGPT_ID, CHATGPT_SESSION),
        ("sessionId", CODEX_ID.lower(), CODEX_SESSION),
        ("vscode.conversationId", VSCODE_ID, VSCODE_SESSION),
    ],
)
def test_derivation_matches_the_spec_vectors(key, value, session_id):
    assert derive_session_id_from_client_conversation(key, value) == session_id


@pytest.mark.parametrize("enable_conversation_id", [True, False])
@pytest.mark.parametrize(
    ("call_input", "session_id"),
    [
        ({"user_agent": CHATGPT, "meta": CHATGPT_META}, CHATGPT_SESSION),
        ({"user_agent": CODEX, "meta": {"sessionId": CODEX_ID}}, CODEX_SESSION),
        (
            {"client_name": "codex-mcp-client", "meta": {"sessionId": CODEX_ID}},
            CODEX_SESSION,
        ),
        (
            {"meta": {**VSCODE_CLIENT_INFO, "vscode.conversationId": VSCODE_ID}},
            VSCODE_SESSION,
        ),
        (
            {
                "user_agent": "Visual Studio Code - Insiders/1.106.0",
                "meta": {"vscode.conversationId": VSCODE_ID},
            },
            VSCODE_SESSION,
        ),
    ],
    ids=["chatgpt", "codex ua", "codex name", "vscode meta name", "insiders ua"],
)
async def test_a_client_conversation_id_sets_the_session_without_a_handle(
    call_input, session_id, enable_conversation_id
):
    call, props = await call_on_fresh_pod(enable_conversation_id, **call_input)

    assert not call.minted_conversation_id
    assert call.virtual_result_texts("ok") == ["ok"]
    assert props["$session_id"] == session_id
    assert props["$mcp_session_source"] == "client_metadata"
    assert "$mcp_conversation_id" not in props


@pytest.mark.parametrize(
    "carried",
    [
        {"arguments": {"conversation_id": HANDLE}},
        {"mcp_session_id": "transport"},
        {"token": SessionTokenPayload(session_id="ses_token")},
    ],
    ids=["conversation handle", "transport session id", "session token"],
)
async def test_a_client_conversation_id_wins_over(carried):
    call, props = await call_on_fresh_pod(
        user_agent=CHATGPT, meta=CHATGPT_META, **carried
    )

    assert call.conversation_id is None
    assert props["$session_id"] == CHATGPT_SESSION
    assert props["$mcp_session_source"] == "client_metadata"
    assert "$mcp_conversation_id" not in props


@pytest.mark.parametrize(
    "meta",
    [
        {"sessionId": "not-a-uuid"},
        {"sessionId": "00000000-0000-0000-0000-000000000000"},
        None,
    ],
    ids=["not a uuid", "nil uuid", "absent"],
)
async def test_an_invalid_codex_id_falls_back_to_a_handle(meta):
    call, props = await call_on_fresh_pod(user_agent=CODEX, meta=meta)

    assert call.minted_conversation_id
    assert props["$mcp_session_source"] == "conversation"


@pytest.mark.parametrize(
    "meta",
    [{"openai/session": ""}, {"openai/session": "x" * 129}, None],
    ids=["empty", "too long", "absent"],
)
async def test_a_handle_free_client_is_minted_no_handle(meta):
    first, first_props = await call_on_fresh_pod(user_agent=CHATGPT, meta=meta)
    second, second_props = await call_on_fresh_pod(user_agent=CHATGPT, meta=meta)

    assert not first.minted_conversation_id and not second.minted_conversation_id
    assert first.virtual_result_texts("ok") == ["ok"]
    assert first_props["$session_id"] != second_props["$session_id"]
    assert first_props["$mcp_session_source"] == "generated"


async def test_a_handle_free_client_still_counts_an_echoed_handle():
    call, props = await call_on_fresh_pod(
        user_agent=CHATGPT, arguments={"conversation_id": HANDLE}
    )

    assert props["$session_id"] == HANDLE_SESSION
    assert not call.mirrors_output_instructions


async def test_a_key_is_read_only_from_the_client_that_defines_it():
    call, props = await call_on_fresh_pod(
        user_agent=OTHER, meta={"sessionId": CODEX_ID, **CHATGPT_META}
    )

    assert call.minted_conversation_id
    assert props["$session_id"] not in (CODEX_SESSION, CHATGPT_SESSION)


async def test_a_client_conversation_never_touches_shared_state():
    data = MCPAnalyticsData(options=MCPAnalyticsOptions(), session_id="ses_memory")
    before = (data.session_id, data.session_source, data.last_activity)

    resolved = await resolve_session_id_with_source(
        data,
        "transport",
        client_conversation=ClientConversation("openai/session", CHATGPT_ID),
    )

    assert resolved == (CHATGPT_SESSION, "client_metadata")
    assert (data.session_id, data.session_source, data.last_activity) == before


@pytest.mark.parametrize(
    ("gaps", "rotated"), [([20, 20], False), ([40, 1], True)], ids=["keeps", "rotates"]
)
async def test_calls_grouped_by_a_client_id_count_as_connection_activity(gaps, rotated):
    client = FakeClient()
    data = fresh_data(client, enable_conversation_id=False)

    async def call(meta=None):
        lifecycle = start_call(data, user_agent=CODEX, meta=meta)
        await lifecycle.prime_session()
        await lifecycle.record_result(
            {"content": []}, 1.0, conversation_id_delivered=False
        )

    await call()
    data.last_activity -= timedelta(minutes=gaps[0])
    await call({"sessionId": CODEX_ID})
    data.last_activity -= timedelta(minutes=gaps[1])
    await call()
    await _flush()

    first, grouped, last = (e["properties"] for e in _events(client, "$mcp_tool_call"))
    assert grouped["$session_id"] == CODEX_SESSION
    assert last["$mcp_session_source"] == "generated"
    assert (last["$session_id"] == first["$session_id"]) is not rotated


async def test_a_client_conversation_session_is_not_a_synthesized_handshake():
    client = FakeClient()

    await prepare_request(
        fresh_data(client),
        mcp_session_id=None,
        client_name=None,
        client_version=None,
        request={"method": "tools/call", "params": {}},
        extra={},
        client_conversation=ClientConversation("openai/session", CHATGPT_ID),
    )
    await _flush()

    assert _events(client, "$mcp_initialize") == []


@pytest.mark.parametrize(
    ("internal", "wire"),
    [
        ("conversation", "conversation"),
        ("token", "transport"),
        ("mcp", "transport"),
        ("client_metadata", "client_metadata"),
        ("generated", "generated"),
        (None, None),
    ],
)
def test_session_source_property_names_the_grouping_source(internal, wire):
    (capture,) = build_posthog_capture_events(
        {
            "event_type": "mcp:tools/call",
            "session_id": "ses_x",
            "session_source": internal,
        }
    )

    assert capture["properties"].get("$mcp_session_source") == wire


@pytest.mark.parametrize(
    ("session_source", "wire"),
    [("generated", "generated"), ("mcp", "transport"), ("token", "transport")],
)
async def test_custom_events_carry_the_instance_session_source(session_source, wire):
    client = FakeClient()
    key = FakeServer()
    set_server_tracking_data(
        key,
        MCPAnalyticsData(
            options=MCPAnalyticsOptions(),
            sink=McpEventSink(client),
            session_id="ses_instance",
            session_source=session_source,
        ),
    )

    await McpAnalytics(key).capture("custom_thing")

    (event,) = _events(client, "custom_thing")
    assert event["properties"]["$session_id"] == "ses_instance"
    assert event["properties"]["$mcp_session_source"] == wire


# --- PostHogMCP --------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "session_id", "minted"),
    [
        (
            {
                "headers": {"User-Agent": CHATGPT},
                "request_meta": CHATGPT_META,
                "session_id": "ses_x",
            },
            CHATGPT_SESSION,
            False,
        ),
        (
            {
                "client_info": {"name": "codex-mcp-client"},
                "request_meta": {"sessionId": CODEX_ID},
            },
            CODEX_SESSION,
            False,
        ),
        ({"headers": {"user-agent": CHATGPT}}, None, False),
        ({"request_meta": CHATGPT_META}, None, True),
        (
            {"headers": {"user-agent": CHATGPT}, "session_id": "ses_carried"},
            "ses_carried",
            False,
        ),
    ],
    ids=[
        "chatgpt id over a carried session",
        "codex id from the bound handshake",
        "handle-free client without its id",
        "chatgpt key without its user-agent",
        "carried session",
    ],
)
def test_prepare_tool_call(kwargs, session_id, minted):
    client = PostHogMCP("phc_test", host="https://us.i.posthog.com")

    call = client.prepare_tool_call("search_docs", {}, **kwargs)
    prepared = client.prepare_tool_result({"content": []}, call)

    assert ("conversation_id" in json.dumps(prepared.result)) is minted
    if not minted:
        assert prepared.session_id == session_id
        assert prepared.conversation_id is None


@pytest.mark.parametrize(
    ("user_agent", "session_id"),
    [(CHATGPT, CHATGPT_SESSION), (None, "ses_caller")],
    ids=["recognized", "unrecognized"],
)
async def test_capture_tool_call_applies_a_client_conversation_id(
    user_agent, session_id
):
    fake = FakeClient()
    client = PostHogMCP("phc_test", host="https://us.i.posthog.com")
    client.capture = fake.capture

    client.capture_tool_call(
        "search_docs",
        session_id="ses_caller",
        conversation_id=HANDLE,
        client_user_agent=user_agent,
        request_meta=CHATGPT_META,
    )
    await _flush()

    (event,) = _events(fake, "$mcp_tool_call")
    assert event["properties"]["$session_id"] == session_id
    assert ("$mcp_conversation_id" in event["properties"]) is (user_agent is None)


@pytest.mark.parametrize(
    ("user_agent", "advertised"), [(CHATGPT, False), (OTHER, True), (None, True)]
)
def test_prepare_tool_list_advertises_conversation_id(user_agent, advertised):
    client = PostHogMCP("phc_test", host="https://us.i.posthog.com")
    tool = {"name": "search_docs", "inputSchema": {"type": "object", "properties": {}}}

    (prepared,) = client.prepare_tool_list(
        [tool], headers={"user-agent": user_agent} if user_agent else None
    )

    assert ("conversation_id" in prepared["inputSchema"]["properties"]) is advertised


# --- over HTTP, on every server kind -------------------------------------------


def _headers(user_agent):
    return {**BASE_HEADERS, "user-agent": user_agent}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("user_agent", "advertised"), [(CHATGPT, False), (OTHER, True)]
)
async def test_only_a_handle_free_clients_listing_has_no_conversation_id(
    kind, user_agent, advertised
):
    client = FakeClient()
    options = MCPAnalyticsOptions(enable_conversation_id=True)
    async with fleet(client, pods=1, kind=kind, options=options) as lb:
        response = await lb.post(rpc("tools/list", {}), _headers(user_agent))
    assert response.status_code == 200, response.text

    (tool,) = [t for t in response.json()["result"]["tools"] if t["name"] == "add"]
    assert ("context" in tool["inputSchema"]["properties"]) is True
    assert ("conversation_id" in tool["inputSchema"]["properties"]) is advertised


@pytest.mark.parametrize("kind", KINDS)
async def test_chatgpt_calls_share_a_session_across_pods(kind):
    client = FakeClient()
    options = MCPAnalyticsOptions(enable_conversation_id=True)
    async with fleet(client, pods=2, kind=kind, options=options) as lb:
        responses = [
            await lb.post(
                rpc(
                    "tools/call",
                    {
                        "name": "add",
                        "arguments": {"a": 1, "b": 2, "conversation_id": HANDLE},
                        "_meta": CHATGPT_META,
                    },
                    request_id,
                ),
                _headers(CHATGPT),
            )
            for request_id in (1, 2)
        ]
    await _flush()

    for response in responses:
        assert response.status_code == 200, response.text
        assert not response.json()["result"].get("isError"), response.text
        assert "conversation_id" not in json.dumps(response.json()["result"]["content"])
    calls = _events(client, "$mcp_tool_call")
    assert [c["properties"]["$session_id"] for c in calls] == [CHATGPT_SESSION] * 2
    assert all("$mcp_conversation_id" not in c["properties"] for c in calls)
    assert all(
        "conversation_id" not in json.dumps(c["properties"].get("$mcp_parameters"))
        for c in calls
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_a_chatgpt_listing_reads_no_client_conversation(kind):
    """The spec reads client conversation ids from tool calls only."""
    client = FakeClient()
    async with fleet(client, pods=1, kind=kind) as lb:
        await lb.post(rpc("tools/list", {"_meta": CHATGPT_META}), _headers(CHATGPT))
    await _flush()

    (listing,) = _events(client, "$mcp_tools_list")
    assert listing["properties"]["$session_id"] != CHATGPT_SESSION
    assert listing["properties"]["$session_id"] != derive_session_id_from_mcp_session(
        CHATGPT_ID
    )
