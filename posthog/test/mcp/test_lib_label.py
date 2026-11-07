"""``posthog-python-mcp`` labels the MCP pipeline's events, and only those."""

from datetime import datetime, timezone
from unittest import mock

import pytest

from posthog.async_client import AsyncClient
from posthog.capture_event import _to_v1_event
from posthog.client import Client
from posthog.contexts import new_context, tag
from posthog.mcp import instrument
from posthog.mcp._event_types import MCPAnalyticsEventType
from posthog.mcp._sink import McpCaptureOptions, McpEventSink
from posthog.mcp.posthog_mcp import PostHogMCP
from posthog.test.mcp._helpers import MCP_MAJOR, flush_background
from posthog.test.capture_helpers import (
    patch_async_capture_send,
    patch_capture_send,
    sent_batch,
    sent_events,
)
from posthog.version import VERSION

MCP_LIB = ("posthog-python-mcp", VERSION)
SDK_LIB = ("posthog-python", VERSION)


def _tool_call(**overrides):
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_abc",
        "resource_name": "search",
        "timestamp": datetime.now(timezone.utc),
    }
    event.update(overrides)
    return event


def _failed_tool_call():
    return _tool_call(is_error=True, error={"message": "boom", "type": "ValueError"})


def _lib(properties):
    return (properties.get("$lib"), properties.get("$lib_version"))


def _sync_wire(send):
    return {e["event"]: e["properties"] for e in map(_to_v1_event, sent_events(send))}


async def test_sync_client_labels_mcp_events_and_not_app_events():
    with patch_capture_send("client") as send:
        client = Client("phc_test", sync_mode=True)
        await McpEventSink(client).capture(_failed_tool_call(), McpCaptureOptions())
        client.capture("app event", distinct_id="user-1")

    wire = _sync_wire(send)
    assert _lib(wire["$mcp_tool_call"]) == MCP_LIB
    assert _lib(wire["$exception"]) == MCP_LIB
    assert _lib(wire["app event"]) == SDK_LIB
    assert {c.kwargs["sdk_info"] for c in send.call_args_list} == {
        f"posthog-python/{VERSION}"
    }


async def test_async_client_labels_mcp_events_and_not_app_events():
    with patch_async_capture_send(new=mock.AsyncMock()) as send:
        client = AsyncClient("phc_test")
        await McpEventSink(client).capture(_failed_tool_call(), McpCaptureOptions())
        client.capture("app event", distinct_id="user-1")
        await client.shutdown()

    wire = {
        e["event"]: e["properties"]
        for index in range(len(send.await_args_list))
        for e in sent_batch(send, index)
    }
    assert _lib(wire["$mcp_tool_call"]) == MCP_LIB
    assert _lib(wire["$exception"]) == MCP_LIB
    assert _lib(wire["app event"]) == SDK_LIB


@pytest.mark.parametrize("client_kind", ["sync", "async"])
async def test_instrumented_server_labels_its_tool_calls(client_kind):
    if MCP_MAJOR >= 2:
        from mcp.server.mcpserver import MCPServer as Server
    else:
        from mcp.server.fastmcp import FastMCP as Server

    server = Server(f"lib-label-{client_kind}")

    @server.tool()
    def echo(msg: str) -> str:
        return msg

    if client_kind == "sync":
        with patch_capture_send("client") as send:
            client = Client("phc_test", sync_mode=True)
            instrument(server, client)
            await server._tool_manager.call_tool("echo", {"msg": "hi"})
            await flush_background()
            client.capture("app event", distinct_id="user-1")
        wire = _sync_wire(send)
    else:
        with patch_async_capture_send(new=mock.AsyncMock()) as send:
            client = AsyncClient("phc_test")
            instrument(server, client)
            await server._tool_manager.call_tool("echo", {"msg": "hi"})
            await flush_background()
            client.capture("app event", distinct_id="user-1")
            await client.shutdown()
        wire = {
            e["event"]: e["properties"]
            for index in range(len(send.await_args_list))
            for e in sent_batch(send, index)
        }

    assert _lib(wire["$mcp_tool_call"]) == MCP_LIB
    assert _lib(wire["app event"]) == SDK_LIB


def test_posthog_mcp_labels_tool_calls_and_not_custom_events():
    with patch_capture_send("client") as send:
        client = PostHogMCP("phc_test", sync_mode=True)
        client.capture_tool_call("search", distinct_id="user-1", session_id="ses_abc")
        client.flush()
        client.capture("app event", distinct_id="user-1")

    wire = _sync_wire(send)
    assert _lib(wire["$mcp_tool_call"]) == MCP_LIB
    assert _lib(wire["app event"]) == SDK_LIB


def test_instrument_without_a_client_leaves_the_default_client_unlabeled():
    from mcp.server.lowlevel import Server

    with patch_capture_send("client") as send:
        default_client = Client("phc_test", sync_mode=True)
        with mock.patch("posthog.setup", return_value=default_client):
            instrument(Server("probe-default-client"))
        default_client.capture("app event", distinct_id="user-1")

    assert _lib(_sync_wire(send)["app event"]) == SDK_LIB
    assert send.call_args.kwargs["sdk_info"] == f"posthog-python/{VERSION}"


@pytest.mark.parametrize(
    "properties, expected",
    [
        ({"$lib": "my-wrapper", "$lib_version": "2.0.0"}, ("my-wrapper", "2.0.0")),
        ({"$lib": "my-wrapper"}, ("my-wrapper", None)),
        ({"$lib_version": "9.9.9"}, MCP_LIB),
        ({"$lib": ""}, MCP_LIB),
        ({"$lib": None, "$lib_version": "9.9.9"}, MCP_LIB),
        ({"$lib": 5}, MCP_LIB),
        ({"$lib": "posthog-python-mcp"}, MCP_LIB),
        ({"$lib": "posthog-python"}, SDK_LIB),
    ],
    ids=[
        "app_pair_wins",
        "app_lib_keeps_no_version",
        "lone_version_replaced",
        "empty_lib_unset",
        "null_lib_unset",
        "number_lib_unset",
        "label_gets_version",
        "sdk_lib_gets_version",
    ],
)
async def test_event_properties_follow_the_pair_rule(properties, expected):
    with patch_capture_send("client") as send:
        client = Client("phc_test", sync_mode=True)
        await McpEventSink(client).capture(
            _tool_call(properties=properties), McpCaptureOptions()
        )

    assert _lib(_sync_wire(send)["$mcp_tool_call"]) == expected


@pytest.mark.parametrize("layer", ["super_properties", "context_tag"])
async def test_mcp_label_beats_lower_layers_but_not_on_app_events(layer):
    wrapper = {"$lib": "my-wrapper", "$lib_version": "2.0.0"}
    with patch_capture_send("client") as send:
        client = Client(
            "phc_test",
            sync_mode=True,
            super_properties=wrapper if layer == "super_properties" else None,
        )
        with new_context(fresh=True):
            if layer == "context_tag":
                for key, value in wrapper.items():
                    tag(key, value)
            await McpEventSink(client).capture(_tool_call(), McpCaptureOptions())
            client.capture("app event", distinct_id="user-1")

    wire = _sync_wire(send)
    assert _lib(wire["$mcp_tool_call"]) == MCP_LIB
    assert _lib(wire["app event"]) == ("my-wrapper", "2.0.0")


def _relabel(event):
    event["properties"]["$lib"] = "my-wrapper"
    event["properties"]["$lib_version"] = "2.0.0"
    return event


def _unlabel(event):
    event["properties"].pop("$lib")
    event["properties"].pop("$lib_version")
    return event


@pytest.mark.parametrize(
    "before_send, expected",
    [(_relabel, ("my-wrapper", "2.0.0")), (_unlabel, SDK_LIB)],
    ids=["mcp_before_send_relabels", "removal_falls_back_to_the_sdk_pair"],
)
async def test_mcp_before_send_sees_the_label(before_send, expected):
    with patch_capture_send("client") as send:
        client = Client("phc_test", sync_mode=True)
        await McpEventSink(client).capture(
            _tool_call(), McpCaptureOptions(before_send=before_send)
        )

    assert _lib(_sync_wire(send)["$mcp_tool_call"]) == expected
