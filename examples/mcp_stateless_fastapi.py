"""Stateless / multi-pod MCP analytics -- two ways to set it up.

A stateless MCP server issues no session id, so across pods (or per-request
transports) ``$session_id`` fragments and the client identity (the "harness",
e.g. Claude Code / Cursor) -- sent only at ``initialize`` -- is lost on any pod
that never processed the handshake. The fix is a self-encoded session token minted
onto the ``Mcp-Session-Id`` response header at ``initialize`` and replayed by the
client on every request. You never set that header by hand.

PREFERRED -- FastMCP + ``instrument()`` (no extra code)::

    from mcp.server.fastmcp import FastMCP
    from posthog import Posthog
    from posthog.mcp import instrument

    server = FastMCP("my-server", stateless_http=True)  # stateless_http is the key bit
    instrument(server, Posthog("phc_your_project_api_key"))

    # Serve via the FastMCP app factory -- instrument() already wired the mint into it:
    server.run(transport="streamable-http")
    # or:  app = server.streamable_http_app()   # then serve with uvicorn

That's it -- no middleware, no header handling. ``instrument()`` auto-wires the
mint into the server's ``streamable_http_app()`` / ``sse_app()`` factories (which
``run()`` uses), so every pod keeps one ``$session_id`` + the harness.

CUSTOM DISPATCHER -- ``PostHogMCP`` (the rest of this file):

Only when you hand-roll the MCP endpoint yourself, so there's no FastMCP server for
``instrument()`` to wire. Then add
:class:`~posthog.mcp.PostHogMcpStatelessSessionMiddleware` once and read the
recovered session with :func:`~posthog.mcp.get_mcp_session`. Run it with::

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

# Custom-dispatcher path: there's no FastMCP server for instrument() to auto-wire,
# so add the mint middleware yourself -- one line. It mints the session token onto
# the `Mcp-Session-Id` response header at `initialize` (when the client sent none)
# and decodes the replayed token on every later request. No manual header handling.
app.add_middleware(PostHogMcpStatelessSessionMiddleware)


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method")

    # Recovered by the middleware from the replayed token. On the very first
    # `initialize` it reflects the token just minted; on every later request
    # (any pod) it carries the same session id + harness.
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
