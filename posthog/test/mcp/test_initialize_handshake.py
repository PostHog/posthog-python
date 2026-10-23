"""``$mcp_initialize`` is one event per ``initialize`` the client actually sent.

The spec (sdk-specs ``mcp-analytics``) allows ``$mcp_initialize`` only for a legacy
``initialize`` request, and never without one. Python cannot wrap ``initialize``
the way posthog-js does — mcp 1.x answers it inside ``ServerSession`` and mcp 2.x
refuses a handler for it — so these tests drive a real streamable-HTTP app over
raw JSON-RPC and count what comes out. Written once, collected under both SDK
majors; the version-specific code is :func:`make_pod`, which builds each server
shape a host might run.

The shapes are the ones agents produce in the field: a client that reconnects
before every few calls (Codex, hosted connectors), a probe that handshakes and
leaves, and a stateless fleet behind a load balancer with no stickiness, where
one handshake's requests land on several pods.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
import pytest

from posthog.mcp import (
    MCPAnalyticsOptions,
    UserIdentity,
    decode_session_id,
    get_request_headers,
    instrument,
)
from posthog.test.mcp._helpers import (
    MCP_MAJOR,
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)

PROTOCOL_VERSION = "2025-06-18"
BASE_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


#: The server shapes a host builds, each served the way that shape is served:
#: the official SDK's high-level server, standalone ``fastmcp`` (jlowin's 2.x/3.x
#: on mcp 1.x, 4.x on mcp 2.x), and a low-level ``Server``.
KINDS = ["official", "fastmcp", "lowlevel"]


def make_pod(
    client: FakeClient,
    *,
    kind: str = "official",
    stateless: bool = True,
    options: Optional[MCPAnalyticsOptions] = None,
    bind: bool = True,
):
    """One instrumented server of ``kind``, as ``(asgi_app, lifespan, server)``.

    ``bind`` only matters for an mcp 1.x low-level server: nothing autowires it,
    so the middleware is mounted by hand, with or without ``server=``."""
    options = options or MCPAnalyticsOptions(enable_conversation_id=False)
    if kind == "fastmcp":
        return _fastmcp_pod(client, options, stateless)
    if kind == "lowlevel":
        return _lowlevel_pod(client, options, stateless, bind)
    if MCP_MAJOR < 2:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings

        server = FastMCP(
            "handshake",
            stateless_http=stateless,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
    else:
        from mcp.server.mcpserver import MCPServer

        server = MCPServer("handshake")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    instrument(server, client, options)
    if MCP_MAJOR < 2:
        app = server.streamable_http_app()
    else:
        app = server.streamable_http_app(json_response=True, stateless_http=stateless)
    return app, server.session_manager.run(), server


def _fastmcp_pod(client, options, stateless):
    fastmcp = pytest.importorskip("fastmcp")

    server = fastmcp.FastMCP("handshake")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    instrument(server, client, options)
    app = server.http_app(json_response=True, stateless_http=stateless)
    return app, app.router.lifespan_context(app), server


def _lowlevel_pod(client, options, stateless, bind):
    import mcp.types as mcp_types
    from mcp.server.lowlevel import Server

    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    }

    def total(arguments):
        return str((arguments or {}).get("a", 0) + (arguments or {}).get("b", 0))

    if MCP_MAJOR >= 2:

        async def on_list_tools(ctx, params):
            return mcp_types.ListToolsResult(
                tools=[mcp_types.Tool(name="add", input_schema=schema)]
            )

        async def on_call_tool(ctx, params):
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(type="text", text=total(params.arguments))
                ]
            )

        server = Server(
            "handshake", on_list_tools=on_list_tools, on_call_tool=on_call_tool
        )
        instrument(server, client, options)
        app = server.streamable_http_app(json_response=True, stateless_http=stateless)
        return app, server.session_manager.run(), server

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from posthog.mcp import PostHogMcpStatelessSessionMiddleware

    server = Server("handshake")

    @server.list_tools()
    async def list_tools_handler():
        return [mcp_types.Tool(name="add", inputSchema=schema)]

    @server.call_tool()
    async def call_tool_handler(name, arguments):
        return [mcp_types.TextContent(type="text", text=total(arguments))]

    instrument(server, client, options)
    manager = StreamableHTTPSessionManager(
        app=server, stateless=stateless, json_response=True
    )
    app = (
        PostHogMcpStatelessSessionMiddleware(manager.handle_request, server=server)
        if bind
        else PostHogMcpStatelessSessionMiddleware(manager.handle_request)
    )
    return app, manager.run(), server


@asynccontextmanager
async def fleet(client: FakeClient, pods: int = 1, **pod_options: Any):
    """``pods`` independently instrumented servers, each a live HTTP surface. Every
    pod has its own tracking state, exactly like separate processes behind a
    load balancer."""
    async with AsyncExitStack() as stack:
        surfaces: List[httpx.AsyncClient] = []
        for _ in range(pods):
            app, lifespan, _ = make_pod(client, **pod_options)
            await stack.enter_async_context(lifespan)
            surfaces.append(
                await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://127.0.0.1:8000",
                    )
                )
            )
        yield RoundRobin(surfaces)


class RoundRobin:
    """A load balancer with no stickiness: each request goes to the next pod."""

    def __init__(self, pods: List[httpx.AsyncClient]):
        self.pods = pods
        self.turn = 0

    async def post(
        self, body: Dict[str, Any], headers: Dict[str, str]
    ) -> httpx.Response:
        pod = self.pods[self.turn % len(self.pods)]
        self.turn += 1
        return await pod.post("/mcp", json=body, headers=headers)


def rpc(method: str, params: Dict[str, Any], request_id: Optional[int] = 1):
    body: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        body["id"] = request_id
    return body


def initialize_params(client_name: str) -> Dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": client_name, "version": "0.50.0"},
    }


async def handshake(
    lb: RoundRobin,
    *,
    client_name: str = "codex-mcp-client",
    extra_headers: Optional[Dict[str, str]] = None,
    send_initialized: bool = True,
):
    """``initialize`` then ``notifications/initialized``, as every MCP client
    connects. Returns the headers the client replays afterwards and the
    negotiated protocol version."""
    headers = {**BASE_HEADERS, **(extra_headers or {})}
    response = await lb.post(rpc("initialize", initialize_params(client_name)), headers)
    assert response.status_code == 200, response.text
    negotiated = response.json()["result"]["protocolVersion"]
    replay = {**headers, "mcp-protocol-version": negotiated}
    session_header = response.headers.get("mcp-session-id")
    if session_header:
        replay["mcp-session-id"] = session_header
    if send_initialized:
        await lb.post(rpc("notifications/initialized", {}, None), replay)
    return replay, negotiated


async def call_add(
    lb: RoundRobin, headers: Dict[str, str], request_id: int = 3, **extra: Any
):
    """``tools/call add``. ``extra`` adds arguments or params (``_meta``)."""
    meta = {"_meta": extra.pop("_meta")} if "_meta" in extra else {}
    arguments = {"a": 2, "b": 3, "context": "adding", **extra}
    response = await lb.post(
        rpc("tools/call", {"name": "add", "arguments": arguments, **meta}, request_id),
        headers,
    )
    assert response.status_code == 200, response.text
    return response


async def list_tools(lb: RoundRobin, headers: Dict[str, str]):
    response = await lb.post(rpc("tools/list", {}, 2), headers)
    assert response.status_code == 200, response.text
    return response


# --- one event per handshake ------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_one_initialize_per_handshake_across_stateless_pods(kind):
    # The overcount this file exists for: dedupe used to be per process, so every
    # pod a session touched emitted its own $mcp_initialize.
    client = FakeClient()
    async with fleet(client, pods=2, kind=kind) as lb:
        headers, negotiated = await handshake(lb)
        await list_tools(lb, headers)
        await call_add(lb, headers, 3)
        await call_add(lb, headers, 4)
        await call_add(lb, headers, 5)
    await _flush()

    inits = _events(client, "$mcp_initialize")
    assert len(inits) == 1
    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 3
    sessions = {c["properties"]["$session_id"] for c in calls}
    assert sessions == {inits[0]["properties"]["$session_id"]}
    props = inits[0]["properties"]
    assert props["$mcp_client_name"] == "codex-mcp-client"
    assert props["$mcp_client_version"] == "0.50.0"
    assert props["$mcp_protocol_version"] == negotiated
    assert (
        props["$mcp_parameters"]["params"]["clientInfo"]["name"] == "codex-mcp-client"
    )
    assert props["$mcp_duration_ms"] >= 0


@pytest.mark.parametrize("kind", KINDS)
async def test_each_reconnect_is_one_initialize(kind):
    client = FakeClient()
    async with fleet(client, pods=2, kind=kind) as lb:
        for _ in range(3):
            headers, _ = await handshake(lb)
            await list_tools(lb, headers)
            await call_add(lb, headers)
            await call_add(lb, headers, 4)
    await _flush()

    inits = _events(client, "$mcp_initialize")
    assert len(inits) == 3
    assert len({e["properties"]["$session_id"] for e in inits}) == 3
    assert len(_events(client, "$mcp_tool_call")) == 6


@pytest.mark.parametrize("kind", KINDS)
async def test_a_bare_handshake_is_counted(kind):
    # Probes and health checkers connect and leave. There is no later request to
    # hang a synthesized event on, which used to make them invisible.
    client = FakeClient()
    async with fleet(client, kind=kind) as lb:
        await handshake(lb, send_initialized=False)
        await handshake(lb)
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 2


@pytest.mark.parametrize("kind", KINDS)
async def test_initialize_session_matches_a_stateful_transport(kind):
    # On a stateful server the transport assigns Mcp-Session-Id in the initialize
    # response; the event must land in the session every later request resolves to.
    client = FakeClient()
    async with fleet(client, stateless=False, kind=kind) as lb:
        headers, _ = await handshake(lb)
        await call_add(lb, headers)
        await call_add(lb, headers, 4)
    await _flush()

    inits = _events(client, "$mcp_initialize")
    assert len(inits) == 1
    calls = _events(client, "$mcp_tool_call")
    assert {c["properties"]["$session_id"] for c in calls} == {
        inits[0]["properties"]["$session_id"]
    }


async def test_initialize_is_attributed_on_the_pod_that_served_it():
    def identify(request, extra):
        headers = get_request_headers(extra) or {}
        token = headers.get("authorization", "")
        return (
            UserIdentity(distinct_id=token.removeprefix("Bearer ")) if token else None
        )

    client = FakeClient()
    options = MCPAnalyticsOptions(enable_conversation_id=False, identify=identify)
    async with fleet(client, pods=2, options=options) as lb:
        headers, _ = await handshake(
            lb, extra_headers={"authorization": "Bearer user_alice"}
        )
        await call_add(lb, headers)
    await _flush()

    (init,) = _events(client, "$mcp_initialize")
    assert init["distinct_id"] == "user_alice"
    assert init["properties"]["$mcp_client_user_agent"]


@pytest.mark.skipif(
    MCP_MAJOR < 2,
    reason="mcp 1.x reads only the response head, and a JSON-RPC error is an HTTP 200",
)
async def test_a_failed_initialize_is_not_captured():
    client = FakeClient()
    async with fleet(client) as lb:
        response = await lb.post(rpc("initialize", {"capabilities": {}}), BASE_HEADERS)
    await _flush()

    assert "error" in response.json()
    assert _events(client, "$mcp_initialize") == []


async def test_an_echoed_conversation_does_not_initialize_again():
    # ADR-0004 anchors echoed calls to the conversation's session. That session
    # never sent an initialize, so it gets no $mcp_initialize of its own.
    from posthog.mcp._conversation_id import _uuid7

    conversation_id = _uuid7()
    client = FakeClient()
    options = MCPAnalyticsOptions(enable_conversation_id=True)
    async with fleet(client, pods=2, options=options) as lb:
        headers, _ = await handshake(lb)
        for request_id in (3, 4, 5):
            await call_add(lb, headers, request_id, conversation_id=conversation_id)
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 3
    assert {c["properties"]["$mcp_conversation_id"] for c in calls} == {conversation_id}
    assert len(_events(client, "$mcp_initialize")) == 1


# --- the 2026-07-28 revision has no initialize -------------------------------------


@pytest.mark.skipif(MCP_MAJOR < 2, reason="mcp 1.x cannot serve 2026-07-28")
async def test_modern_era_requests_emit_no_initialize():
    from posthog.test.mcp._helpers_v2 import modern_headers, modern_meta

    client = FakeClient()
    headers = {**BASE_HEADERS, **modern_headers("tools/call", "add")}
    async with fleet(client, pods=2) as lb:
        for request_id in (1, 2, 3):
            await call_add(lb, headers, request_id, _meta=modern_meta())
    await _flush()

    assert len(_events(client, "$mcp_tool_call")) == 3
    assert _events(client, "$mcp_initialize") == []


# --- mcp 1.x low-level server, middleware mounted by hand --------------------------


@pytest.mark.skipif(MCP_MAJOR >= 2, reason="mcp 2.x observes initialize in-process")
async def test_an_unbound_middleware_keeps_the_first_request_fallback():
    # Without server= the middleware cannot reach the tracking state, so the first
    # request of the session on each pod still stands in for the handshake: the
    # pre-existing behaviour, kept so a hand-mounted middleware never goes dark.
    client = FakeClient()
    async with fleet(client, pods=2, kind="lowlevel", bind=False) as lb:
        headers, _ = await handshake(lb)
        await call_add(lb, headers)
        await call_add(lb, headers, 4)
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 2


# --- stdio: a real server process, driven over its pipes ---------------------------

_STDIO_SERVER = """
import importlib.metadata
import json
import os

from posthog.mcp import MCPAnalyticsOptions, instrument


class FileClient:
    # Appends each capture to a file the test reads, one JSON line per event.
    def capture(self, event, distinct_id=None, properties=None, **kwargs):
        with open(os.environ["EVENTS_FILE"], "a") as sink:
            sink.write(json.dumps({"event": event, "properties": properties or {}}, default=str) + "\\n")


if int(importlib.metadata.version("mcp").split(".")[0]) < 2:
    from mcp.server.fastmcp import FastMCP as Server
else:
    from mcp.server.mcpserver import MCPServer as Server

server = Server("stdio-handshake")


@server.tool()
def add(a: int, b: int) -> int:
    return a + b


instrument(server, FileClient(), MCPAnalyticsOptions(enable_conversation_id=False))
server.run()
"""


class StdioServer:
    """The instrumented server as a child process on stdio, the way a desktop client
    launches it: newline-delimited JSON-RPC on stdin/stdout."""

    def __init__(self, process, events_file):
        self.process = process
        self.events_file = events_file

    async def send(self, method, params, request_id=None):
        self.process.stdin.write(
            (json.dumps(rpc(method, params, request_id)) + "\n").encode()
        )
        await self.process.stdin.drain()
        if request_id is None:
            return None
        line = await asyncio.wait_for(self.process.stdout.readline(), timeout=20)
        return json.loads(line)

    async def events(self, name, count):
        """Captures are fire-and-forget in the child: wait for ``count`` of them,
        then a moment more for any that would make it one too many."""
        for _ in range(100):
            if self.events_file.exists():
                lines = self.events_file.read_text().splitlines()
                found = [e for e in map(json.loads, lines) if e["event"] == name]
                if len(found) >= count:
                    break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        lines = self.events_file.read_text().splitlines()
        return [e for e in map(json.loads, lines) if e["event"] == name]


@asynccontextmanager
async def stdio_server(tmp_path):
    script = tmp_path / "stdio_server.py"
    script.write_text(_STDIO_SERVER)
    events_file = tmp_path / "events.jsonl"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "EVENTS_FILE": str(events_file)},
        cwd=str(tmp_path),
    )
    try:
        yield StdioServer(process, events_file)
    finally:
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def test_stdio_handshake_then_calls(tmp_path):
    async with stdio_server(tmp_path) as server:
        reply = await server.send("initialize", initialize_params("claude-desktop"), 1)
        assert "result" in reply, reply
        await server.send("notifications/initialized", {})
        await server.send("tools/list", {}, 2)
        for request_id in (3, 4):
            reply = await server.send(
                "tools/call",
                {"name": "add", "arguments": {"a": 1, "b": 2, "context": "stdio"}},
                request_id,
            )
            assert reply["result"]["isError"] is False, reply
        calls = await server.events("$mcp_tool_call", 2)
        inits = await server.events("$mcp_initialize", 1)

    assert len(calls) == 2
    assert len(inits) == 1
    props = inits[0]["properties"]
    assert props["$mcp_client_name"] == "claude-desktop"
    assert {c["properties"]["$session_id"] for c in calls} == {props["$session_id"]}


@pytest.mark.skipif(MCP_MAJOR < 2, reason="mcp 1.x stdio has no handshake seam")
async def test_stdio_bare_handshake_is_counted(tmp_path):
    async with stdio_server(tmp_path) as server:
        reply = await server.send("initialize", initialize_params("claude-desktop"), 1)
        assert "result" in reply, reply
        inits = await server.events("$mcp_initialize", 1)

    assert len(inits) == 1
    assert (
        inits[0]["properties"]["$mcp_protocol_version"]
        == reply["result"]["protocolVersion"]
    )


# --- custom dispatcher: PostHogMCP behind an unbound middleware ---------------------


def make_dispatcher(posthog_mcp):
    """A hand-rolled JSON-RPC endpoint, the shape ``PostHogMCP`` exists for: the host
    answers ``initialize`` itself and captures it through the manual API."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    from posthog.mcp import get_mcp_session

    async def endpoint(request):
        message = await request.json()
        if "id" not in message:  # a notification: accepted, nothing to answer
            return Response(status_code=202)
        result: Dict[str, Any] = {"tools": []}
        if message["method"] == "initialize":
            session = get_mcp_session(request)
            posthog_mcp.capture_initialize(
                client_name=message["params"]["clientInfo"]["name"],
                session_id=session.session_id if session else None,
            )
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "dispatcher", "version": "1.0.0"},
            }
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})

    return Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])])


async def test_a_posthogmcp_dispatcher_reports_only_its_own_initialize():
    from posthog.mcp import PostHogMCP, PostHogMcpStatelessSessionMiddleware

    captured: List[Dict[str, Any]] = []

    def keep(event):
        captured.append(event)
        return event

    posthog_mcp = PostHogMCP("phc_test", send=False, before_send=keep)
    # An instrumented server in the same process: an unbound middleware must never
    # attach the dispatcher's handshakes to it.
    bystander = FakeClient()
    make_pod(bystander)

    app = PostHogMcpStatelessSessionMiddleware(make_dispatcher(posthog_mcp))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as http:
        lb = RoundRobin([http])
        headers, _ = await handshake(lb, client_name="dispatcher-client")
        await list_tools(lb, headers)
    await _flush()

    inits = [e for e in captured if e["event"] == "$mcp_initialize"]
    assert len(inits) == 1
    assert inits[0]["properties"]["$mcp_client_name"] == "dispatcher-client"
    # The session the host read back from the minted token, not a second one.
    token = decode_session_id(headers["mcp-session-id"])
    assert token is not None
    assert inits[0]["properties"]["$session_id"] == token.session_id
    assert bystander.events == []


# --- the deprecated SSE transport: session in a query parameter ---------------------


@asynccontextmanager
async def sse_server(client: FakeClient):
    """An instrumented server's ``sse_app()`` on a real port. The SSE transport keeps
    its session in the ``session_id`` query parameter of ``/messages/``, not in a
    header, and answers each POST with 202 and the reply on the event stream."""
    import uvicorn

    if MCP_MAJOR < 2:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings

        server = FastMCP(
            "handshake",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
    else:
        from mcp.server.mcpserver import MCPServer

        server = MCPServer("handshake")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=False))
    config = uvicorn.Config(server.sse_app(), port=0, log_level="error", ws="none")
    http_server = uvicorn.Server(config)
    serving = asyncio.create_task(http_server.serve())
    while not http_server.started:
        await asyncio.sleep(0.01)
    port = http_server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        http_server.should_exit = True
        await asyncio.wait_for(serving, timeout=10)


async def test_sse_transport_handshake_then_calls():
    client = FakeClient()
    async with sse_server(client) as url:
        async with httpx.AsyncClient(base_url=url, timeout=20) as http:
            async with http.stream("GET", "/sse") as stream:
                lines = stream.aiter_lines()

                async def next_data() -> str:
                    async for line in lines:
                        if line.startswith("data:"):
                            return line[len("data:") :].strip()
                    raise AssertionError("the event stream closed")

                endpoint = await next_data()

                async def send(method, params, request_id=None):
                    response = await http.post(
                        endpoint, json=rpc(method, params, request_id)
                    )
                    assert response.status_code == 202, response.text
                    return json.loads(await next_data()) if request_id else None

                await send("initialize", initialize_params("sse-client"), 1)
                await send("notifications/initialized", {})
                await send("tools/list", {}, 2)
                for request_id in (3, 4):
                    arguments = {"a": 1, "b": 2, "context": "sse"}
                    reply = await send(
                        "tools/call",
                        {"name": "add", "arguments": arguments},
                        request_id,
                    )
                    assert reply["result"]["isError"] is False, reply
    await _flush()

    (init,) = _events(client, "$mcp_initialize")
    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 2
    assert {c["properties"]["$session_id"] for c in calls} == {
        init["properties"]["$session_id"]
    }


# --- the middleware is per request and fail-safe -----------------------------------


@pytest.mark.skipif(MCP_MAJOR >= 2, reason="mcp 2.x has no first-request fallback")
async def test_a_route_without_the_middleware_keeps_the_fallback():
    # One process, one server, two routes: the autowired app with the bound
    # middleware, and the session manager mounted by hand without it. Each route
    # reports its own handshakes, once.
    client = FakeClient()
    app, lifespan, server = make_pod(client)
    async with lifespan:
        for surface in (app, server.session_manager.handle_request):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=surface),
                base_url="http://127.0.0.1:8000",
            ) as http:
                lb = RoundRobin([http])
                headers, _ = await handshake(lb)
                await call_add(lb, headers)
                await call_add(lb, headers, 4)
    await _flush()

    inits = _events(client, "$mcp_initialize")
    assert len(inits) == 2
    calls = _events(client, "$mcp_tool_call")
    assert {c["properties"]["$session_id"] for c in calls} == {
        e["properties"]["$session_id"] for e in inits
    }


async def test_a_handshake_leaves_the_shared_session_alone():
    # McpAnalytics.capture() reads the server's shared session. A handshake that
    # carries its own session (a token here) must not move it.
    from posthog.mcp._internal import get_server_tracking_data, tracking_key

    client = FakeClient()
    app, lifespan, server = make_pod(client)
    data = get_server_tracking_data(tracking_key(server))
    before = (data.session_id, data.session_source)
    async with lifespan:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as http:
            await handshake(RoundRobin([http]))
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 1
    assert (data.session_id, data.session_source) == before


async def test_a_server_that_cannot_be_bound_does_not_break_requests():
    from posthog.mcp import PostHogMcpStatelessSessionMiddleware

    async def echo(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    for unbindable in (object(), {"not": "hashable"}):
        app = PostHogMcpStatelessSessionMiddleware(echo, server=unbindable)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as http:
            response = await http.post(
                "/mcp",
                json=rpc("initialize", initialize_params("x")),
                headers=BASE_HEADERS,
            )
        assert response.status_code == 200


@pytest.mark.parametrize("kind", KINDS)
async def test_flush_waits_for_the_handshake_capture(kind):
    # The handshake capture starts during the request. flush() must wait for it,
    # or shutdown drops $mcp_initialize when identify or before_send is slow.
    from posthog.mcp import McpAnalytics
    from posthog.mcp._internal import tracking_key

    async def slow_identify(request, extra):
        await asyncio.sleep(0.05)
        return UserIdentity(distinct_id="user_slow")

    async def slow_before_send(event):
        await asyncio.sleep(0.05)
        return event

    client = FakeClient()
    options = MCPAnalyticsOptions(
        enable_conversation_id=False,
        identify=slow_identify,
        before_send=slow_before_send,
    )
    app, lifespan, server = make_pod(client, kind=kind, options=options)
    async with lifespan:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as http:
            await handshake(RoundRobin([http]), send_initialized=False)
        await McpAnalytics(tracking_key(server)).flush()
        assert len(_events(client, "$mcp_initialize")) == 1


@pytest.mark.skipif(
    MCP_MAJOR < 2,
    reason="mcp 1.x does not read a request that carries a session id (README)",
)
@pytest.mark.parametrize("kind", KINDS)
async def test_an_initialize_with_a_session_header_keeps_that_session(kind):
    # A stateless client that sends initialize with an existing Mcp-Session-Id
    # keeps sending it, and its later requests resolve their session from it.
    client = FakeClient()
    async with fleet(client, pods=2, kind=kind) as lb:
        headers, _ = await handshake(
            lb, extra_headers={"mcp-session-id": "an-existing-session"}
        )
        assert headers["mcp-session-id"] == "an-existing-session"
        await call_add(lb, headers)
        await call_add(lb, headers, 4)
    await _flush()

    (init,) = _events(client, "$mcp_initialize")
    calls = _events(client, "$mcp_tool_call")
    assert {c["properties"]["$session_id"] for c in calls} == {
        init["properties"]["$session_id"]
    }


@pytest.mark.parametrize("kind", KINDS)
async def test_a_hung_identify_leaves_no_task_behind(kind):
    # identify runs inline in the handshake, as it does on a tool call. A callback
    # that never returns holds those requests, and leaves no analytics task behind
    # for each connection. Once the callback recovers, recording resumes.
    import contextlib

    import posthog.mcp._instrumentation as instr
    from posthog.mcp import McpAnalytics
    from posthog.mcp._internal import get_server_tracking_data, tracking_key

    recovered = asyncio.Event()

    async def identify(request, extra):
        await recovered.wait()
        return UserIdentity(distinct_id="user_1")

    client = FakeClient()
    options = MCPAnalyticsOptions(enable_conversation_id=False, identify=identify)
    app, lifespan, server = make_pod(client, kind=kind, options=options)
    data = get_server_tracking_data(tracking_key(server))

    def pending():
        with instr._tasks_lock:
            tasks = list(instr._BACKGROUND_TASKS.items())
        return [task for task, owner in tasks if owner is data and not task.done()]

    async with lifespan:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as http:
            lb = RoundRobin([http])
            for _ in range(5):  # each client gives up, as a timeout would
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(handshake(lb, send_initialized=False), 0.2)
            assert pending() == []
            await asyncio.wait_for(McpAnalytics(tracking_key(server)).flush(), 1)

            recovered.set()
            await handshake(lb, client_name="after-recovery", send_initialized=False)
    await _flush()

    names = [
        e["properties"]["$mcp_client_name"] for e in _events(client, "$mcp_initialize")
    ]
    assert "after-recovery" in names


@pytest.mark.skipif(MCP_MAJOR >= 2, reason="the mcp 1.x auth context middleware")
async def test_identify_sees_the_auth_context_of_the_handshake():
    # FastMCP's AuthContextMiddleware sets the authenticated user for the length of
    # the request and resets it on the way out. Our middleware sits outside it, so
    # the handshake must be recorded while the app still handles the request.
    from mcp.server.auth.middleware.auth_context import (
        auth_context_var,
        get_access_token,
    )
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from posthog.mcp import PostHogMcpStatelessSessionMiddleware

    def with_user(app):
        user = AuthenticatedUser(
            AccessToken(token="t", client_id="alice", scopes=[], expires_at=None)
        )

        async def authenticated(scope, receive, send):
            reset = auth_context_var.set(user)
            try:
                await app(scope, receive, send)
            finally:
                auth_context_var.reset(reset)

        return authenticated

    def identify(request, extra):
        token = get_access_token()
        return UserIdentity(distinct_id=token.client_id) if token else None

    client = FakeClient()
    options = MCPAnalyticsOptions(enable_conversation_id=False, identify=identify)
    _, lifespan, server = make_pod(client, options=options)
    app = PostHogMcpStatelessSessionMiddleware(
        with_user(server.session_manager.handle_request), server=server
    )
    async with lifespan:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as http:
            await handshake(RoundRobin([http]), send_initialized=False)
    await _flush()

    (init,) = _events(client, "$mcp_initialize")
    assert init["distinct_id"] == "alice"


@pytest.mark.parametrize("kind", KINDS)
async def test_an_escaped_initialize_still_mints_a_session(kind):
    # "\u0069nitialize" is valid JSON for "initialize". The middleware parses the
    # body as JSON, so it mints the token and captures the handshake.
    raw = (
        b'{"jsonrpc":"2.0","id":1,"method":"\\u0069nitialize","params":'
        + json.dumps(initialize_params("escaped-client")).encode()
        + b"}"
    )
    client = FakeClient()
    async with fleet(client, kind=kind) as lb:
        response = await lb.pods[0].post("/mcp", content=raw, headers=BASE_HEADERS)
    await _flush()

    assert response.status_code == 200, response.text
    assert decode_session_id(response.headers.get("mcp-session-id")) is not None
    (init,) = _events(client, "$mcp_initialize")
    assert init["properties"]["$mcp_client_name"] == "escaped-client"


@pytest.mark.skipif(
    MCP_MAJOR < 2,
    reason="mcp 1.x does not read a request that carries a session id (README)",
)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_nested_method_before_the_top_level_initialize_is_counted(kind):
    # JSON key order is arbitrary: params can come first, and can hold their own
    # "method" key. Only the top-level method says whether this is a handshake.
    params = initialize_params("nested-client")
    params["capabilities"] = {
        "experimental": {"vendor": {"method": "custom", "padding": "x" * 8192}}
    }
    body = {"params": params, "jsonrpc": "2.0", "id": 1, "method": "initialize"}
    headers = {**BASE_HEADERS, "mcp-session-id": "an-existing-session"}
    client = FakeClient()
    async with fleet(client, kind=kind) as lb:
        response = await lb.post(body, headers)
    await _flush()

    assert response.status_code == 200, response.text
    (init,) = _events(client, "$mcp_initialize")
    assert init["properties"]["$mcp_client_name"] == "nested-client"
