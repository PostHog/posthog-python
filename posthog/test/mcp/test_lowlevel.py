"""End-to-end tests for the low-level mcp.server.Server adapter (Milestone 3)."""

import json

import pytest

import mcp.types as mcp_types
from mcp.server.lowlevel import Server

from posthog.mcp import instrument
from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
    listed_uris,
)


def make_server(*, resource_error: bool = False, listing: str = "resources") -> Server:
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
        if listing == "error":
            raise ValueError("listing unavailable")
        return mcp_types.ServerResult(
            mcp_types.ListResourcesResult(
                resources=[]
                if listing == "empty"
                else [
                    mcp_types.Resource(
                        name="Guide",
                        uri="file:///guide.md",
                        mimeType="text/markdown",
                    )
                ]
            )
        )

    async def list_resource_templates(_request):
        return mcp_types.ServerResult(
            mcp_types.ListResourceTemplatesResult(
                resourceTemplates=[
                    mcp_types.ResourceTemplate(
                        name="Profile",
                        uriTemplate="users://{user_id}/profile",
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
    server.request_handlers[mcp_types.ListResourceTemplatesRequest] = (
        list_resource_templates
    )
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
            "https://fakeuser:fakepass@example.com/guide",
            "https://%5Bredacted%5D@example.com/guide",
            False,
        ),
        (
            "https://fakeuser:fakepass@example.com/guide",
            "https://%5Bredacted%5D@example.com/guide",
            True,
        ),
        (
            "https://example.com/guide?token=fakesecret&chapter=intro",
            "https://example.com/guide?token=%5Bredacted%5D&chapter=intro",
            False,
        ),
        (
            "https://example.com/guide?token=fakesecret&chapter=intro",
            "https://example.com/guide?token=%5Bredacted%5D&chapter=intro",
            True,
        ),
        (
            "https://example.com/guide?access_token=fakeaccess&X-Amz-Credential=fakecredential&X-Amz-Signature=fakesignature",
            "https://example.com/guide?access_token=%5Bredacted%5D&X-Amz-Credential=%5Bredacted%5D&X-Amz-Signature=%5Bredacted%5D",
            False,
        ),
        (
            "https://example.com/guide?access_token=fakeaccess&X-Amz-Credential=fakecredential&X-Amz-Signature=fakesignature",
            "https://example.com/guide?access_token=%5Bredacted%5D&X-Amz-Credential=%5Bredacted%5D&X-Amz-Signature=%5Bredacted%5D",
            True,
        ),
        (
            "ui://guide/page?%74oken=fakesecret&TOKEN=fakeaccess&chapter=intro#section",
            "ui://guide/page?token=%5Bredacted%5D&TOKEN=%5Bredacted%5D&chapter=intro#section",
            False,
        ),
        (
            "ui://guide/page?%74oken=fakesecret&TOKEN=fakeaccess&chapter=intro#section",
            "ui://guide/page?token=%5Bredacted%5D&TOKEN=%5Bredacted%5D&chapter=intro#section",
            True,
        ),
        # The PostHog-token pass runs before the URL is parsed, so the token is
        # already `[redacted]` by then and the URL rewrite finds nothing left to
        # change — the value keeps that literal form instead of being re-encoded.
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
    for secret in (
        "phx_EXAMPLEONLYFAKEVALUE00000000000",
        "fakeuser",
        "fakepass",
        "fakesecret",
        "fakeaccess",
        "fakecredential",
        "fakesignature",
    ):
        assert secret not in json.dumps(client.events)


@pytest.mark.parametrize(
    "method, listing, listed",
    [
        ("resources/list", "resources", ["file:///guide.md"]),
        ("resources/list", "empty", []),
        ("resources/templates/list", "resources", ["users://{user_id}/profile"]),
    ],
)
async def test_resource_listing_event_carries_the_listing(
    method: str, listing: str, listed: list
) -> None:
    server = make_server(listing=listing)
    client = FakeClient()
    instrument(server, client)

    request_type = (
        mcp_types.ListResourcesRequest
        if method == "resources/list"
        else mcp_types.ListResourceTemplatesRequest
    )
    await server.request_handlers[request_type](request_type())
    await _flush()

    events = _events(client, "$mcp_resources_list")
    assert len(events) == 1
    props = events[0]["properties"]
    assert props["$mcp_parameters"]["request"]["method"] == method
    assert listed_uris(props["$mcp_response"]) == listed
    # An empty listing is a legitimate answer (a template-only server lists no
    # static resources), unlike an empty tools/list.
    assert props["$mcp_is_error"] is False
    assert props["$mcp_duration_ms"] >= 0
    assert "$mcp_resource_name" not in props
    assert not _events(client, "$exception")


async def test_failed_resource_listing_is_captured() -> None:
    server = make_server(listing="error")
    client = FakeClient()
    instrument(server, client)

    with pytest.raises(ValueError, match="listing unavailable"):
        await server.request_handlers[mcp_types.ListResourcesRequest](
            mcp_types.ListResourcesRequest()
        )
    await _flush()

    events = _events(client, "$mcp_resources_list")
    assert len(events) == 1
    props = events[0]["properties"]
    assert props["$mcp_is_error"] is True
    assert props["$mcp_duration_ms"] >= 0
    assert "$mcp_response" not in props
    assert "$mcp_resource_name" not in props
    exceptions = _events(client, "$exception")
    assert len(exceptions) == 1
    assert "listing unavailable" in json.dumps(exceptions[0]["properties"])


async def test_identify_on_a_resource_read_is_named_by_the_uri() -> None:
    server = make_server()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            identify=lambda request, extra: UserIdentity(distinct_id="user_42")
        ),
    )

    await server.request_handlers[mcp_types.ReadResourceRequest](
        mcp_types.ReadResourceRequest(
            params=mcp_types.ReadResourceRequestParams(
                uri="https://fakeuser:fakepass@example.com/guide"
            )
        )
    )
    await _flush()

    identified = _events(client, "$identify")
    assert len(identified) == 1
    # A resources/read request carries no `name`, so the uri is the only thing
    # that can name it — sanitized like any other captured URL.
    assert (
        identified[0]["properties"]["$mcp_resource_name"]
        == "https://%5Bredacted%5D@example.com/guide"
    )


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
