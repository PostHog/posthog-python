"""Tests for the PostHogMCP custom-dispatcher client (Milestone 3)."""

import json
import pickle
import re
from types import SimpleNamespace
from unittest import mock

import pytest
from mcp.types import CallToolResult, ServerResult, TextContent, Tool

from posthog.capture_mode import CaptureMode
from posthog.mcp import (
    PostHogMCP,
    PreparedToolCall,
    derive_session_id_from_conversation,
    get_more_tools_result,
)
from posthog.mcp._output_instructions import MCP_INSTRUCTIONS_KEY
from posthog.test.mcp._helpers import (
    events_named as _events,
    flush_background as _flush,
)
from posthog.version import VERSION


def make_client(**kwargs):
    client = PostHogMCP("phc_test", host="https://us.i.posthog.com", **kwargs)
    captured = []
    # Intercept the inherited Client.capture so nothing is sent over the network.
    client.capture = lambda event, **kwargs: captured.append({"event": event, **kwargs})
    return client, captured


async def test_capture_tool_call_success():
    client, captured = make_client()
    client.capture_tool_call(
        "search_docs",
        intent="finding the install guide",
        intent_source="context_parameter",
        duration_ms=42,
        distinct_id="user_1",
        groups={"organization": "org_1"},
    )
    await _flush()

    calls = _events(captured, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "search_docs"
    assert props["$mcp_intent"] == "finding the install guide"
    assert props["$mcp_is_error"] is False
    assert props["$mcp_duration_ms"] == 42
    assert props["$groups"] == {"organization": "org_1"}
    assert calls[0]["distinct_id"] == "user_1"


async def test_capture_tool_call_error_fans_out_exception():
    client, captured = make_client()
    client.capture_tool_call(
        "broken", is_error=True, error=RuntimeError("kaboom"), distinct_id="u"
    )
    await _flush()

    assert _events(captured, "$mcp_tool_call")[0]["properties"]["$mcp_is_error"] is True
    exc = _events(captured, "$exception")
    assert exc and exc[0]["properties"]["$exception_list"][0]["value"] == "kaboom"


async def test_mcp_events_use_mcp_library_identity():
    captured = []

    def before_send(event):
        captured.append(event)
        return event

    client = PostHogMCP(
        "phc_test",
        host="https://us.i.posthog.com",
        send=False,
        before_send=before_send,
    )
    client.capture_tool_call("broken", is_error=True, error=RuntimeError("kaboom"))
    await _flush()

    assert {event["event"] for event in captured} == {"$mcp_tool_call", "$exception"}
    assert all(
        event["properties"]["$lib"] == "posthog-python-mcp"
        and event["properties"]["$lib_version"] == VERSION
        for event in captured
    )


def test_mcp_library_identity_reaches_capture_v0_header():
    response = mock.Mock(status_code=200)
    client = PostHogMCP("phc_test", sync_mode=True)

    with mock.patch("posthog.request._session.post", return_value=response) as post:
        client.capture("$mcp_custom")

    assert post.call_args.kwargs["headers"]["User-Agent"] == (
        f"posthog-python-mcp/{VERSION}"
    )


def test_mcp_library_identity_reaches_capture_v1_header():
    client = PostHogMCP("phc_test", sync_mode=True, capture_mode=CaptureMode.V1)
    with mock.patch("posthog.client._send_v1_batch") as send:
        client.capture("$mcp_custom")

    assert send.call_args.kwargs["sdk_info"] == f"posthog-python-mcp/{VERSION}"
    event = send.call_args.args[2][0]
    assert event["properties"]["$lib"] == "posthog-python-mcp"
    assert event["properties"]["$lib_version"] == VERSION


def test_mcp_library_identity_reaches_feature_flag_requests():
    response = mock.Mock(status_code=200)
    response.json.return_value = {"flags": {}}
    client = PostHogMCP("phc_test", send=False)

    with mock.patch(
        "posthog.request._flags_session.post", return_value=response
    ) as post:
        client.evaluate_flags("user_1")

    assert post.call_args.kwargs["headers"]["User-Agent"] == (
        f"posthog-python-mcp/{VERSION}"
    )


def test_mcp_library_identity_reaches_feature_flag_definition_requests():
    response = mock.Mock(status_code=200, headers={})
    response.json.return_value = {"flags": [], "group_type_mapping": {}, "cohorts": {}}
    client = PostHogMCP(
        "phc_test",
        secret_key="phs_test",
        send=False,
        enable_local_evaluation=False,
    )

    with mock.patch("posthog.request._session.get", return_value=response) as get:
        client.load_feature_flags()

    assert get.call_args.kwargs["headers"]["User-Agent"] == (
        f"posthog-python-mcp/{VERSION}"
    )
    client.shutdown()


def test_mcp_library_identity_reaches_remote_config_requests():
    response = mock.Mock(status_code=200, headers={})
    response.json.return_value = "payload"
    client = PostHogMCP("phc_test", secret_key="phs_test", send=False)

    with mock.patch("posthog.request._session.get", return_value=response) as get:
        assert client.get_remote_config_payload("flag-key") == "payload"

    assert get.call_args.kwargs["headers"]["User-Agent"] == (
        f"posthog-python-mcp/{VERSION}"
    )


async def test_capture_initialize_and_tools_list():
    client, captured = make_client()
    client.capture_initialize(
        client_name="claude-code", client_version="1.2.3", distinct_id="u"
    )
    client.capture_tools_list(tool_names=["a", "b"], distinct_id="u")
    await _flush()

    init = _events(captured, "$mcp_initialize")
    assert init and init[0]["properties"]["$mcp_client_name"] == "claude-code"
    listed = _events(captured, "$mcp_tools_list")
    assert listed and listed[0]["properties"]["$mcp_listed_tool_names"] == ["a", "b"]


async def test_capture_missing_capability():
    client, captured = make_client()
    client.capture_missing_capability(
        context="wanted a tool to export to CSV", distinct_id="u"
    )
    await _flush()

    missing = _events(captured, "$mcp_missing_capability")
    assert (
        missing
        and missing[0]["properties"]["$mcp_intent"] == "wanted a tool to export to CSV"
    )


def test_prepare_tool_call_extracts_intent_and_strips_context():
    client, _ = make_client()
    prepared = client.prepare_tool_call(
        "search", {"q": "x", "context": "looking up the answer"}
    )
    assert prepared.intent == "looking up the answer"
    assert prepared.intent_source == "context_parameter"
    assert prepared.args == {"q": "x"}
    assert prepared.is_missing_capability is False

    prepared_missing = client.prepare_tool_call(
        "get_more_tools", {"context": "need something else"}
    )
    assert prepared_missing.is_missing_capability is True


def test_prepare_tool_list_injects_context_into_dicts():
    client, _ = make_client()
    tools = [
        {
            "name": "search",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]
    prepared = client.prepare_tool_list(tools)
    assert "context" in prepared[0]["inputSchema"]["properties"]
    # original tool dict is untouched
    assert "context" not in tools[0]["inputSchema"]["properties"]


def test_prepare_tool_list_can_be_disabled():
    client, _ = make_client()
    tools = [{"name": "search", "inputSchema": {"type": "object", "properties": {}}}]
    prepared = client.prepare_tool_list(tools, context=False)
    assert "context" not in prepared[0]["inputSchema"]["properties"]


@pytest.mark.parametrize(
    "options", [{"capture_model": True}, {"capture_model": False}, {}]
)
async def test_prepare_and_capture_model(options: dict[str, bool]) -> None:
    client, captured = make_client(**options)
    enabled = options.get("capture_model", True)
    tools = [
        {
            "name": "search",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]

    prepared_tools = client.prepare_tool_list(tools)
    schema = prepared_tools[0]["inputSchema"]
    assert ("llm_model" in schema["properties"]) == enabled
    assert ("llm_model" in schema.get("required", [])) == enabled

    call = client.prepare_tool_call(
        "search",
        {"q": "docs", "llm_model": "claude-opus-4-8"},
        request_meta={"x-codex-turn-metadata": {"model": "gpt-5.6-sol"}},
    )
    assert call.args == (
        {"q": "docs"} if enabled else {"q": "docs", "llm_model": "claude-opus-4-8"}
    )
    assert call.llm_model == ("gpt-5.6-sol" if enabled else None)
    assert call.llm_model_source == ("client_metadata" if enabled else None)

    client.capture_tool_call(
        "search",
        llm_model=call.llm_model,
        llm_model_source=call.llm_model_source,
    )
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert props.get("$mcp_llm_model") == ("gpt-5.6-sol" if enabled else None)
    assert props.get("$mcp_llm_model_source") == (
        "client_metadata" if enabled else None
    )


@pytest.mark.parametrize("sdk_tool", [False, True])
@pytest.mark.parametrize("pass_original_tool", [False, True])
def test_prepare_model_preserves_object_tool_ownership(
    sdk_tool: bool, pass_original_tool: bool
) -> None:
    client, _ = make_client(capture_model=True)
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    tool = (
        Tool(name="search", inputSchema=schema)
        if sdk_tool
        else SimpleNamespace(name="search", input_schema=schema)
    )
    schema_attribute = (
        "input_schema" if hasattr(tool, "input_schema") else "inputSchema"
    )
    for _ in range(2):
        prepared = client.prepare_tool_list([tool], context=False)
        assert "llm_model" in getattr(prepared[0], schema_attribute)["properties"]
        call = client.prepare_tool_call(
            "search",
            {"q": "docs", "llm_model": "example-model"},
            original_tool=tool if pass_original_tool else None,
        )
        assert call.args == {"q": "docs"}
        assert (call.llm_model, call.llm_model_source) == (
            "example-model",
            "self_reported",
        )
        assert "llm_model" not in getattr(tool, schema_attribute)["properties"]


def test_prepare_tool_call_preserves_application_owned_model_argument():
    client, _ = make_client(capture_model=True)
    tool = {
        "name": "route",
        "inputSchema": {
            "type": "object",
            "properties": {"llm_model": {"type": "string"}},
            "required": ["llm_model"],
        },
    }
    client.prepare_tool_list([tool])

    call = client.prepare_tool_call(
        "route", {"llm_model": "application-owned"}, original_tool=tool
    )
    assert call.args == {"llm_model": "application-owned"}
    assert call.llm_model is None


def test_prepare_tool_list_fails_closed_for_duplicate_tool_names():
    client, _ = make_client(capture_model=True)
    tools = [
        {"name": "route", "inputSchema": {"type": "object", "properties": {}}},
        {
            "name": "route",
            "inputSchema": {
                "type": "object",
                "properties": {"llm_model": {"type": "string"}},
            },
        },
    ]

    prepared = client.prepare_tool_list(tools)
    assert "llm_model" not in prepared[0]["inputSchema"]["properties"]
    assert prepared[1]["inputSchema"]["properties"]["llm_model"] == {"type": "string"}
    call = client.prepare_tool_call("route", {"llm_model": "application-owned"})
    assert call.args == {"llm_model": "application-owned"}
    assert call.llm_model is None


_CONVERSATION_ID = "0198ef20-1234-7abc-8def-123456789abc"
_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _sql_tool() -> dict:
    return {
        "name": "execute-sql",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "outputSchema": {
            "type": "object",
            "properties": {"rows": {"type": "array"}},
            "additionalProperties": False,
        },
    }


def _handle_block(conversation_id: str) -> dict:
    return {"type": "text", "text": json.dumps({"conversation_id": conversation_id})}


def test_prepare_tool_list_adds_conversation_schemas_without_mutating_source():
    client, _ = make_client()
    tools = [_sql_tool()]

    prepared = client.prepare_tool_list(tools)

    input_schema = prepared[0]["inputSchema"]
    assert input_schema["properties"]["conversation_id"]["type"] == "string"
    assert "conversation_id" not in input_schema.get("required", [])
    assert prepared[0]["outputSchema"]["properties"][MCP_INSTRUCTIONS_KEY]["type"] == (
        "object"
    )
    assert tools == [_sql_tool()]


def test_conversation_disabled_leaves_schemas_arguments_and_results_unchanged():
    client, _ = make_client(enable_conversation_id=False)
    prepared_tools = client.prepare_tool_list([_sql_tool()])
    assert "conversation_id" not in prepared_tools[0]["inputSchema"]["properties"]
    assert MCP_INSTRUCTIONS_KEY not in prepared_tools[0]["outputSchema"]["properties"]

    raw_args = {"query": "select 1", "conversation_id": _CONVERSATION_ID}
    call = client.prepare_tool_call("execute-sql", raw_args)
    tool_result = {"content": [], "structuredContent": {"rows": []}}
    prepared = client.prepare_tool_result(tool_result, call)

    assert call.args == raw_args
    assert (call.session_id, call.conversation_id) == (None, None)
    assert prepared.result is tool_result
    assert (prepared.session_id, prepared.conversation_id) == (None, None)


def test_conversation_preserves_application_fields_and_fails_closed_for_duplicates():
    client, _ = make_client()
    application_owned = {
        "name": "execute-sql",
        "inputSchema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"}},
        },
        "outputSchema": {
            "type": "object",
            "properties": {MCP_INSTRUCTIONS_KEY: {"type": "string"}},
        },
    }

    prepared = client.prepare_tool_list([_sql_tool(), application_owned])

    assert "conversation_id" not in prepared[0]["inputSchema"]["properties"]
    assert MCP_INSTRUCTIONS_KEY not in prepared[0]["outputSchema"]["properties"]
    assert prepared[1]["inputSchema"]["properties"]["conversation_id"] == {
        "type": "string"
    }
    assert prepared[1]["outputSchema"]["properties"][MCP_INSTRUCTIONS_KEY] == {
        "type": "string"
    }
    call = client.prepare_tool_call(
        "execute-sql", {"conversation_id": _CONVERSATION_ID}
    )
    assert call.args == {"conversation_id": _CONVERSATION_ID}
    assert call.conversation_id is None


def test_prepare_tool_call_uses_original_tool_without_prior_listing():
    client, _ = make_client()
    call = client.prepare_tool_call(
        "execute-sql",
        {"query": "select 1", "conversation_id": _CONVERSATION_ID},
        original_tool=_sql_tool(),
    )

    assert call.args == {"query": "select 1"}
    assert call.conversation_id == _CONVERSATION_ID
    assert call.session_id == derive_session_id_from_conversation(_CONVERSATION_ID)


def test_prepare_tool_call_mints_handles_and_derives_stable_sessions_across_clients():
    client, _ = make_client()
    client.prepare_tool_list([_sql_tool()])
    minted = client.prepare_tool_call(
        "execute-sql", {"query": "select 1", "conversation_id": "invalid"}
    )
    assert _UUID7.match(minted.conversation_id)
    assert minted.args == {"query": "select 1"}
    assert minted.session_id == derive_session_id_from_conversation(
        minted.conversation_id
    )

    other_replica, _ = make_client()
    first = client.prepare_tool_call(
        "execute-sql", {"conversation_id": _CONVERSATION_ID}
    )
    echoed = other_replica.prepare_tool_call(
        "execute-sql",
        {"conversation_id": _CONVERSATION_ID.upper()},
        original_tool=_sql_tool(),
    )
    assert echoed.conversation_id == _CONVERSATION_ID
    assert echoed.session_id == first.session_id


def test_prepare_tool_call_keeps_carried_session_unless_a_handle_is_echoed():
    client, _ = make_client()
    client.prepare_tool_list([_sql_tool()])

    carried = client.prepare_tool_call("execute-sql", {}, session_id="ses_carried")
    assert (carried.session_id, carried.conversation_id) == ("ses_carried", None)

    echoed = client.prepare_tool_call(
        "execute-sql",
        {"conversation_id": _CONVERSATION_ID},
        session_id="ses_carried",
    )
    assert echoed.conversation_id == _CONVERSATION_ID
    assert echoed.session_id == derive_session_id_from_conversation(_CONVERSATION_ID)


@pytest.mark.parametrize(
    "transport",
    [lambda call: call, lambda call: pickle.loads(pickle.dumps(call))],
    ids=["same-process", "pickled"],
)
def test_prepare_tool_result_delivers_minted_handle_without_mutation(transport):
    client, _ = make_client()
    client.prepare_tool_list([_sql_tool()])
    call = client.prepare_tool_call("execute-sql", {"query": "select 1"})
    tool_result = {
        "content": [{"type": "text", "text": "done"}],
        "structuredContent": {"rows": []},
    }

    prepared = client.prepare_tool_result(tool_result, transport(call))

    assert tool_result == {
        "content": [{"type": "text", "text": "done"}],
        "structuredContent": {"rows": []},
    }
    assert prepared.result["content"][-1] == _handle_block(call.conversation_id)
    assert prepared.result["structuredContent"][MCP_INSTRUCTIONS_KEY] == {
        "conversation_id": call.conversation_id
    }
    assert prepared.conversation_id == call.conversation_id


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "server-result"])
def test_prepare_tool_result_delivers_into_call_tool_result_models(wrapped):
    if wrapped and not isinstance(ServerResult, type):
        pytest.skip("MCP SDK 2.x has no ServerResult wrapper")
    client, _ = make_client()
    client.prepare_tool_list([_sql_tool()])
    call = client.prepare_tool_call("execute-sql", {})
    call_result = CallToolResult(
        content=[TextContent(type="text", text="done")],
        structuredContent={"rows": []},
    )
    tool_result = ServerResult(call_result) if wrapped else call_result

    prepared = client.prepare_tool_result(tool_result, call)

    delivered = prepared.result.root if wrapped else prepared.result
    assert len(call_result.content) == 1
    assert delivered.content[-1].text == _handle_block(call.conversation_id)["text"]
    assert delivered.structuredContent[MCP_INSTRUCTIONS_KEY] == {
        "conversation_id": call.conversation_id
    }
    assert prepared.conversation_id == call.conversation_id


def test_prepare_tool_result_omits_conversation_without_delivery_state():
    client, _ = make_client()
    tool_result = {"content": []}
    call = PreparedToolCall(session_id="ses_123", conversation_id=_CONVERSATION_ID)

    prepared = client.prepare_tool_result(tool_result, call)

    assert prepared.result is tool_result
    assert (prepared.session_id, prepared.conversation_id) == ("ses_123", None)


def test_prepare_tool_result_preserves_application_structured_instructions():
    client, _ = make_client()
    tool = {
        **_sql_tool(),
        "outputSchema": {
            "type": "object",
            "properties": {MCP_INSTRUCTIONS_KEY: {"type": "string"}},
        },
    }
    client.prepare_tool_list([tool])
    call = client.prepare_tool_call("execute-sql", {})

    prepared = client.prepare_tool_result(
        {"content": [], "structuredContent": {MCP_INSTRUCTIONS_KEY: "app-value"}},
        call,
    )

    assert prepared.result["structuredContent"][MCP_INSTRUCTIONS_KEY] == "app-value"
    assert prepared.conversation_id == call.conversation_id


def test_prepare_tool_result_delivers_minted_handle_on_error_results():
    client, _ = make_client()
    client.prepare_tool_list([_sql_tool()])
    call = client.prepare_tool_call("execute-sql", {})

    prepared = client.prepare_tool_result({"content": [], "isError": True}, call)

    assert prepared.result["isError"] is True
    assert _handle_block(call.conversation_id) in prepared.result["content"]
    assert prepared.conversation_id == call.conversation_id


def test_prepare_tool_result_omits_undelivered_minted_handle_but_keeps_session():
    client, _ = make_client()
    client.prepare_tool_list([_sql_tool()])
    call = client.prepare_tool_call("execute-sql", {})
    tool_result = {"value": 1}

    prepared = client.prepare_tool_result(tool_result, call)

    assert prepared.result is tool_result
    assert prepared.conversation_id is None
    assert prepared.session_id == call.session_id


def test_prepare_tool_list_adds_conversation_to_virtual_tools():
    client, _ = make_client()
    prepared = client.prepare_tool_list([], report_missing=True)
    virtual_tool = next(t for t in prepared if t["name"] == "get_more_tools")
    call = client.prepare_tool_call("get_more_tools", {"context": "Find a tool"})

    result = client.prepare_tool_result(get_more_tools_result(), call)

    assert virtual_tool["inputSchema"]["properties"]["conversation_id"]["type"] == (
        "string"
    )
    assert result.result["content"][-1] == _handle_block(call.conversation_id)


async def test_capture_tool_call_records_prepared_conversation_and_session():
    client, captured = make_client()
    client.prepare_tool_list([_sql_tool()])
    call = client.prepare_tool_call("execute-sql", {})
    prepared = client.prepare_tool_result({"content": []}, call)

    client.capture_tool_call(
        "execute-sql",
        distinct_id="user-123",
        session_id=prepared.session_id,
        conversation_id=prepared.conversation_id,
    )
    await _flush()

    props = _events(captured, "$mcp_tool_call")[0]["properties"]
    assert props["$mcp_conversation_id"] == prepared.conversation_id
    assert props["$session_id"] == prepared.session_id
