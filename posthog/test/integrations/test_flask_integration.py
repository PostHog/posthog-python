from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from flask import Flask, abort, jsonify

import posthog
from posthog import contexts
from posthog.client import Client
from posthog.integrations.flask import (
    PosthogFlaskIntegration,
    _sanitize_tracing_header_value,
)


def _app() -> Flask:
    app = Flask(__name__)
    app.config.update(TESTING=True)
    return app


def test_adds_request_context_and_restores_parent_context() -> None:
    app = _app()
    PosthogFlaskIntegration(
        app,
        extra_properties=lambda request: {"tenant": "acme"},
        trust_tracing_headers=True,
    )

    @app.get("/users/<user_id>")
    def view(user_id: str):
        scope = contexts._get_current_context()
        assert scope is not None
        return jsonify(
            distinct_id=contexts.get_context_distinct_id(),
            session_id=contexts.get_context_session_id(),
            properties=scope.collect_tags(),
        )

    with contexts.new_context():
        contexts.tag("outer", "must-not-leak-into-request")
        response = app.test_client().get(
            "/users/123?access_token=secret",
            headers={
                "X-PostHog-Distinct-Id": "  person-1  ",
                "X-PostHog-Session-Id": " session-1 ",
                "User-Agent": "integration-test/1.0",
            },
            environ_base={"REMOTE_ADDR": "203.0.113.10"},
        )

        payload = response.get_json()
        assert payload["distinct_id"] == "person-1"
        assert payload["session_id"] == "session-1"
        assert payload["properties"] == {
            "$current_url": "http://localhost/users/123",
            "$ip": "203.0.113.10",
            "$raw_user_agent": "integration-test/1.0",
            "$request_method": "GET",
            "$request_path": "/users/123",
            "$request_route": "/users/<user_id>",
            "$user_agent": "integration-test/1.0",
            "tenant": "acme",
        }
        assert "secret" not in payload["properties"]["$current_url"]

        parent = contexts._get_current_context()
        assert parent is not None
        assert parent.collect_tags() == {"outer": "must-not-leak-into-request"}


def test_fresh_request_preserves_enclosing_exception_privacy_settings() -> None:
    app = _app()
    client = Mock()
    observed = {}

    def capture(exception, **kwargs):
        scope = contexts._get_current_context()
        assert scope is not None
        observed.update(
            capture_code_variables=contexts.get_capture_exception_code_variables_context(),
            mask_patterns=contexts.get_code_variables_mask_patterns_context(),
            ignore_patterns=contexts.get_code_variables_ignore_patterns_context(),
            mask_url_credentials=contexts.get_code_variables_mask_url_credentials_context(),
            detect_secrets=contexts.get_code_variables_detect_secrets_context(),
            distinct_id=contexts.get_context_distinct_id(),
            session_id=contexts.get_context_session_id(),
            properties=scope.collect_tags(),
        )
        return "event-id"

    client.capture_exception.side_effect = capture
    PosthogFlaskIntegration(app, client=client, capture_exceptions=True)

    @app.get("/privacy")
    def privacy_failure():
        raise ValueError("privacy settings")

    with contexts.new_context():
        contexts.set_capture_exception_code_variables_context(False)
        contexts.set_code_variables_mask_patterns_context(["secret"])
        contexts.set_code_variables_ignore_patterns_context(["ignored"])
        contexts.set_code_variables_mask_url_credentials_context(True)
        contexts.set_code_variables_detect_secrets_context(True)
        contexts.identify_context("outer-person")
        contexts.set_context_session("outer-session")
        contexts.tag("outer-property", "must-not-leak")

        with pytest.raises(ValueError, match="privacy settings"):
            app.test_client().get("/privacy")

        # Teardown restores the enclosing context unchanged.
        assert contexts.get_capture_exception_code_variables_context() is False
        assert contexts.get_code_variables_mask_patterns_context() == ["secret"]
        assert contexts.get_code_variables_ignore_patterns_context() == ["ignored"]
        assert contexts.get_code_variables_mask_url_credentials_context() is True
        assert contexts.get_code_variables_detect_secrets_context() is True

    properties = observed.pop("properties")
    assert properties["$request_path"] == "/privacy"
    assert "outer-property" not in properties
    assert observed == {
        "capture_code_variables": False,
        "mask_patterns": ["secret"],
        "ignore_patterns": ["ignored"],
        "mask_url_credentials": True,
        "detect_secrets": True,
        # Identity remains isolated by the fresh scope.
        "distinct_id": None,
        "session_id": None,
    }


def test_captures_unhandled_exception_once_with_request_properties() -> None:
    app = _app()
    client = Mock()
    captures = []

    def capture(exception, **kwargs):
        scope = contexts._get_current_context()
        assert scope is not None
        captures.append((exception, kwargs, scope.collect_tags()))
        return "event-id"

    client.capture_exception.side_effect = capture
    PosthogFlaskIntegration(app, client=client, capture_exceptions=True)
    error = ValueError("view failed")

    @app.get("/failure")
    def failure():
        raise error

    with pytest.raises(ValueError, match="view failed"):
        app.test_client().get("/failure")

    assert len(captures) == 1
    exception, kwargs, properties = captures[0]
    assert exception is error
    assert kwargs == {
        "_capture_metadata": {
            "level": "error",
            "source": "flask.got_request_exception",
            "mechanism": {"type": "middleware", "handled": False},
        }
    }
    assert properties["$request_path"] == "/failure"
    assert contexts._get_current_context() is None


def test_uses_global_client_when_custom_client_is_not_provided() -> None:
    app = _app()
    PosthogFlaskIntegration(app, capture_exceptions=True)
    error = RuntimeError("boom")

    @app.get("/failure")
    def failure():
        raise error

    with patch("posthog.capture_exception", return_value="event-id") as capture:
        with pytest.raises(RuntimeError, match="boom"):
            app.test_client().get("/failure")

    capture.assert_called_once_with(
        error,
        _capture_metadata={
            "level": "error",
            "source": "flask.got_request_exception",
            "mechanism": {"type": "middleware", "handled": False},
        },
    )


def test_does_not_capture_handled_exception_or_expected_http_error() -> None:
    app = _app()
    client = Mock()
    PosthogFlaskIntegration(app, client=client, capture_exceptions=True)

    @app.errorhandler(ValueError)
    def handle_value_error(error):
        return {"error": str(error)}, 422

    @app.get("/handled")
    def handled():
        raise ValueError("expected")

    @app.get("/missing")
    def missing():
        abort(404)

    assert app.test_client().get("/handled").status_code == 422
    assert app.test_client().get("/missing").status_code == 404
    client.capture_exception.assert_not_called()


def test_capture_exceptions_can_be_disabled_without_changing_propagation() -> None:
    app = _app()
    client = Mock()
    PosthogFlaskIntegration(app, client=client, capture_exceptions=False)

    @app.get("/failure")
    def failure():
        raise LookupError("disabled")

    with pytest.raises(LookupError, match="disabled"):
        app.test_client().get("/failure")

    client.capture_exception.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_exception_capture_defaults_to_custom_client_setting(enabled: bool) -> None:
    app = _app()
    client = Mock(enable_exception_autocapture=enabled)
    PosthogFlaskIntegration(app, client=client)

    @app.get("/failure")
    def failure():
        raise RuntimeError("custom default")

    with pytest.raises(RuntimeError, match="custom default"):
        app.test_client().get("/failure")

    if enabled:
        client.capture_exception.assert_called_once()
    else:
        client.capture_exception.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("initialized", [False, True])
def test_exception_capture_defaults_to_global_client_setting(
    enabled: bool, initialized: bool
) -> None:
    app = _app()
    default_client = Mock(enable_exception_autocapture=enabled) if initialized else None

    # App-factory setup may run before the global client is configured.
    PosthogFlaskIntegration(app)

    @app.get("/failure")
    def failure():
        raise RuntimeError("global default")

    with (
        patch.object(posthog, "default_client", default_client),
        patch.object(
            posthog,
            "enable_exception_autocapture",
            not enabled if initialized else enabled,
        ),
        patch("posthog.capture_exception") as capture_exception,
        pytest.raises(RuntimeError, match="global default"),
    ):
        app.test_client().get("/failure")

    if enabled:
        capture_exception.assert_called_once()
    else:
        capture_exception.assert_not_called()


def test_context_enriches_events_without_redirecting_capture_calls() -> None:
    app = _app()
    events = []

    def record_event(event):
        events.append(event)
        return event

    event_client = Client(
        "event-api-key", send=False, sync_mode=True, before_send=record_event
    )
    exception_client = Mock(enable_exception_autocapture=False)
    PosthogFlaskIntegration(
        app,
        client=exception_client,
        extra_properties=lambda request: {"tenant": "acme"},
    )

    @app.get("/capture")
    def capture_event():
        event_client.capture(
            "request event", distinct_id="event-user", properties={"source": "app"}
        )
        return "ok"

    try:
        assert app.test_client().get("/capture").status_code == 200
    finally:
        event_client.shutdown()

    assert len(events) == 1
    assert events[0]["distinct_id"] == "event-user"
    assert events[0]["properties"]["source"] == "app"
    assert events[0]["properties"]["tenant"] == "acme"
    assert events[0]["properties"]["$request_path"] == "/capture"
    assert {"tenant", "$request_path"} <= set(events[0]["properties"]["$context_tags"])
    exception_client.capture.assert_not_called()


def test_tracing_headers_are_ignored_by_default_and_can_be_trusted() -> None:
    def request_identity(trust_tracing_headers: bool) -> dict:
        app = _app()
        PosthogFlaskIntegration(app, trust_tracing_headers=trust_tracing_headers)

        @app.get("/identity")
        def identity():
            return jsonify(
                distinct_id=contexts.get_context_distinct_id(),
                session_id=contexts.get_context_session_id(),
            )

        response = app.test_client().get(
            "/identity",
            headers={
                "X-PostHog-Distinct-Id": "person-1",
                "X-PostHog-Session-Id": "session-1",
            },
        )
        return response.get_json()

    assert request_identity(False) == {"distinct_id": None, "session_id": None}
    assert request_identity(True) == {
        "distinct_id": "person-1",
        "session_id": "session-1",
    }


def test_request_filter_skips_context_and_exception_capture() -> None:
    app = _app()
    client = Mock()
    PosthogFlaskIntegration(
        app,
        client=client,
        capture_exceptions=True,
        request_filter=lambda request: request.path != "/ignored",
    )

    @app.get("/ignored")
    def ignored():
        assert contexts._get_current_context() is None
        raise RuntimeError("ignored")

    with pytest.raises(RuntimeError, match="ignored"):
        app.test_client().get("/ignored")

    client.capture_exception.assert_not_called()


def test_supports_application_factory_pattern_and_rejects_duplicate_setup() -> None:
    app = _app()
    integration = PosthogFlaskIntegration()
    integration.init_app(app)

    assert app.extensions["posthog"] is integration

    with pytest.raises(RuntimeError, match="already initialized"):
        PosthogFlaskIntegration(app)


def test_sanitizes_and_bounds_tracing_headers() -> None:
    assert _sanitize_tracing_header_value("  person\n-\t1\x85  ") == "person-1"
    assert _sanitize_tracing_header_value("\r\n") is None
    assert _sanitize_tracing_header_value(123) is None
    assert _sanitize_tracing_header_value("a" * 1001) == "a" * 1000
