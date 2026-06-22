"""Dogfood demo for the PostHog MCP analytics SDK.

Instruments a small FastMCP server and sends real ``$mcp_*`` events to PostHog so
you can watch them land in the MCP analytics dashboard.

Usage::

    POSTHOG_PROJECT_API_KEY=phc_xxx python examples/mcp_analytics_demo.py
    # optional: POSTHOG_HOST=https://us.i.posthog.com (default)

This drives the instrumented server's seams directly (tools/list + tool calls)
rather than spinning up a transport + client, so it's a self-contained way to
generate events.
"""

import asyncio
import os

import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP

from posthog import Posthog
from posthog.mcp import instrument
from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity

API_KEY = os.environ.get("POSTHOG_PROJECT_API_KEY")
HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
SERVER_NAME = "posthog-python-mcp-demo"


def build_server() -> FastMCP:
    server = FastMCP(SERVER_NAME)

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool()
    def divide(a: int, b: int) -> float:
        """Divide a by b."""
        return a / b

    return server


async def main() -> None:
    if not API_KEY:
        raise SystemExit(
            "Set POSTHOG_PROJECT_API_KEY (a phc_ project key) to run the demo."
        )

    posthog = Posthog(API_KEY, host=HOST)
    server = build_server()
    analytics = instrument(
        server,
        posthog,
        MCPAnalyticsOptions(
            identify=lambda request, extra: UserIdentity(
                distinct_id="python-sdk-dogfood",
                properties={"source": "posthog-python mcp demo"},
            ),
        ),
    )

    # tools/list -> $mcp_tools_list (+ context injection)
    list_handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    await list_handler(mcp_types.ListToolsRequest(method="tools/list"))

    # tool calls -> $mcp_initialize (lazy, once), $identify, $mcp_tool_call x3, $exception
    await server._tool_manager.call_tool(
        "add",
        {"a": 2, "b": 3, "context": "adding two numbers to demo the python mcp sdk"},
    )
    await server._tool_manager.call_tool(
        "divide",
        {"a": 10, "b": 2, "context": "dividing values to show a successful tool call"},
    )
    try:
        await server._tool_manager.call_tool(
            "divide",
            {"a": 1, "b": 0, "context": "dividing by zero to exercise error capture"},
        )
    except Exception:
        pass

    # custom event via the handle
    await analytics.capture("demo_feedback", {"rating": 5})

    await asyncio.sleep(0.3)  # let fire-and-forget capture tasks complete
    posthog.flush()
    posthog.shutdown()
    print(f"Sent MCP analytics events for server '{SERVER_NAME}' to {HOST}")


if __name__ == "__main__":
    asyncio.run(main())
