from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier
from unittest.mock import patch

import pytest
from flask import Flask
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from posthog import contexts
from posthog.integrations.flask import PosthogFlaskIntegration
from posthog.integrations.otel import PostHogContextSpanProcessor
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


@pytest.fixture
def native_client():
    client = make_client(traces={})
    with patch("posthog.tracing._transport._get_session", return_value=mock_session()):
        yield client
        client.shutdown()


@pytest.mark.parametrize("fail", [False, True])
def test_flask_enriches_existing_request_and_child_spans_and_cleans_up(
    tracing, native_client, fail
):
    tracer, exporter = tracing
    app = Flask(__name__)
    app.config["TESTING"] = True
    PosthogFlaskIntegration(app, trust_tracing_headers=True, capture_exceptions=False)

    @app.get("/")
    def view():
        native_client.start_span("native").end()
        tracer.start_span("child").end()
        if fail:
            raise ValueError("view failed")
        return "response"

    with contexts.new_context(fresh=True, capture_exceptions=False):
        with pytest.raises(ValueError, match="view failed") if fail else nullcontext():
            with tracer.start_as_current_span("request"):
                response = app.test_client().get(
                    "/",
                    headers={
                        "X-PostHog-Distinct-ID": "browser-person",
                        "X-PostHog-Session-ID": "browser-session",
                    },
                )
                assert response.status_code == 200
        tracer.start_span("after-request").end()
        assert contexts.get_context_distinct_id() is None
        assert contexts.get_context_session_id() is None

    spans = {span.name: dict(span.attributes) for span in exporter.get_finished_spans()}
    expected = {
        "posthogDistinctId": "browser-person",
        "sessionId": "browser-session",
    }
    assert spans == {"request": expected, "child": expected, "after-request": {}}
    payload, _, _ = flush_and_capture(native_client)
    (native,) = spans_from(payload)
    assert {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in native["attributes"]
    } == expected


@pytest.mark.parametrize(
    "trust,headers,attributes,expected",
    [
        (
            False,
            {"X-PostHog-Distinct-ID": "person", "X-PostHog-Session-ID": "session"},
            {},
            {},
        ),
        (True, {}, {}, {}),
        (True, {"X-PostHog-Distinct-ID": "   ", "X-PostHog-Session-ID": "   "}, {}, {}),
        (True, {"X-PostHog-Session-ID": "session"}, {}, {"sessionId": "session"}),
        (
            True,
            {"X-PostHog-Distinct-ID": "person"},
            {},
            {"posthogDistinctId": "person"},
        ),
        (
            True,
            {"X-PostHog-Distinct-ID": "person", "X-PostHog-Session-ID": "session"},
            {"posthogDistinctId": "own-person", "sessionId": "own-session"},
            {"posthogDistinctId": "own-person", "sessionId": "own-session"},
        ),
        (
            True,
            {"X-PostHog-Distinct-ID": "person", "X-PostHog-Session-ID": "session"},
            {"user.id": "authenticated"},
            {"user.id": "authenticated", "sessionId": "session"},
        ),
    ],
)
def test_flask_request_span_respects_header_policy_and_explicit_identity(
    tracing, trust, headers, attributes, expected
):
    tracer, exporter = tracing
    app = Flask(__name__)
    PosthogFlaskIntegration(app, trust_tracing_headers=trust, capture_exceptions=False)
    app.add_url_rule("/", view_func=lambda: "response")

    with contexts.new_context(fresh=True, capture_exceptions=False):
        with tracer.start_as_current_span("request", attributes=attributes):
            assert app.test_client().get("/", headers=headers).status_code == 200

    assert dict(exporter.get_finished_spans()[0].attributes) == expected


def test_flask_concurrent_requests_keep_separate_span_context(tracing):
    tracer, exporter = tracing
    app = Flask(__name__)
    both_started = Barrier(2)
    PosthogFlaskIntegration(app, trust_tracing_headers=True, capture_exceptions=False)

    @app.get("/<identity>")
    def view(identity):
        both_started.wait(timeout=5)
        tracer.start_span(identity + "-child").end()
        return "response"

    def run_request(identity):
        with contexts.new_context(fresh=True, capture_exceptions=False):
            with tracer.start_as_current_span(identity):
                assert (
                    app.test_client()
                    .get(
                        "/" + identity,
                        headers={
                            "X-PostHog-Distinct-ID": identity,
                            "X-PostHog-Session-ID": identity + "-session",
                        },
                    )
                    .status_code
                    == 200
                )
            tracer.start_span(identity + "-after").end()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run_request, ("first", "second")))

    spans = {span.name: dict(span.attributes) for span in exporter.get_finished_spans()}
    for identity in ("first", "second"):
        expected = {
            "posthogDistinctId": identity,
            "sessionId": identity + "-session",
        }
        assert spans[identity] == expected
        assert spans[identity + "-child"] == expected
        assert spans[identity + "-after"] == {}
