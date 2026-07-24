"""Stateless / multi-pod MCP analytics: keep one ``$session_id`` + the client
identity across pods via a session token in the ``Mcp-Session-Id`` header.

FastMCP + ``instrument()`` needs no extra code -- ``instrument()`` wires the mint
into the server's ``streamable_http_app()`` / ``sse_app()`` factories::

    server = FastMCP("my-server", stateless_http=True)
    instrument(server, Posthog("phc_..."))
    server.run(transport="streamable-http")  # or: app = server.streamable_http_app()

This file shows the other case: a custom ``PostHogMCP`` dispatcher, where there's
no server for ``instrument()`` to wire, so you add the middleware yourself. Run::

    POSTHOG_PROJECT_API_KEY=phc_xxx uvicorn examples.mcp_stateless_fastapi:app
"""

import os

from fastapi import FastAPI, Request

from posthog.mcp import (
    PostHogMCP,
    PostHogMcpStatelessSessionMiddleware,
    get_mcp_session,
)

posthog = PostHogMCP(
    os.environ.get("POSTHOG_PROJECT_API_KEY", "phc_xxx"),
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)

app = FastAPI()

# Mints the token at `initialize` and decodes the replayed one on every request.
app.add_middleware(PostHogMcpStatelessSessionMiddleware)


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method")

    # Session id + client identity recovered from the token, same across pods.
    sess = get_mcp_session(request)
    session_id = sess.session_id if sess else None
    client_name = sess.client_name if sess else None
    client_version = sess.client_version if sess else None

    if method == "initialize":
        posthog.capture_initialize(
            session_id=session_id,
            client_name=client_name,
            client_version=client_version,
            parameters=body.get("params"),
        )
        # ... return your real InitializeResult here ...
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": {}}

    if method == "tools/call":
        name = body["params"]["name"]
        prepared = posthog.prepare_tool_call(name, body["params"].get("arguments"))
        # ... dispatch prepared.args to your tool, then: ...
        posthog.capture_tool_call(
            tool_name=name,
            session_id=session_id,
            client_name=client_name,
            client_version=client_version,
            intent=prepared.intent,
            intent_source=prepared.intent_source,
        )
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": {"content": []}}

    return {"jsonrpc": "2.0", "id": body.get("id"), "result": {}}
