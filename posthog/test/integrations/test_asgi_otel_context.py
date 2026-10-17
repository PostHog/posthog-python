import asyncio
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from posthog import contexts
from posthog.integrations.asgi import PosthogASGIMiddleware
from posthog.integrations.otel import PostHogContextSpanProcessor
from posthog.test.integrations.test_asgi_integration import (
    http_scope,
    noop_receive,
    noop_send,
)
from posthog.test.tracing.test_client_traces import (
    flush_and_capture,
    make_client,
    mock_session,
    spans_from,
)


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(PostHogContextSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer(__name__), exporter
    provider.shutdown()


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
@pytest.mark.parametrize(
    "trust,headers,attributes,fail,expected",
    [
        (
            True,
            None,
            {},
            False,
            {"sessionId": "session-123", "posthogDistinctId": "user-456"},
        ),
        (
            True,
            None,
            {},
            True,
            {"sessionId": "session-123", "posthogDistinctId": "user-456"},
        ),
        (False, None, {}, False, {}),
        (True, [], {}, False, {}),
        (
            True,
            [(b"x-posthog-session-id", b"\x00\n"), (b"x-posthog-distinct-id", b"\t ")],
            {},
            False,
            {},
        ),
        (
            True,
            None,
            {"user.id": "authenticated"},
            False,
            {"user.id": "authenticated", "sessionId": "session-123"},
        ),
        (
            True,
            None,
            {"sessionId": "explicit-session", "posthogDistinctId": "explicit-person"},
            False,
            {"sessionId": "explicit-session", "posthogDistinctId": "explicit-person"},
        ),
    ],
)
async def test_asgi_enriches_existing_request_and_child_spans(
    tracing, scope_type, trust, headers, attributes, fail, expected
):
    tracer, exporter = tracing

    async def app(scope, receive, send):
        tracer.start_span("child", attributes=attributes).end()
        if fail:
            raise ValueError("handler failed")
        await send(
            {"type": "websocket.close"}
            if scope_type == "websocket"
            else {"type": "http.response.body", "body": b""}
        )

    scope = http_scope(type=scope_type)
    if headers is not None:
        scope["headers"] = headers
    middleware = PosthogASGIMiddleware(
        app, capture_exceptions=False, trust_tracing_headers=trust
    )
    with contexts.new_context(fresh=True, capture_exceptions=False):
        with (
            pytest.raises(ValueError, match="handler failed") if fail else nullcontext()
        ):
            with tracer.start_as_current_span("request", attributes=attributes):
                await middleware(scope, noop_receive, noop_send)
        assert contexts.get_context_session_id() is None
        assert contexts.get_context_distinct_id() is None
        tracer.start_span("after-request").end()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    for name in ("request", "child"):
        assert dict(spans[name].attributes) == expected
    assert dict(spans["after-request"].attributes) == {}


async def test_asgi_concurrent_requests_and_ended_task_context(tracing):
    tracer, exporter = tracing
    both_started = asyncio.Event()
    finished = asyncio.Event()
    held_spans = []
    background_tasks = []

    async def background(name):
        await finished.wait()
        tracer.start_span("late-" + name).end()

    async def app(scope, receive, send):
        name = scope["path"]
        held_spans.append(tracer.start_span("held-" + name))
        background_tasks.append(asyncio.create_task(background(name)))
        if len(held_spans) == 2:
            both_started.set()
        await both_started.wait()
        tracer.start_span("child-" + name).end()

    middleware = PosthogASGIMiddleware(app, trust_tracing_headers=True)

    async def request(name):
        with tracer.start_as_current_span("request-" + name):
            await middleware(
                http_scope(
                    path=name,
                    headers=[
                        (b"x-posthog-session-id", ("session-" + name).encode()),
                        (b"x-posthog-distinct-id", ("person-" + name).encode()),
                    ],
                ),
                noop_receive,
                noop_send,
            )

    with contexts.new_context(fresh=True, capture_exceptions=False):
        await asyncio.gather(request("first"), request("second"))
        finished.set()
        await asyncio.gather(*background_tasks)
        for span in held_spans:
            span.end()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    for name in ("first", "second"):
        for prefix in ("request-", "child-", "held-"):
            assert dict(spans[prefix + name].attributes) == {
                "sessionId": "session-" + name,
                "posthogDistinctId": "person-" + name,
            }
        assert dict(spans["late-" + name].attributes) == {}


@pytest.mark.parametrize("scope_type,filtered", [("http", True), ("lifespan", False)])
async def test_asgi_bypassed_scopes_do_not_enrich_spans(tracing, scope_type, filtered):
    tracer, exporter = tracing

    async def app(scope, receive, send):
        tracer.start_span("child").end()

    middleware = PosthogASGIMiddleware(
        app,
        trust_tracing_headers=True,
        request_filter=(lambda scope: False) if filtered else None,
    )
    with contexts.new_context(fresh=True, capture_exceptions=False):
        with tracer.start_as_current_span("request"):
            await middleware(http_scope(type=scope_type), noop_receive, noop_send)
    assert all(dict(span.attributes) == {} for span in exporter.get_finished_spans())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_fastapi_sync_and_async_handlers_enrich_native_and_otel_spans(
    tracing, asynchronous, fail
):
    tracer, exporter = tracing
    native = make_client(traces={})

    def handler():
        native.start_span("native").end()
        tracer.start_span("child").end()
        if fail:
            raise ValueError("handler failed")
        return {"ok": True}

    async def async_handler():
        return handler()

    app = FastAPI()
    app.add_api_route("/api/items", async_handler if asynchronous else handler)
    app.add_middleware(
        PosthogASGIMiddleware, trust_tracing_headers=True, capture_exceptions=False
    )
    responses = []

    async def send(message):
        responses.append(message)

    try:
        with (
            patch(
                "posthog.tracing._transport._get_session", return_value=mock_session()
            ),
            contexts.new_context(fresh=True, capture_exceptions=False),
        ):
            with (
                pytest.raises(ValueError, match="handler failed")
                if fail
                else nullcontext()
            ):
                with tracer.start_as_current_span("request"):
                    await app(http_scope(), noop_receive, send)
            assert contexts.get_context_session_id() is None
            assert contexts.get_context_distinct_id() is None
            payload, _, _ = flush_and_capture(native)
    finally:
        native.shutdown()

    assert responses[0]["status"] == (500 if fail else 200)
    spans = {span.name: span for span in exporter.get_finished_spans()}
    for name in ("request", "child"):
        assert dict(spans[name].attributes) == {
            "sessionId": "session-123",
            "posthogDistinctId": "user-456",
        }
    [span] = spans_from(payload)
    attributes = {item["key"]: item["value"] for item in span["attributes"]}
    assert attributes["sessionId"] == {"stringValue": "session-123"}
    assert attributes["posthogDistinctId"] == {"stringValue": "user-456"}
