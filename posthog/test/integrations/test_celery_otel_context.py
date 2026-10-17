from unittest.mock import Mock

import pytest
from celery import Celery
from opentelemetry import trace
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
        client=Mock(), capture_exceptions=False, capture_task_lifecycle_events=False
    )
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(PostHogContextSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    integration.instrument()
    try:
        yield app, provider.get_tracer(__name__), exporter
    finally:
        integration.shutdown()
        provider.shutdown()
        app.close()


@pytest.mark.parametrize("state", ["success", "failure", "retry"])
@pytest.mark.parametrize("propagated", [True, False])
def test_eager_task_enriches_existing_span_and_clears_context(
    celery_tracing, state, propagated
):
    app, tracer, exporter = celery_tracing
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

    task_span = tracer.start_span("task")
    with (
        trace.use_span(task_span, end_on_exit=True),
        contexts.new_context(fresh=True, capture_exceptions=False),
    ):
        contexts.identify_context("outer-person")
        contexts.set_context_session("outer-session")
        result = task.apply(headers=headers, throw=False)
        assert result.state == ("FAILURE" if state == "failure" else "SUCCESS")
        assert contexts.get_context_distinct_id() == "outer-person"
        assert contexts.get_context_session_id() == "outer-session"

    assert attempts == ([0, 1] if state == "retry" else [0])
    tracer.start_span("after-task").end()
    for span in exporter.get_finished_spans():
        assert dict(span.attributes) == ({} if span.name == "after-task" else expected)


def test_published_context_reaches_task_span_without_worker_context_bleed(
    celery_tracing,
):
    app, tracer, exporter = celery_tracing

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

    with tracer.start_as_current_span("task"):
        result = task.apply(headers=worker_headers)
    assert result.get() == ("producer-person", "producer-session")

    with tracer.start_as_current_span("unlinked-task"):
        assert task.apply().get() == (None, None)
    expected = {
        "posthogDistinctId": "producer-person",
        "sessionId": "producer-session",
    }
    spans = exporter.get_finished_spans()
    assert [dict(span.attributes) for span in spans] == [expected, expected, {}, {}]
