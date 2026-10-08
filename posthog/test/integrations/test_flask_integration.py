from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from flask import Flask, abort, jsonify

from posthog import contexts
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
    PosthogFlaskIntegration(app, extra_tags=lambda request: {"tenant": "acme"})

    @app.get("/users/<user_id>")
    def view(user_id: str):
        scope = contexts._get_current_context()
        assert scope is not None
        return jsonify(
            distinct_id=contexts.get_context_distinct_id(),
            session_id=contexts.get_context_session_id(),
            tags=scope.collect_tags(),
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
        assert payload["tags"] == {
            "$current_url": "http://localhost/users/123",
            "$ip": "203.0.113.10",
            "$raw_user_agent": "integration-test/1.0",
            "$request_method": "GET",
            "$request_path": "/users/123",
            "$request_route": "/users/<user_id>",
            "$user_agent": "integration-test/1.0",
            "tenant": "acme",
        }
        assert "secret" not in payload["tags"]["$current_url"]

        parent = contexts._get_current_context()
        assert parent is not None
        assert parent.collect_tags() == {"outer": "must-not-leak-into-request"}


def test_captures_unhandled_exception_once_with_request_tags() -> None:
    app = _app()
    client = Mock()
    captures = []

    def capture(exception, **kwargs):
        scope = contexts._get_current_context()
        assert scope is not None
        captures.append((exception, kwargs, scope.collect_tags()))
        return "event-id"

    client.capture_exception.side_effect = capture
    PosthogFlaskIntegration(app, client=client)
    error = ValueError("view failed")

    @app.get("/failure")
    def failure():
        raise error

    with pytest.raises(ValueError, match="view failed"):
        app.test_client().get("/failure")

    assert len(captures) == 1
    exception, kwargs, tags = captures[0]
    assert exception is error
    assert kwargs == {
        "_capture_metadata": {
            "level": "error",
            "source": "flask.got_request_exception",
            "mechanism": {"type": "middleware", "handled": False},
        }
    }
    assert tags["$request_path"] == "/failure"
    assert contexts._get_current_context() is None


def test_uses_global_client_when_custom_client_is_not_provided() -> None:
    app = _app()
    PosthogFlaskIntegration(app)
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
    PosthogFlaskIntegration(app, client=client)

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


def test_request_filter_skips_context_and_exception_capture() -> None:
    app = _app()
    client = Mock()
    PosthogFlaskIntegration(
        app,
        client=client,
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
