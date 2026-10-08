from unittest.mock import Mock, patch

import pytest

from posthog import contexts
from posthog.client import Client
from posthog.integrations.asgi import PosthogASGIMiddleware


def http_scope(**overrides):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "https",
        "method": "GET",
        "path": "/api/items",
        "raw_path": b"/api/items",
        "query_string": b"token=secret",
        "headers": [
            (b"host", b"api.example.com"),
            (b"user-agent", b"test-agent/1.0"),
            (b"x-forwarded-for", b"203.0.113.5, 10.0.0.1"),
            (b"x-posthog-session-id", b"session-123"),
            (b"x-posthog-distinct-id", b"user-456"),
        ],
        "client": ("198.51.100.8", 1234),
        "server": ("api.example.com", 443),
    }
    scope.update(overrides)
    return scope


async def noop_receive():
    return {"type": "http.disconnect"}


async def noop_send(message):
    return None


@pytest.mark.asyncio
async def test_adds_request_properties_and_tracing_context_then_restores_parent():
    observed = {}

    async def app(scope, receive, send):
        observed["session_id"] = contexts.get_context_session_id()
        observed["distinct_id"] = contexts.get_context_distinct_id()
        observed["properties"] = contexts.get_tags()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def send(message):
        sent.append(message)

    with contexts.new_context(fresh=True):
        contexts.identify_context("parent-user")
        contexts.tag("parent-property", "kept")
        middleware = PosthogASGIMiddleware(app, trust_tracing_headers=True)
        await middleware(http_scope(method="POST"), noop_receive, send)

        assert contexts.get_context_distinct_id() == "parent-user"
        assert contexts.get_context_session_id() is None
        assert contexts.get_tags() == {"parent-property": "kept"}

    assert observed["session_id"] == "session-123"
    assert observed["distinct_id"] == "user-456"
    assert observed["properties"] == {
        "parent-property": "kept",
        "$request_method": "POST",
        "$request_path": "/api/items",
        "$user_agent": "test-agent/1.0",
        "$raw_user_agent": "test-agent/1.0",
        "$ip": "203.0.113.5",
        "$current_url": "https://api.example.com/api/items",
    }
    assert sent == [
        {"type": "http.response.start", "status": 204, "headers": []},
        {"type": "http.response.body", "body": b""},
    ]
    assert "secret" not in observed["properties"]["$current_url"]


@pytest.mark.asyncio
async def test_ignores_client_controlled_tracing_headers_by_default():
    observed = {}

    async def app(scope, receive, send):
        observed["session_id"] = contexts.get_context_session_id()
        observed["distinct_id"] = contexts.get_context_distinct_id()

    await PosthogASGIMiddleware(app)(http_scope(), noop_receive, noop_send)

    assert observed == {"session_id": None, "distinct_id": None}


@pytest.mark.asyncio
async def test_sanitizes_tracing_headers_and_uses_socket_ip_fallback():
    observed = {}

    async def app(scope, receive, send):
        observed["session_id"] = contexts.get_context_session_id()
        observed["distinct_id"] = contexts.get_context_distinct_id()
        observed["properties"] = contexts.get_tags()

    scope = http_scope(
        headers=[
            (b"host", b"example.com"),
            (b"x-posthog-session-id", b"  session\n-123  "),
            (b"x-posthog-distinct-id", b" user\t-456 "),
        ]
    )
    await PosthogASGIMiddleware(app, trust_tracing_headers=True)(
        scope, noop_receive, noop_send
    )

    assert observed["session_id"] == "session-123"
    assert observed["distinct_id"] == "user-456"
    assert observed["properties"]["$ip"] == "198.51.100.8"


@pytest.mark.asyncio
async def test_malformed_and_duplicate_headers_are_handled_safely():
    observed = {}

    async def app(scope, receive, send):
        observed["distinct_id"] = contexts.get_context_distinct_id()

    scope = http_scope(
        headers=[
            (b"x-posthog-distinct-id", b"first"),
            (b"X-POSTHOG-DISTINCT-ID", b"second"),
            ("not-bytes", "ignored"),
            (b"incomplete",),
        ]
    )
    await PosthogASGIMiddleware(app, trust_tracing_headers=True)(
        scope, noop_receive, noop_send
    )

    assert observed["distinct_id"] == "first"


@pytest.mark.asyncio
async def test_captures_exception_with_client_and_preserves_propagation():
    client = Mock()
    error = RuntimeError("application failed")

    async def app(scope, receive, send):
        assert contexts.get_tags()["$request_path"] == "/api/items"
        raise error

    middleware = PosthogASGIMiddleware(app, client=client)

    with pytest.raises(RuntimeError, match="application failed") as raised:
        await middleware(http_scope(), noop_receive, noop_send)

    assert raised.value is error
    client.capture_exception.assert_called_once_with(
        error,
        _capture_metadata={
            "level": "error",
            "source": "asgi.middleware",
            "mechanism": {"type": "middleware", "handled": False},
        },
    )


@pytest.mark.asyncio
async def test_captured_event_uses_canonical_framework_boundary_metadata():
    error = RuntimeError("application failed")

    async def app(scope, receive, send):
        raise error

    client = Client("test-api-key", sync_mode=True)
    try:
        with patch.object(client, "capture", return_value="event-id") as capture:
            with pytest.raises(RuntimeError, match="application failed"):
                await PosthogASGIMiddleware(app, client=client)(
                    http_scope(), noop_receive, noop_send
                )

        properties = capture.call_args.kwargs["properties"]
        outermost = properties["$exception_list"][0]
        assert properties["$exception_level"] == "error"
        assert properties["$exception_source"] == "asgi.middleware"
        assert outermost["mechanism"] == {
            "type": "middleware",
            "handled": False,
            "exception_id": 0,
            "synthetic": False,
        }
    finally:
        client.shutdown()


@pytest.mark.asyncio
async def test_captures_exception_with_global_client():
    error = ValueError("bad request handler")

    async def app(scope, receive, send):
        raise error

    with patch("posthog.capture_exception") as capture_exception:
        with pytest.raises(ValueError, match="bad request handler"):
            await PosthogASGIMiddleware(app)(http_scope(), noop_receive, noop_send)

    capture_exception.assert_called_once_with(
        error,
        _capture_metadata={
            "level": "error",
            "source": "asgi.middleware",
            "mechanism": {"type": "middleware", "handled": False},
        },
    )


@pytest.mark.asyncio
async def test_can_disable_exception_capture():
    client = Mock()

    async def app(scope, receive, send):
        raise LookupError("not captured")

    with pytest.raises(LookupError, match="not captured"):
        await PosthogASGIMiddleware(app, client=client, capture_exceptions=False)(
            http_scope(), noop_receive, noop_send
        )

    client.capture_exception.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_filter", [False, True])
async def test_request_filter_bypasses_all_instrumentation(async_filter):
    observed = {}

    async def app(scope, receive, send):
        observed["session_id"] = contexts.get_context_session_id()
        observed["properties"] = contexts.get_tags()

    if async_filter:

        async def request_filter(scope):
            return False

    else:

        def request_filter(scope):
            return False

    with contexts.new_context(fresh=True):
        contexts.tag("existing", True)
        await PosthogASGIMiddleware(app, request_filter=request_filter)(
            http_scope(), noop_receive, noop_send
        )

    assert observed == {"session_id": None, "properties": {"existing": True}}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_properties", [False, True])
async def test_extra_properties_supports_sync_and_async_callbacks(async_properties):
    observed = {}

    async def app(scope, receive, send):
        observed.update(contexts.get_tags())

    if async_properties:

        async def extra_properties(scope):
            return {"framework": "fastapi"}

    else:

        def extra_properties(scope):
            return {"framework": "starlette"}

    await PosthogASGIMiddleware(app, extra_properties=extra_properties)(
        http_scope(), noop_receive, noop_send
    )

    assert observed["framework"] == ("fastapi" if async_properties else "starlette")


@pytest.mark.asyncio
async def test_websocket_scope_is_instrumented():
    observed = {}

    async def app(scope, receive, send):
        observed["session_id"] = contexts.get_context_session_id()
        observed["path"] = contexts.get_tags()["$request_path"]

    scope = http_scope(type="websocket", scheme="wss", method=None, path="/socket")
    await PosthogASGIMiddleware(app, trust_tracing_headers=True)(
        scope, noop_receive, noop_send
    )

    assert observed == {"session_id": "session-123", "path": "/socket"}


@pytest.mark.asyncio
async def test_lifespan_scope_passes_through_without_context():
    scope = {"type": "lifespan"}
    observed = {}

    async def app(received_scope, receive, send):
        observed["scope"] = received_scope
        observed["properties"] = contexts.get_tags()

    with contexts.new_context(fresh=True):
        contexts.tag("existing", "value")
        await PosthogASGIMiddleware(app)(scope, noop_receive, noop_send)

    assert observed == {"scope": scope, "properties": {"existing": "value"}}


@pytest.mark.asyncio
async def test_builds_url_from_server_when_host_header_is_absent():
    observed = {}

    async def app(scope, receive, send):
        observed.update(contexts.get_tags())

    scope = http_scope(
        headers=[], scheme="http", server=("2001:db8::1", 8080), path="/health"
    )
    await PosthogASGIMiddleware(app)(scope, noop_receive, noop_send)

    assert observed["$current_url"] == "http://[2001:db8::1]:8080/health"
