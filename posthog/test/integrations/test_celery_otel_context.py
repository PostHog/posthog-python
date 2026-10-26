from unittest.mock import Mock

import pytest
from celery import Celery
from opentelemetry import trace
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from posthog import contexts
from posthog.integrations.celery import (
    CONTEXT_DISTINCT_ID_HEADER,
    CONTEXT_SESSION_ID_HEADER,
    PosthogCeleryIntegration,
)
from posthog.integrations.otel import PostHogContextSpanProcessor


@pytest.fixture
def celery_tracing():
    app = Celery("span-context", broker="memory://")
    integration = PosthogCeleryIntegration(
        client=Mock(disabled=False),
        capture_exceptions=False,
        capture_task_lifecycle_events=False,
    )
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(PostHogContextSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    integration.instrument()
    try:
        yield app, provider.get_tracer(__name__), exporter, provider
    finally:
        integration.shutdown()
        provider.shutdown()
        app.close()


@pytest.mark.parametrize("state", ["success", "failure", "retry"])
@pytest.mark.parametrize("propagated", [True, False])
def test_eager_task_enriches_existing_span_and_clears_context(
    celery_tracing, state, propagated
):
    app, tracer, exporter, _ = celery_tracing
    headers = (
        {
            CONTEXT_DISTINCT_ID_HEADER: "job-person",
            CONTEXT_SESSION_ID_HEADER: "job-session",
        }
        if propagated
        else {}
    )
    expected = (
        {"posthogDistinctId": "job-person", "sessionId": "job-session"}
        if propagated
        else {}
    )
    attempts = []

    @app.task(bind=True, name="span-context.task")
    def task(self):
        attempts.append(self.request.retries)
        tracer.start_span("child").end()
        assert contexts.get_context_distinct_id() == (
            "job-person" if propagated else None
        )
        assert contexts.get_context_session_id() == (
            "job-session" if propagated else None
        )
        if state == "failure":
            raise ValueError("task failed")
        if state == "retry" and self.request.retries == 0:
            raise self.retry(exc=ValueError("try again"), countdown=0)
        return "done"

    task_id = "owned-task"
    task_span = tracer.start_span("task", attributes={"celery.task_id": task_id})
    with (
        trace.use_span(task_span, end_on_exit=True),
        contexts.new_context(fresh=True, capture_exceptions=False),
    ):
        contexts.identify_context("outer-person")
        contexts.set_context_session("outer-session")
        result = task.apply(task_id=task_id, headers=headers, throw=False)
        assert result.state == ("FAILURE" if state == "failure" else "SUCCESS")
        assert contexts.get_context_distinct_id() == "outer-person"
        assert contexts.get_context_session_id() == "outer-session"

    assert attempts == ([0, 1] if state == "retry" else [0])
    tracer.start_span("after-task").end()
    for span in exporter.get_finished_spans():
        span_expected = {} if span.name == "after-task" else expected
        if span.name == "task":
            span_expected = {**span_expected, "celery.task_id": task_id}
        assert dict(span.attributes) == span_expected


def test_published_context_reaches_task_span_without_worker_context_bleed(
    celery_tracing,
):
    app, tracer, exporter, _ = celery_tracing

    @app.task(name="span-context.published")
    def task():
        tracer.start_span("child").end()
        return contexts.get_context_distinct_id(), contexts.get_context_session_id()

    with contexts.new_context(fresh=True, capture_exceptions=False):
        contexts.identify_context("producer-person")
        contexts.set_context_session("producer-session")
        task.apply_async()

    with app.connection_for_read() as connection:
        with connection.SimpleQueue("celery") as queue:
            message = queue.get(block=False)
            worker_headers = message.headers["headers"]
            message.ack()

    with tracer.start_as_current_span(
        "task", attributes={"celery.task_id": "published"}
    ):
        result = task.apply(task_id="published", headers=worker_headers)
    assert result.get() == ("producer-person", "producer-session")

    with tracer.start_as_current_span(
        "unlinked-task", attributes={"celery.task_id": "unlinked"}
    ):
        assert task.apply(task_id="unlinked").get() == (None, None)
    expected = {
        "posthogDistinctId": "producer-person",
        "sessionId": "producer-session",
    }
    spans = exporter.get_finished_spans()
    assert [dict(span.attributes) for span in spans] == [
        expected,
        {**expected, "celery.task_id": "published"},
        {},
        {"celery.task_id": "unlinked"},
    ]


@pytest.mark.parametrize("state", ["success", "failure", "retry"])
def test_official_eager_task_spans_do_not_stamp_the_caller(celery_tracing, state):
    app, tracer, exporter, provider = celery_tracing
    instrumentor = CeleryInstrumentor()
    instrumentor.instrument(tracer_provider=provider)

    @app.task(bind=True, name="span-context.official")
    def task(self):
        tracer.start_span(f"child-{self.request.id}").end()
        if state == "failure":
            raise ValueError("task failed")
        if state == "retry" and self.request.retries == 0:
            raise self.retry(exc=ValueError("try again"), countdown=0)
        return "done"

    try:
        with tracer.start_as_current_span("caller") as caller:
            for name in ("first", "second"):
                result = task.apply(
                    task_id=name,
                    headers={
                        CONTEXT_DISTINCT_ID_HEADER: f"{name}-person",
                        CONTEXT_SESSION_ID_HEADER: f"{name}-session",
                    },
                    throw=False,
                )
                assert result.state == ("FAILURE" if state == "failure" else "SUCCESS")
                assert dict(caller.attributes) == {}
                assert contexts.get_context_distinct_id() is None
                assert contexts.get_context_session_id() is None
            tracer.start_span("after-tasks").end()
    finally:
        instrumentor.uninstrument()

    task_spans = [
        span
        for span in exporter.get_finished_spans()
        if span.kind == trace.SpanKind.CONSUMER
    ]
    assert len(task_spans) == (4 if state == "retry" else 2)
    for span in exporter.get_finished_spans():
        if span.name in ("caller", "after-tasks"):
            assert dict(span.attributes) == {}
        else:
            name = (
                span.name.removeprefix("child-")
                if span.name.startswith("child-")
                else span.attributes["messaging.message.id"]
            )
            assert span.attributes["posthogDistinctId"] == f"{name}-person"
            assert span.attributes["sessionId"] == f"{name}-session"


@pytest.mark.parametrize(
    "attributes", [{}, {"celery.task_id": "other-task"}, {"celery.task_id": 42}]
)
def test_eager_task_does_not_enrich_an_unverified_span(celery_tracing, attributes):
    app, tracer, exporter, _ = celery_tracing

    @app.task(name="span-context.unverified")
    def task():
        tracer.start_span("child").end()

    with tracer.start_as_current_span("task", attributes=attributes):
        task.apply(
            task_id="actual-task", headers={CONTEXT_SESSION_ID_HEADER: "job-session"}
        )
    child, parent = exporter.get_finished_spans()
    assert dict(child.attributes) == {"sessionId": "job-session"}
    assert dict(parent.attributes) == attributes


@pytest.mark.parametrize("api_state", ["missing", "failing", "unreadable"])
def test_task_context_survives_unavailable_current_span_api(
    celery_tracing, monkeypatch, api_state
):
    app, _, _, _ = celery_tracing
    if api_state == "missing":
        monkeypatch.delattr(trace, "get_current_span")
    elif api_state == "failing":
        monkeypatch.setattr(
            trace, "get_current_span", Mock(side_effect=RuntimeError("lookup failed"))
        )
    else:
        monkeypatch.setattr(trace, "get_current_span", lambda: object())

    @app.task(name="span-context.unavailable")
    def task():
        return contexts.get_context_session_id()

    assert (
        task.apply(headers={CONTEXT_SESSION_ID_HEADER: "job-session"}).get()
        == "job-session"
    )
    assert contexts.get_context_session_id() is None
