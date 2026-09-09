"""Standalone FastMCP on the MCP SDK v2 registry, exercised through HTTP."""

from contextlib import asynccontextmanager

import httpx
import pytest

pytest.importorskip("fastmcp", minversion="4")

from fastmcp import FastMCP  # noqa: E402

from posthog.mcp import MCPAnalyticsOptions, instrument  # noqa: E402
from posthog.test.mcp._helpers import (  # noqa: E402
    FakeClient,
    events_named,
    flush_background,
)
from posthog.test.mcp._helpers_v2 import (  # noqa: E402
    LEGACY_PROTOCOL_VERSION,
    MODERN_PROTOCOL_VERSION,
    legacy_headers,
    modern_headers,
    modern_meta,
)


@asynccontextmanager
async def wire(server):
    app = server.http_app(json_response=True, stateless_http=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            yield http


async def rpc(http, protocol, method, params):
    params = dict(params)
    headers = legacy_headers()
    if protocol == MODERN_PROTOCOL_VERSION:
        params["_meta"] = modern_meta()
        headers = modern_headers(method, params.get("name"))
    else:
        headers["mcp-protocol-version"] = protocol
    response = await http.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]


async def initialize(http, protocol):
    if protocol == LEGACY_PROTOCOL_VERSION:
        await rpc(
            http,
            protocol,
            "initialize",
            {
                "protocolVersion": protocol,
                "capabilities": {},
                "clientInfo": {"name": "example-client", "version": "1.0"},
            },
        )


@pytest.mark.parametrize("protocol", [LEGACY_PROTOCOL_VERSION, MODERN_PROTOCOL_VERSION])
async def test_capture_success_failure_and_sink_outage(protocol):
    server = FastMCP("example-server")
    sink = FakeClient()
    options = MCPAnalyticsOptions(enable_conversation_id=True, capture_model=True)
    instrument(server, sink, options)
    instrument(server, sink, options)
    received = []

    @server.tool()
    def add(a: int, b: int) -> str:
        received.append({"a": a, "b": b})
        return str(a + b)

    @server.tool()
    def fail() -> str:
        raise ValueError("example failure")

    async with wire(server) as http:
        await initialize(http, protocol)
        listed = await rpc(http, protocol, "tools/list", {})
        schema = next(t for t in listed["tools"] if t["name"] == "add")["inputSchema"]
        assert {"context", "conversation_id", "llm_model"} <= schema[
            "properties"
        ].keys()
        assert not {"context", "conversation_id", "llm_model"} & set(
            schema.get("required", [])
        )
        args = {"context": "example addition", "llm_model": "example-model"}
        result = await rpc(
            http,
            protocol,
            "tools/call",
            {"name": "add", "arguments": {"a": 2, "b": 3, **args}},
        )
        assert not result.get("isError", False)
        assert result["content"][0]["text"] == "5"
        assert received == [{"a": 2, "b": 3}]
        assert len(result["content"]) == 2
        failed = await rpc(
            http, protocol, "tools/call", {"name": "fail", "arguments": args}
        )
        assert failed["isError"]
        await flush_background()
        calls = events_named(sink, "$mcp_tool_call")
        assert len(calls) == 2
        assert [c["properties"]["$mcp_is_error"] for c in calls] == [False, True]
        assert all(c["properties"]["$mcp_protocol_version"] == protocol for c in calls)
        assert calls[0]["properties"]["$mcp_intent"] == "example addition"
        assert calls[0]["properties"]["$mcp_llm_model"] == "example-model"

        def unavailable(*args, **kwargs):
            raise RuntimeError("example capture outage")

        sink.capture = unavailable
        offline = await rpc(
            http, protocol, "tools/call", {"name": "add", "arguments": {"a": 3, "b": 4}}
        )
        assert not offline.get("isError", False)
        assert offline["content"][0]["text"] == "7"
        await flush_background()


@pytest.mark.parametrize("list_first", [False, True])
@pytest.mark.parametrize("mounted", [False, True])
async def test_preserve_application_parameters(list_first, mounted):
    child = FastMCP("example-tools")

    @child.tool()
    def echo(context: str, conversation_id: str, llm_model: str) -> str:
        return f"{context}|{conversation_id}|{llm_model}"

    server = FastMCP("example-app") if mounted else child
    if mounted:
        server.mount(child, namespace="shared")
    name = "shared_echo" if mounted else "echo"
    sink = FakeClient()
    instrument(
        server,
        sink,
        MCPAnalyticsOptions(enable_conversation_id=True, capture_model=True),
    )
    async with wire(server) as http:
        if list_first:
            await rpc(http, MODERN_PROTOCOL_VERSION, "tools/list", {})
        result = await rpc(
            http,
            MODERN_PROTOCOL_VERSION,
            "tools/call",
            {
                "name": name,
                "arguments": {
                    "context": "own-context",
                    "conversation_id": "own-id",
                    "llm_model": "own-model",
                },
            },
        )
        assert not result.get("isError", False)
        assert result["content"][0]["text"] == "own-context|own-id|own-model"
        await flush_background()
    calls = events_named(sink, "$mcp_tool_call")
    assert len(calls) == 1
    assert "$mcp_llm_model" not in calls[0]["properties"]
