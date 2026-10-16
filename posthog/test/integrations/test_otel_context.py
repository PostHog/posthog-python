import asyncio
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from django.test import override_settings
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from posthog import contexts
from posthog.integrations.django import PosthogContextMiddleware
from posthog.integrations.otel import PostHogContextSpanProcessor, enrich_current_span
from posthog.test.integrations.test_middleware import MockRequest
from posthog.test.tracing.test_client_traces import (
    make_client,
    mock_session,
    flush_and_capture,
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


@pytest.fixture
def native_client():
    client = make_client(traces={})
    with patch("posthog.tracing._transport._get_session", return_value=mock_session()):
        yield client
        client.shutdown()


@pytest.mark.parametrize(
    "attributes,expected",
    [
        ({}, {"sessionId": "session", "posthogDistinctId": "person"}),
        (
            {"sessionId": "own-session", "posthogDistinctId": "own-person"},
            {"sessionId": "own-session", "posthogDistinctId": "own-person"},
        ),
        (
            {"user.id": "authenticated"},
            {"user.id": "authenticated", "sessionId": "session"},
        ),
    ],
)
def test_child_span_snapshots_context_and_preserves_identity(
    tracing, attributes, expected
):
    tracer, exporter = tracing
    with contexts.new_context(fresh=True, capture_exceptions=False):
        contexts.identify_context("person")
        contexts.set_context_session("session")
        span = tracer.start_span("child", attributes=attributes)
        contexts.identify_context("later-person")
        contexts.set_context_session("later-session")
    span.end()
    assert dict(exporter.get_finished_spans()[0].attributes) == expected


@pytest.mark.parametrize("failure", [ImportError("optional"), RuntimeError("lookup")])
def test_current_span_lookup_failure_does_not_escape(failure):
    with patch("opentelemetry.trace.get_current_span", side_effect=failure):
        enrich_current_span()


def test_no_context_omits_identifiers(tracing):
    tracer, exporter = tracing
    with contexts.new_context(fresh=True, capture_exceptions=False):
        tracer.start_span("unlinked").end()
    assert dict(exporter.get_finished_spans()[0].attributes) == {}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_django_enriches_existing_request_and_child_spans(
    tracing, native_client, asynchronous, fail
):
    tracer, exporter = tracing

    def view(request):
        native_client.start_span("native").end()
        tracer.start_span("child").end()
        if fail:
            raise ValueError("view failed")
        return "response"

    async def async_view(request):
        return view(request)

    async def run_async(middleware, request):
        with tracer.start_as_current_span("request"):
            return await middleware(request)

    request = MockRequest(
        headers={
            "X-POSTHOG-DISTINCT-ID": "browser-person",
            "X-POSTHOG-SESSION-ID": "browser-session",
        }
    )
    with (
        override_settings(
            POSTHOG_MW_CAPTURE_EXCEPTIONS=False, POSTHOG_MW_TRUST_TRACING_HEADERS=True
        ),
        contexts.new_context(fresh=True, capture_exceptions=False),
    ):
        middleware = PosthogContextMiddleware(async_view if asynchronous else view)
        with pytest.raises(ValueError) if fail else nullcontext():
            if asynchronous:
                asyncio.run(run_async(middleware, request))
            else:
                with tracer.start_as_current_span("request"):
                    middleware(request)
        assert contexts.get_context_distinct_id() is None
        assert contexts.get_context_session_id() is None
        tracer.start_span("after-request").end()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    for name in ("request", "child"):
        assert dict(spans[name].attributes) == {
            "posthogDistinctId": "browser-person",
            "sessionId": "browser-session",
        }
    assert dict(spans["after-request"].attributes) == {}
    payload, _, _ = flush_and_capture(native_client)
    (native,) = spans_from(payload)
    assert {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in native["attributes"]
    } == {
        "posthogDistinctId": "browser-person",
        "sessionId": "browser-session",
    }


def test_async_django_requests_keep_separate_span_context(tracing):
    tracer, exporter = tracing
    both_started = asyncio.Event()
    started = 0

    async def view(request):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        tracer.start_span(request.path + "-child").end()
        return "response"

    async def run_request(middleware, identity):
        request = MockRequest(
            path=identity,
            headers={
                "X-POSTHOG-DISTINCT-ID": identity,
                "X-POSTHOG-SESSION-ID": identity + "-session",
            },
        )
        with tracer.start_as_current_span(identity):
            await middleware(request)

    async def run():
        await asyncio.gather(
            *(run_request(middleware, identity) for identity in ("one", "two"))
        )

    with override_settings(
        POSTHOG_MW_CAPTURE_EXCEPTIONS=False, POSTHOG_MW_TRUST_TRACING_HEADERS=True
    ):
        middleware = PosthogContextMiddleware(view)
        asyncio.run(run())
    for span in exporter.get_finished_spans():
        identity = span.name.split("-")[0]
        assert dict(span.attributes) == {
            "posthogDistinctId": identity,
            "sessionId": identity + "-session",
        }


def test_context_lookup_failure_does_not_break_span_creation(tracing):
    tracer, exporter = tracing
    with patch.object(
        contexts, "_get_active_span_context", side_effect=RuntimeError("context")
    ):
        tracer.start_span("child").end()
    assert len(exporter.get_finished_spans()) == 1
    assert dict(exporter.get_finished_spans()[0].attributes) == {}


@pytest.mark.parametrize("nested", [False, True])
def test_spans_started_after_django_request_do_not_use_inherited_ids(
    tracing, native_client, nested
):
    tracer, exporter = tracing

    async def run():
        release = asyncio.Event()
        tasks = []

        async def late_work(held, native_held):
            await release.wait()
            with contexts.new_context() if nested else nullcontext():
                tracer.start_span("late").end()
                native_client.start_span("native-late").end()
                assert contexts.get_context_session_id() == "session"
            held.end()
            native_held.end()

        async def view(request):
            tasks.append(
                asyncio.create_task(
                    late_work(
                        tracer.start_span("held"),
                        native_client.start_span("native-held"),
                    )
                )
            )
            return "response"

        middleware = PosthogContextMiddleware(view)
        await middleware(
            MockRequest(
                headers={
                    "X-POSTHOG-DISTINCT-ID": "person",
                    "X-POSTHOG-SESSION-ID": "session",
                }
            )
        )
        release.set()
        await asyncio.gather(*tasks)

    with override_settings(
        POSTHOG_MW_CAPTURE_EXCEPTIONS=False, POSTHOG_MW_TRUST_TRACING_HEADERS=True
    ):
        asyncio.run(run())
    spans = {span.name: dict(span.attributes) for span in exporter.get_finished_spans()}
    assert spans["late"] == {}
    assert spans["held"] == {"sessionId": "session", "posthogDistinctId": "person"}
    payload, _, _ = flush_and_capture(native_client)
    native = {span["name"]: span for span in spans_from(payload)}
    assert "attributes" not in native["native-late"]
    assert {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in native["native-held"]["attributes"]
    } == {
        "sessionId": "session",
        "posthogDistinctId": "person",
    }
