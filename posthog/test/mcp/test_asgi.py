"""Cancellation at the request-body boundary must not start the downstream app."""

import asyncio

import pytest

from posthog.mcp.asgi import PostHogMcpStatelessSessionMiddleware, get_mcp_session


async def test_cancellation_while_sniffing_initialize_propagates():
    calls = []
    scope = {"type": "http", "method": "POST", "headers": []}

    async def receive():
        raise asyncio.CancelledError()

    async def app(*args):
        calls.append("app")

    async def send(message):
        calls.append(message)

    middleware = PostHogMcpStatelessSessionMiddleware(app)
    with pytest.raises(asyncio.CancelledError):
        await middleware(scope, receive, send)

    assert calls == []
    assert get_mcp_session(scope) is None
