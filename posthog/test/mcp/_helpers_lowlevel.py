"""Shared fixtures for the raw low-level (mcp 1.x) virtual-tool tests.

Kept out of ``_helpers`` because that module is imported under both SDK majors,
including by ``conftest``, while these seams are 1.x only.
"""

import mcp.types as mcp_types
from mcp.server.lowlevel import Server

ECHO_TOOL = mcp_types.Tool(
    name="echo",
    description="Echo",
    inputSchema={"type": "object", "properties": {"msg": {"type": "string"}}},
)


def make_paged_lowlevel(pages, name="paged"):
    """A raw low-level server whose tools/list handler serves ``pages`` (a list of
    tool lists) one page per request, chained by ``nextCursor``. The paged handler
    is registered directly into ``request_handlers`` so the wire pagination shape
    is exact; the real tool handler answers ``real tool ran``."""
    server = Server(name)

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text="real tool ran")]

    async def paged_list(req):
        cursor = getattr(getattr(req, "params", None), "cursor", None) if req else None
        index = int(cursor) if cursor else 0
        next_cursor = str(index + 1) if index + 1 < len(pages) else None
        return mcp_types.ServerResult(
            mcp_types.ListToolsResult(tools=list(pages[index]), nextCursor=next_cursor)
        )

    server.request_handlers[mcp_types.ListToolsRequest] = paged_list
    return server


def list_page(server, cursor=None):
    """Request one page. ``cursor=None`` is a first page; any string -- including
    ``""``, a valid opaque cursor -- is a continuation, so the empty case must
    not collapse to ``params=None``."""
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    params = (
        mcp_types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
    )
    return handler(mcp_types.ListToolsRequest(method="tools/list", params=params))


def call_request(name, arguments):
    return mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )


async def call_tool(server, name, arguments):
    handler = server.request_handlers[mcp_types.CallToolRequest]
    return await handler(call_request(name, arguments))


def tool_names(page):
    return [tool.name for tool in page.root.tools]
