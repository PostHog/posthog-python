# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Transport-level client identity: the two request headers that say *which
product* is calling, where ``clientInfo`` only says which client library is.

MCP's own identity fields are too coarse to attribute usage to a surface. A
vendor ships many products on one client: Anthropic reports
``clientInfo.name = "claude-code"`` from the CLI, the Agent SDK, the VS Code
extension and the desktop app alike, so ``$mcp_client_name`` collapses all of
them into one bucket. The distinguishing detail lives in the User-Agent
parenthetical — ``claude-code/2.1.0 (cli)`` vs ``(sdk-ts)`` vs
``(claude-vscode)`` — and in vendor headers like ``x-anthropic-client``.
Capturing them is the only way a server owner can tell their surfaces apart.

We capture the raw strings and classify nothing. No vendor table, no product
labels: friendly names are resolved at query time server-side, so labels can
improve (and new surfaces appear) without waiting on an SDK release.

Deliberately separate from the client identity read out of the request body's
``_meta``, which works on every transport. Headers exist only on HTTP
transports, so everything here is a silent no-op on stdio and in-memory servers
and their events stay byte-identical to before.
"""

from __future__ import annotations

from typing import Any, Dict

from .request_headers import get_request_headers

__all__ = [
    "CLIENT_USER_AGENT_HEADER",
    "VENDOR_CLIENT_HEADER",
    "stamp_transport_identity",
]

#: Header carrying the client's product/surface, e.g. ``claude-code/2.1.0 (cli)``.
CLIENT_USER_AGENT_HEADER = "user-agent"

#: Vendor-specific client header. Anthropic's clients send it alongside the
#: User-Agent; captured verbatim as a second, independent signal rather than
#: merged into one, so a query-time resolver can prefer whichever the vendor
#: keeps stable.
VENDOR_CLIENT_HEADER = "x-anthropic-client"


def stamp_transport_identity(event: Dict[str, Any], extra: Any) -> None:
    """Stamp the transport identity onto the event being built for *this*
    request, so it carries ``$mcp_client_user_agent`` and ``$mcp_vendor_client``.

    Headers are per-request, so this writes to the event — a per-request object
    — and never to server-wide state. One instrumented server multiplexes
    concurrent requests from different clients, and caching a header into shared
    state would attribute one client's surface to another's event.

    Values are capped downstream by truncation, which runs on every capture
    path, so a hostile 1MB header cannot inflate an event. Never raises: a
    header read must not take a tool call down with it.
    """
    try:
        headers = get_request_headers(extra)
    except Exception:  # noqa: BLE001 - defensive; get_request_headers is already guarded
        return
    if not headers:
        return

    # get_request_headers lowercases keys, so a direct lookup is enough.
    user_agent = headers.get(CLIENT_USER_AGENT_HEADER)
    if user_agent:
        event["client_user_agent"] = user_agent
    vendor_client = headers.get(VENDOR_CLIENT_HEADER)
    if vendor_client:
        event["vendor_client"] = vendor_client
