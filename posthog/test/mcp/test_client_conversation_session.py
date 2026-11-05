"""A host client's own conversation id in request ``_meta`` (ChatGPT's
``openai/session``) groups stateless calls without touching tool schemas or
results (posthog-js ADR-0014). Every call lands on a fresh server state, as on a
per-request stateless server. Runs under both MCP majors."""

import pytest

from posthog.mcp import PostHogMCP
from posthog.mcp._instrumentation import prepare_request, start_tool_call_lifecycle
from posthog.mcp._internal import MCPAnalyticsData
from posthog.mcp._posthog_events import build_posthog_capture_events
from posthog.mcp._sink import McpEventSink
from posthog.mcp.session import (
    ClientConversation,
    derive_session_id_from_client_conversation,
    derive_session_id_from_conversation,
    derive_session_id_from_mcp_session,
    new_session_id,
    read_client_conversation,
    resolve_session_id_with_source,
)
from posthog.mcp.session_token import SessionTokenPayload
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)

CHATGPT_SESSION = "v1/aBcDeF123"
CHATGPT_META = {"openai/session": CHATGPT_SESSION}
CHATGPT_SESSION_ID = derive_session_id_from_client_conversation(
    "openai/session", CHATGPT_SESSION
)
HANDLE = "019fd2b0-4444-7444-8444-444444444444"


async def call_on_fresh_pod(
    *,
    meta=None,
    arguments=None,
    mcp_session_id=None,
    token=None,
    enable_conversation_id=True,
    conversation_id_delivered=False,
):
    client = FakeClient()
    data = MCPAnalyticsData(
        options=MCPAnalyticsOptions(enable_conversation_id=enable_conversation_id),
        sink=McpEventSink(client),
        session_id=new_session_id(),
    )
    call = start_tool_call_lifecycle(
        data,
        name="get_plan",
        arguments=arguments or {},
        request_meta=meta,
        allow_self_reported_model=True,
        mcp_session_id=mcp_session_id,
        token=token,
        client_name=None,
        client_version=None,
        protocol_version=None,
        extra={},
    )
    await call.record_result(
        {"content": []},
        1.0,
        conversation_id_delivered=conversation_id_delivered,
    )
    await _flush()
    (event,) = _events(client, "$mcp_tool_call")
    return call, event["properties"]


def test_derivation_matches_the_typescript_sdk_byte_for_byte():
    # Cross-SDK contract (posthog-js ADR-0014): change both SDKs in lockstep.
    assert CHATGPT_SESSION_ID == "ses_35e4cc45ea3e5e1282c3f091de632132"


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (CHATGPT_META, ClientConversation("openai/session", CHATGPT_SESSION)),
        (
            {"openai/session": "x" * 256},
            ClientConversation("openai/session", "x" * 256),
        ),
        ({"openai/session": " ~"}, ClientConversation("openai/session", " ~")),
        (None, None),
        ("openai/session", None),
        ({"openai/session": ""}, None),
        ({"openai/session": 42}, None),
        ({"openai/session": None}, None),
        ({"openai/session": "x" * 257}, None),
        ({"openai/session": "chat-é"}, None),
        ({"openai/session": "tab\there"}, None),
        ({"sessionId": CHATGPT_SESSION}, None),
    ],
    ids=[
        "chatgpt",
        "longest",
        "printable bounds",
        "absent",
        "not a mapping",
        "empty",
        "not a string",
        "null",
        "too long",
        "non-ascii",
        "control character",
        "unrecognized key",
    ],
)
def test_read_client_conversation(meta, expected):
    assert read_client_conversation(meta) == expected


@pytest.mark.parametrize("enable_conversation_id", [True, False])
async def test_fresh_instances_share_a_session_and_results_stay_unchanged(
    enable_conversation_id,
):
    first, first_props = await call_on_fresh_pod(
        meta=CHATGPT_META, enable_conversation_id=enable_conversation_id
    )
    _, second_props = await call_on_fresh_pod(
        meta=CHATGPT_META, enable_conversation_id=enable_conversation_id
    )

    assert first_props["$session_id"] == CHATGPT_SESSION_ID
    assert second_props["$session_id"] == CHATGPT_SESSION_ID
    assert first_props["$mcp_session_source"] == "client_metadata"
    assert "$mcp_conversation_id" not in first_props
    assert not first.minted_conversation_id
    assert first.conversation_id is None
    assert first.virtual_result_texts("ok") == ["ok"]


async def test_different_client_conversations_get_different_sessions():
    _, first = await call_on_fresh_pod(meta={"openai/session": "chat-a"})
    _, second = await call_on_fresh_pod(meta={"openai/session": "chat-b"})

    assert first["$session_id"] != second["$session_id"]


@pytest.mark.parametrize(
    ("carried", "session_id", "source"),
    [
        (
            {"arguments": {"conversation_id": HANDLE}},
            derive_session_id_from_conversation(HANDLE),
            "conversation",
        ),
        (
            {"token": SessionTokenPayload(session_id="ses_token")},
            "ses_token",
            "transport",
        ),
        (
            {"mcp_session_id": "transport-session"},
            derive_session_id_from_mcp_session("transport-session"),
            "transport",
        ),
    ],
    ids=["conversation handle", "session token", "transport session id"],
)
async def test_carried_session_wins_over_the_client_conversation(
    carried, session_id, source
):
    _, props = await call_on_fresh_pod(meta=CHATGPT_META, **carried)

    assert props["$session_id"] == session_id
    assert props["$mcp_session_source"] == source


@pytest.mark.parametrize(
    "meta",
    [
        None,
        {"openai/session": ""},
        {"openai/session": 42},
        {"openai/session": "x" * 257},
        {"openai/session": "chat-é"},
        {"sessionId": CHATGPT_SESSION},
    ],
    ids=["absent", "empty", "not a string", "too long", "non-ascii", "other key"],
)
async def test_invalid_client_conversation_is_ignored(meta):
    _, first = await call_on_fresh_pod(meta=meta, enable_conversation_id=False)
    _, second = await call_on_fresh_pod(meta=meta, enable_conversation_id=False)

    assert first["$session_id"] != second["$session_id"]
    assert first["$mcp_session_source"] == "generated"


async def test_a_call_with_no_client_conversation_still_mints_a_handle():
    call, props = await call_on_fresh_pod(conversation_id_delivered=True)

    assert call.minted_conversation_id
    assert call.virtual_result_texts("ok") != ["ok"]
    assert props["$mcp_session_source"] == "conversation"


async def test_client_conversation_never_touches_shared_state():
    data = MCPAnalyticsData(options=MCPAnalyticsOptions(), session_id="ses_memory")
    before = (data.session_id, data.session_source, data.last_activity)

    resolved = await resolve_session_id_with_source(
        data,
        None,
        client_conversation=ClientConversation("openai/session", CHATGPT_SESSION),
    )

    assert resolved == (CHATGPT_SESSION_ID, "client_metadata")
    assert (data.session_id, data.session_source, data.last_activity) == before


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
    ("kwargs", "session_id"),
    [
        ({"request_meta": CHATGPT_META}, CHATGPT_SESSION_ID),
        (
            {"request_meta": CHATGPT_META, "session_id": "ses_carried"},
            "ses_carried",
        ),
        (
            {"request_meta": CHATGPT_META, "args": {"conversation_id": HANDLE}},
            derive_session_id_from_conversation(HANDLE),
        ),
    ],
    ids=["client conversation", "explicit session", "echoed handle"],
)
def test_prepare_tool_call_groups_by_the_client_conversation(kwargs, session_id):
    client = PostHogMCP("phc_test", host="https://us.i.posthog.com")
    args = kwargs.pop("args", {})

    call = client.prepare_tool_call("get_plan", args, **kwargs)
    prepared = client.prepare_tool_result({"content": []}, call)

    assert prepared.session_id == session_id
    assert prepared.result == {"content": []}
    expected_handle = HANDLE if args else None
    assert prepared.conversation_id == expected_handle


@pytest.mark.parametrize(
    ("client_conversation", "initialize_events"),
    [(ClientConversation("openai/session", CHATGPT_SESSION), 0), (None, 1)],
    ids=["client conversation", "generated"],
)
async def test_client_conversation_session_is_not_a_synthesized_handshake(
    client_conversation, initialize_events
):
    client = FakeClient()
    data = MCPAnalyticsData(
        options=MCPAnalyticsOptions(),
        sink=McpEventSink(client),
        session_id=new_session_id(),
    )

    await prepare_request(
        data,
        mcp_session_id=None,
        client_name=None,
        client_version=None,
        request={"method": "tools/call", "params": {}},
        extra={},
        client_conversation=client_conversation,
    )
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == initialize_events
