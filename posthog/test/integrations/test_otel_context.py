import asyncio
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from django.test import override_settings
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
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
def tracing(monkeypatch):
    monkeypatch.setattr("posthog.project_api_key", "phc_key")
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


@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize(
    "owner",
    ["scope", "inherited", "fresh", "global-client", "global-setting", "global-no-key"],
)
def test_enrichment_respects_owning_client_disabled_state(
    tracing, monkeypatch, owner, disabled
):
    tracer, exporter = tracing
    client = make_client(send=False, disabled=disabled)
    monkeypatch.setattr(
        "posthog.default_client", client if owner == "global-client" else None
    )
    monkeypatch.setattr(
        "posthog.disabled",
        disabled
        if owner in {"global-setting", "global-no-key"}
        else owner in {"scope", "inherited", "fresh"} and not disabled,
    )
    monkeypatch.setattr("posthog.api_key", None)
    monkeypatch.setattr(
        "posthog.project_api_key", "phc_key" if owner == "global-setting" else " "
    )
    disabled = disabled or owner == "global-no-key"
    try:
        with contexts.new_context(
            fresh=True,
            capture_exceptions=False,
            client=client if owner in {"scope", "inherited", "fresh"} else None,
        ):
            nested = (
                contexts.new_context(fresh=owner == "fresh", capture_exceptions=False)
                if owner in {"inherited", "fresh"}
                else nullcontext()
            )
            with (
                nested,
                tracer.start_as_current_span(
                    "request", attributes={"posthogDistinctId": "application-person"}
                ),
            ):
                contexts.identify_context("person")
                contexts.set_context_session("session")
                enrich_current_span()
                tracer.start_span("child", attributes={"app.key": "keep"}).end()
    finally:
        client.shutdown()
    spans = {span.name: dict(span.attributes) for span in exporter.get_finished_spans()}
    assert spans["request"] == {"posthogDistinctId": "application-person"} | (
        {} if disabled else {"sessionId": "session"}
    )
    assert spans["child"] == {"app.key": "keep"} | (
        {} if disabled else {"posthogDistinctId": "person", "sessionId": "session"}
    )


@pytest.mark.parametrize("outer_disabled", [False, True])
def test_explicit_client_owns_fresh_nested_enrichment(
    tracing, monkeypatch, outer_disabled
):
    tracer, exporter = tracing
    outer = make_client(send=False, disabled=outer_disabled)
    inner = make_client(send=False, disabled=not outer_disabled)
    monkeypatch.setattr("posthog.default_client", outer)
    monkeypatch.setattr("posthog.disabled", outer_disabled)
    try:
        with contexts.new_context(fresh=True, client=outer, capture_exceptions=False):
            contexts.identify_context("outer-person")
            with contexts.new_context(
                fresh=True, client=inner, capture_exceptions=False
            ):
                contexts.identify_context("inner-person")
                contexts.set_context_session("inner-session")
                tracer.start_span("child").end()
    finally:
        inner.shutdown()
        outer.shutdown()
    assert dict(exporter.get_finished_spans()[0].attributes) == (
        {"posthogDistinctId": "inner-person", "sessionId": "inner-session"}
        if outer_disabled
        else {}
    )


@pytest.mark.parametrize("initially_disabled", [False, True])
def test_enrichment_observes_runtime_disabled_changes(tracing, initially_disabled):
    tracer, exporter = tracing
    client = make_client(send=False, disabled=initially_disabled)
    try:
        with contexts.new_context(fresh=True, client=client, capture_exceptions=False):
            contexts.identify_context("person")
            contexts.set_context_session("session")
            before = tracer.start_span("before")
            client.disabled = True
            with tracer.start_as_current_span(
                "disabled", attributes={"sessionId": "application-session"}
            ):
                enrich_current_span()
                tracer.start_span("disabled-child").end()
            client.disabled = False
            tracer.start_span("enabled").end()
            before.end()
    finally:
        client.shutdown()
    spans = {span.name: dict(span.attributes) for span in exporter.get_finished_spans()}
    ids = {"posthogDistinctId": "person", "sessionId": "session"}
    assert spans["before"] == ({} if initially_disabled else ids)
    assert spans["disabled"] == {"sessionId": "application-session"}
    assert spans["disabled-child"] == {}
    assert spans["enabled"] == ids


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
            POSTHOG_MW_CAPTURE_EXCEPTIONS=False, POSTHOG_MW_CAPTURE_TRACING_HEADERS=True
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
        POSTHOG_MW_CAPTURE_EXCEPTIONS=False, POSTHOG_MW_CAPTURE_TRACING_HEADERS=True
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
@pytest.mark.parametrize(
    "own_ids",
    [
        {},
        {"distinct_id": "background-person"},
        {"session_id": "background-session"},
        {"distinct_id": "background-person", "session_id": "background-session"},
    ],
)
def test_spans_started_after_django_request_do_not_use_inherited_ids(
    tracing, native_client, nested, own_ids
):
    tracer, exporter = tracing

    async def run():
        release = asyncio.Event()
        tasks = []

        async def late_work(held, native_held):
            await release.wait()
            with contexts.new_context() if nested else nullcontext():
                if "distinct_id" in own_ids:
                    contexts.identify_context(own_ids["distinct_id"])
                if "session_id" in own_ids:
                    contexts.set_context_session(own_ids["session_id"])
                tracer.start_span("late").end()
                native_client.start_span("native-late").end()
                assert contexts.get_context_session_id() == own_ids.get(
                    "session_id", "session"
                )
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
        POSTHOG_MW_CAPTURE_EXCEPTIONS=False, POSTHOG_MW_CAPTURE_TRACING_HEADERS=True
    ):
        asyncio.run(run())
    spans = {span.name: dict(span.attributes) for span in exporter.get_finished_spans()}
    expected = {
        key: own_ids[field]
        for field, key in (
            ("distinct_id", "posthogDistinctId"),
            ("session_id", "sessionId"),
        )
        if nested and field in own_ids
    }
    assert spans["late"] == expected
    assert spans["held"] == {"sessionId": "session", "posthogDistinctId": "person"}
    payload, _, _ = flush_and_capture(native_client)
    native = {span["name"]: span for span in spans_from(payload)}
    assert {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in native["native-late"].get("attributes", [])
    } == expected
    assert {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in native["native-held"]["attributes"]
    } == {
        "sessionId": "session",
        "posthogDistinctId": "person",
    }


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 128])
@pytest.mark.parametrize("current_span", [False, True])
def test_enrichment_does_not_evict_application_attributes(
    monkeypatch, limit, current_span
):
    monkeypatch.setattr("posthog.project_api_key", "phc_key")
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=limit))
    provider.add_span_processor(PostHogContextSpanProcessor())
    tracer = provider.get_tracer(__name__)
    attributes = {"posthogDistinctId": "application-person", "other": "keep"}
    baseline = tracer.start_span("baseline", attributes=attributes)
    baseline.end()
    try:
        with contexts.new_context(fresh=True, capture_exceptions=False):
            if not current_span:
                contexts.set_context_session("session")
            with tracer.start_as_current_span("span", attributes=attributes) as span:
                if current_span:
                    contexts.set_context_session("session")
                    enrich_current_span()
                retained = dict(list(attributes.items())[-limit:]) if limit else {}
                expected = retained | (
                    {"sessionId": "session"} if limit > len(attributes) else {}
                )
                assert dict(span.attributes) == expected
                assert span.dropped_attributes == baseline.dropped_attributes
    finally:
        provider.shutdown()
