"""Stateless / multi-pod MCP analytics: keep one ``$session_id`` + the client
identity across pods via a session token in the ``Mcp-Session-Id`` header.

``instrument()`` wires the mint into FastMCP's ``streamable_http_app()`` /
``sse_app()`` factories, so a stateless server needs nothing extra. Run::

    POSTHOG_PROJECT_API_KEY=phc_xxx python examples/mcp_stateless.py
"""

import os

from mcp.server.fastmcp import FastMCP

from posthog import Posthog
from posthog.mcp import instrument

posthog = Posthog(
    os.environ.get("POSTHOG_PROJECT_API_KEY", "phc_xxx"),
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)

server = FastMCP("my-server", stateless_http=True)


@server.tool()
def greet(name: str) -> str:
    """Say hello."""
    return f"hi {name}"


instrument(server, posthog)


if __name__ == "__main__":
    # instrument() already wired the session-token mint into this app, so every pod
    # keeps one $session_id + the harness. (app = server.streamable_http_app() too.)
    server.run(transport="streamable-http")


# No FastMCP server to wire (a custom dispatcher)? Add the middleware to your own
# ASGI app and read the recovered session per request:
#
#     from posthog.mcp import PostHogMcpStatelessSessionMiddleware, get_mcp_session
#
#     app.add_middleware(PostHogMcpStatelessSessionMiddleware)
#     sess = get_mcp_session(request)   # sess.session_id, sess.client_name, ...
