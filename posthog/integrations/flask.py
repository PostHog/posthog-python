"""Flask request context and exception tracking integration.

Flask is imported lazily when :meth:`PosthogFlaskIntegration.init_app` is called,
so importing the PostHog SDK does not require Flask to be installed.

Example::

    from flask import Flask
    from posthog.integrations.flask import PosthogFlaskIntegration

    app = Flask(__name__)
    PosthogFlaskIntegration(app)

The request context enriches events captured by any client while the request is
running. Passing ``client`` only selects where automatically captured exceptions
are sent; it does not redirect application calls to ``posthog.capture`` or
``another_client.capture``.
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


@dataclass(frozen=True)
class _ExceptionPrivacySettings:
    capture_code_variables: Optional[bool]
    mask_patterns: Optional[list]
    ignore_patterns: Optional[list]
    mask_url_credentials: Optional[bool]
    detect_secrets: Optional[bool]

    @classmethod
    def from_current_context(cls) -> _ExceptionPrivacySettings:
        """Snapshot effective privacy controls before entering a fresh request scope."""
        return cls(
            capture_code_variables=contexts.get_capture_exception_code_variables_context(),
            mask_patterns=contexts.get_code_variables_mask_patterns_context(),
            ignore_patterns=contexts.get_code_variables_ignore_patterns_context(),
            mask_url_credentials=contexts.get_code_variables_mask_url_credentials_context(),
            detect_secrets=contexts.get_code_variables_detect_secrets_context(),
        )

    def apply(self) -> None:
        """Apply inherited privacy controls without restoring identity or properties."""
        if self.capture_code_variables is not None:
            contexts.set_capture_exception_code_variables_context(
                self.capture_code_variables
            )
        if self.mask_patterns is not None:
            contexts.set_code_variables_mask_patterns_context(self.mask_patterns)
        if self.ignore_patterns is not None:
            contexts.set_code_variables_ignore_patterns_context(self.ignore_patterns)
        if self.mask_url_credentials is not None:
            contexts.set_code_variables_mask_url_credentials_context(
                self.mask_url_credentials
            )
        if self.detect_secrets is not None:
            contexts.set_code_variables_detect_secrets_context(self.detect_secrets)


class PosthogFlaskIntegration:
    """Add PostHog request context and exception tracking to a Flask app.

    Args:
        app: An optional Flask application. If omitted, call :meth:`init_app`
            later (the Flask application-factory pattern).
        client: Optional destination for automatically captured exceptions. The
            global client is used by default. Request context still enriches events
            captured through any client and does not redirect capture calls.
        capture_exceptions: Capture exceptions that reach Flask's unhandled
            exception machinery. If omitted, inherit the effective client's
            ``enable_exception_autocapture`` setting. Pass ``True`` or ``False``
            to override it.
        request_filter: Optional callback receiving Flask's request object. A
            false return value disables both context and exception capture for
            that request.
        extra_properties: Optional callback returning additional event properties.
            This is useful for application-specific metadata such as an authenticated
            user's role. Values should not contain secrets or request bodies.
        trust_tracing_headers: Use client-provided PostHog distinct and session ID
            headers as analytics context. Disabled by default because these headers
            are not authenticated. Enable only when deliberately accepting browser
            attribution or when a trusted upstream replaces incoming values. Never
            use these identifiers for authorization.

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
        capture_exceptions: Optional[bool] = None,
        request_filter: Optional[Callable[[Request], bool]] = None,
        extra_properties: Optional[Callable[[Request], Mapping[str, Any]]] = None,
        trust_tracing_headers: bool = False,
    ) -> None:
        self.client = client
        self.capture_exceptions = (
            contexts._default_capture_exceptions(client)
            if capture_exceptions is None
            else capture_exceptions
        )
        self.request_filter = request_filter
        self.extra_properties = extra_properties
        self.trust_tracing_headers = trust_tracing_headers

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

        # A request gets fresh identity and event properties, but privacy controls
        # must remain at least as strict as the effective enclosing context.
        privacy_settings = _ExceptionPrivacySettings.from_current_context()

        # Flask handles application exceptions before returning control through
        # the request stack, so capture through got_request_exception rather than
        # through new_context. This also avoids duplicate capture.
        scope = contexts.new_context(
            fresh=True,
            capture_exceptions=False,
            client=self.client,
        )
        scope.__enter__()
        privacy_settings.apply()
        setattr(g, _REQUEST_STATE_KEY, _RequestState(scope=scope))

        if self.trust_tracing_headers:
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
