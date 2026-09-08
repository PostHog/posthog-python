"""End-to-end tests for the low-level mcp.server.Server adapter (Milestone 3)."""

import json

import pytest

import mcp.types as mcp_types
from mcp.server.lowlevel import Server

from posthog.mcp import instrument
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)


def make_server(*, resource_error: bool = False) -> Server:
    server = Server("test-lowlevel")

    @server.list_tools()
    async def list_tools():
        return [
            mcp_types.Tool(
                name="echo",
                description="Echo back a message",
                inputSchema={
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "echo":
            return [mcp_types.TextContent(type="text", text=str(arguments.get("msg")))]
        raise ValueError("boom")

    async def list_resources(_request):
        return mcp_types.ServerResult(
            mcp_types.ListResourcesResult(
                resources=[
                    mcp_types.Resource(
                        name="Guide",
                        uri="file:///guide.md",
                        mimeType="text/markdown",
                    )
                ]
            )
        )

    async def read_resource(request):
        if resource_error:
            raise ValueError(f"Cannot read {request.params.uri}")
        return mcp_types.ServerResult(
            mcp_types.ReadResourceResult(
                contents=[
                    mcp_types.TextResourceContents(
                        uri=request.params.uri,
                        mimeType="text/markdown",
                        text="# Guide",
                    )
                ]
            )
        )

    server.request_handlers[mcp_types.ListResourcesRequest] = list_resources
    server.request_handlers[mcp_types.ReadResourceRequest] = read_resource

    return server


def _call_request(name, arguments):
    return mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )


async def test_list_tools_injects_optional_context_and_captures():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    await _flush()

    tool = result.root.tools[0]
    assert "context" in tool.inputSchema["properties"]
    # context is OPTIONAL on the low-level path (schema is also the validation schema)
    assert "context" not in tool.inputSchema.get("required", [])

    listed = _events(client, "$mcp_tools_list")
    assert listed and listed[0]["properties"]["$mcp_listed_tool_names"] == ["echo"]


@pytest.mark.parametrize(
    "uri, captured_uri, resource_error",
    [
        ("file:///guide.md", "file:///guide.md", False),
        (
            "https://example.com/guide?token=phx_EXAMPLEONLYFAKEVALUE00000000000",
            "https://example.com/guide?token=[redacted]",
            False,
        ),
        (
            "https://example.com/guide?token=phx_EXAMPLEONLYFAKEVALUE00000000000",
            "https://example.com/guide?token=[redacted]",
            True,
        ),
    ],
)
async def test_resource_discovery_and_read_are_captured(
    uri: str, captured_uri: str, resource_error: bool
) -> None:
    server = make_server(resource_error=resource_error)
    client = FakeClient()
    instrument(server, client)

    await server.request_handlers[mcp_types.ListResourcesRequest](
        mcp_types.ListResourcesRequest()
    )
    request = mcp_types.ReadResourceRequest(
        params=mcp_types.ReadResourceRequestParams(uri=uri)
    )
    read = server.request_handlers[mcp_types.ReadResourceRequest](request)
    if resource_error:
        with pytest.raises(ValueError) as caught:
            await read
        assert str(caught.value) == f"Cannot read {uri}"
    else:
        result = await read
        assert result.root.contents[0].text == "# Guide"
        assert str(result.root.contents[0].uri) == uri
    await _flush()

    assert len(_events(client, "$mcp_resources_list")) == 1
    reads = _events(client, "$mcp_resource_read")
    assert len(reads) == 1
    props = reads[0]["properties"]
    assert props["$mcp_resource_name"] == captured_uri
    assert props["$mcp_parameters"]["request"]["params"]["uri"] == captured_uri
    assert props["$mcp_is_error"] is resource_error
    assert "$mcp_response" not in props
    exceptions = _events(client, "$exception")
    assert len(exceptions) == int(resource_error)
    if resource_error:
        assert exceptions[0]["properties"]["$mcp_resource_name"] == captured_uri
    assert "phx_EXAMPLEONLYFAKEVALUE00000000000" not in json.dumps(client.events)


async def test_tool_call_success_captures_intent():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    # populate the tool cache first
    await server.request_handlers[mcp_types.ListToolsRequest](
        mcp_types.ListToolsRequest(method="tools/list")
    )

    handler = server.request_handlers[mcp_types.CallToolRequest]
    result = await handler(
        _call_request(
            "echo", {"msg": "hi", "context": "echoing a message for the test"}
        )
    )
    await _flush()

    assert result.root.isError is False
    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "echo"
    # tools/list metadata and tools/call capture share the same lifecycle policy.
    assert props["$mcp_tool_description"] == "Echo back a message"
    assert props["$mcp_intent"] == "echoing a message for the test"
    assert props["$mcp_is_error"] is False
    # context is stripped from captured parameters
    assert "context" not in props["$mcp_parameters"]["request"]["params"]["arguments"]


async def test_tool_call_error_captured_from_is_error_result():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    handler = server.request_handlers[mcp_types.CallToolRequest]
    # "fail" is unlisted -> no validation -> handler raises -> isError result
    result = await handler(
        _call_request("fail", {"context": "trying a tool that errors"})
    )
    await _flush()

    assert result.root.isError is True
    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    exceptions = _events(client, "$exception")
    assert (
        exceptions
        and "boom" in exceptions[0]["properties"]["$exception_list"][0]["value"]
    )


async def test_initialize_emitted_once():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    handler = server.request_handlers[mcp_types.CallToolRequest]
    await handler(_call_request("echo", {"msg": "a", "context": "first call"}))
    await handler(_call_request("echo", {"msg": "b", "context": "second call"}))
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 1
    assert len(_events(client, "$mcp_tool_call")) == 2
