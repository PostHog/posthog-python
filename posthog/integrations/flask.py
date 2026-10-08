"""Flask request context and exception tracking integration.

Flask is imported lazily when :meth:`PosthogFlaskIntegration.init_app` is called,
so importing the PostHog SDK does not require Flask to be installed.

Example::

    from flask import Flask
    from posthog.integrations.flask import PosthogFlaskIntegration

    app = Flask(__name__)
    PosthogFlaskIntegration(app)
"""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, cast

from .. import contexts
from ..client import Client
from ..exception_utils import (
    _ExceptionCaptureMetadata,
    _capture_exception_with_metadata,
)

if TYPE_CHECKING:
    from flask import Flask, Request


__all__ = ["PosthogFlaskIntegration"]

_MAX_TRACING_HEADER_LENGTH = 1000
_TRACING_HEADER_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_EXTENSION_KEY = "posthog"
_REQUEST_STATE_KEY = "_posthog_integration_state"


def _sanitize_tracing_header_value(value: object) -> Optional[str]:
    """Return a bounded tracing header value safe for event properties."""
    if not isinstance(value, str) or not value:
        return None

    return (
        _TRACING_HEADER_CONTROL_CHARS_RE.sub("", value).strip()[
            :_MAX_TRACING_HEADER_LENGTH
        ]
        or None
    )


@dataclass
class _RequestState:
    scope: AbstractContextManager[None]
    tracked: bool = True


class PosthogFlaskIntegration:
    """Add PostHog request context and exception tracking to a Flask app.

    Args:
        app: An optional Flask application. If omitted, call :meth:`init_app`
            later (the Flask application-factory pattern).
        client: Optional PostHog client used to capture exceptions. The global
            client is used by default.
        capture_exceptions: Capture exceptions that reach Flask's unhandled
            exception machinery. Defaults to ``True``.
        request_filter: Optional callback receiving Flask's request object. A
            false return value disables both context and exception capture for
            that request.
        extra_properties: Optional callback returning additional event properties.
            This is useful for application-specific metadata such as an authenticated
            user's role. Values should not contain secrets or request bodies.

    The integration intentionally does not capture exceptions handled by an
    application error handler, expected HTTP exceptions, request or response
    bodies, query strings, cookies, authorization headers, or arbitrary headers.
    Call ``capture_exception`` explicitly from a custom error handler if a
    handled exception should be reported.
    """

    def __init__(
        self,
        app: Optional[Flask] = None,
        *,
        client: Optional[Client] = None,
        capture_exceptions: bool = True,
        request_filter: Optional[Callable[[Request], bool]] = None,
        extra_properties: Optional[Callable[[Request], Mapping[str, Any]]] = None,
    ) -> None:
        self.client = client
        self.capture_exceptions = capture_exceptions
        self.request_filter = request_filter
        self.extra_properties = extra_properties

        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        """Register the integration with a Flask application once."""
        try:
            from flask import got_request_exception
        except ImportError as error:  # pragma: no cover - exercised without Flask
            raise RuntimeError(
                "PosthogFlaskIntegration requires Flask to be installed"
            ) from error

        if _EXTENSION_KEY in app.extensions:
            raise RuntimeError("PostHog is already initialized for this Flask app")

        app.extensions[_EXTENSION_KEY] = self
        app.before_request(self._before_request)
        app.teardown_request(self._teardown_request)
        got_request_exception.connect(self._handle_unhandled_exception, app, weak=False)

    def _before_request(self) -> None:
        from flask import g, request

        if self.request_filter is not None and not self.request_filter(request):
            setattr(g, _REQUEST_STATE_KEY, None)
            return

        # Flask handles application exceptions before returning control through
        # the request stack, so capture through got_request_exception rather than
        # through new_context. This also avoids duplicate capture.
        scope = contexts.new_context(
            fresh=True,
            capture_exceptions=False,
            client=self.client,
        )
        scope.__enter__()
        setattr(g, _REQUEST_STATE_KEY, _RequestState(scope=scope))

        session_id = _sanitize_tracing_header_value(
            request.headers.get("X-POSTHOG-SESSION-ID")
        )
        if session_id:
            contexts.set_context_session(session_id)

        distinct_id = _sanitize_tracing_header_value(
            request.headers.get("X-POSTHOG-DISTINCT-ID")
        )
        if distinct_id:
            contexts.identify_context(distinct_id)

        for key, value in self._request_properties(request).items():
            contexts.tag(key, value)

        if self.extra_properties is not None:
            extra_properties = self.extra_properties(request)
            if extra_properties:
                for key, value in extra_properties.items():
                    contexts.tag(key, value)

    @staticmethod
    def _request_properties(request: Request) -> dict[str, Any]:
        properties: dict[str, Any] = {
            # base_url deliberately excludes query strings, which commonly
            # contain credentials, tokens, and other sensitive values.
            "$current_url": request.base_url,
            "$request_method": request.method,
            "$request_path": request.path,
        }

        if request.remote_addr:
            properties["$ip"] = request.remote_addr

        user_agent = request.headers.get("User-Agent")
        if user_agent:
            properties["$user_agent"] = user_agent
            properties["$raw_user_agent"] = user_agent

        url_rule = getattr(request, "url_rule", None)
        if url_rule is not None:
            properties["$request_route"] = str(url_rule)

        return properties

    def _handle_unhandled_exception(
        self, sender: Flask, exception: BaseException, **kwargs: Any
    ) -> None:
        if not self.capture_exceptions or not self._request_is_tracked():
            return

        capture_metadata: _ExceptionCaptureMetadata = {
            "level": "error",
            "source": "flask.got_request_exception",
            "mechanism": {"type": "middleware", "handled": False},
        }
        if self.client is not None:
            _capture_exception_with_metadata(self.client, exception, capture_metadata)
        else:
            # Keep this import relative so the generated posthoganalytics mirror
            # resolves its own global client rather than the posthog package.
            from .. import capture_exception

            cast(Any, capture_exception)(exception, _capture_metadata=capture_metadata)

    @staticmethod
    def _request_is_tracked() -> bool:
        from flask import g, has_request_context

        if not has_request_context():
            return False
        state = getattr(g, _REQUEST_STATE_KEY, None)
        return isinstance(state, _RequestState) and state.tracked

    def _teardown_request(self, exception: Optional[BaseException]) -> None:
        from flask import g

        state = getattr(g, _REQUEST_STATE_KEY, None)
        if not isinstance(state, _RequestState):
            return

        # The Flask signal already captured any unhandled exception. Close the
        # context normally so new_context cannot capture it a second time.
        setattr(g, _REQUEST_STATE_KEY, None)
        state.scope.__exit__(None, None, None)
