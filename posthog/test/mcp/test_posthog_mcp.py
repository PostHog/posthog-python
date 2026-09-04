"""Tests for the PostHogMCP custom-dispatcher client (Milestone 3)."""

from unittest import mock

from posthog.capture_mode import CaptureMode
from posthog.mcp import PostHogMCP
from posthog.mcp.version import __version__ as MCP_VERSION
from posthog.test.mcp._helpers import (
    events_named as _events,
    flush_background as _flush,
)


def make_client():
    client = PostHogMCP("phc_test", host="https://us.i.posthog.com")
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
        and event["properties"]["$lib_version"] == MCP_VERSION
        for event in captured
    )


def test_mcp_library_identity_reaches_capture_v0_header():
    response = mock.Mock(status_code=200)
    client = PostHogMCP("phc_test", sync_mode=True)

    with mock.patch("posthog.request._session.post", return_value=response) as post:
        client.capture("$mcp_custom")

    assert post.call_args.kwargs["headers"]["User-Agent"] == (
        f"posthog-python-mcp/{MCP_VERSION}"
    )


def test_mcp_library_identity_reaches_capture_v1_header():
    client = PostHogMCP("phc_test", sync_mode=True, capture_mode=CaptureMode.V1)
    with mock.patch("posthog.client._send_v1_batch") as send:
        client.capture("$mcp_custom")

    assert send.call_args.kwargs["sdk_info"] == f"posthog-python-mcp/{MCP_VERSION}"
    event = send.call_args.args[2][0]
    assert event["properties"]["$lib"] == "posthog-python-mcp"
    assert event["properties"]["$lib_version"] == MCP_VERSION


def test_mcp_library_identity_reaches_feature_flag_requests():
    response = mock.Mock(status_code=200)
    response.json.return_value = {"flags": {}}
    client = PostHogMCP("phc_test", send=False)

    with mock.patch(
        "posthog.request._flags_session.post", return_value=response
    ) as post:
        client.evaluate_flags("user_1")

    assert post.call_args.kwargs["headers"]["User-Agent"] == (
        f"posthog-python-mcp/{MCP_VERSION}"
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
        f"posthog-python-mcp/{MCP_VERSION}"
    )
    client.shutdown()


def test_mcp_library_identity_reaches_remote_config_requests():
    response = mock.Mock(status_code=200, headers={})
    response.json.return_value = "payload"
    client = PostHogMCP("phc_test", secret_key="phs_test", send=False)

    with mock.patch("posthog.request._session.get", return_value=response) as get:
        assert client.get_remote_config_payload("flag-key") == "payload"

    assert get.call_args.kwargs["headers"]["User-Agent"] == (
        f"posthog-python-mcp/{MCP_VERSION}"
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
