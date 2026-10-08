"""Django REST Framework exception handling integration.

Django REST Framework (DRF) converts many exceptions into ``Response`` objects
before Django's middleware can observe them. Configure this module's
``exception_handler`` alongside :class:`PosthogContextMiddleware` to capture
handled server errors while leaving DRF's response behavior unchanged::

    REST_FRAMEWORK = {
        "EXCEPTION_HANDLER": "posthog.integrations.drf.exception_handler",
    }

By default, only responses with a 5xx status are captured. Expected 4xx API
errors are ignored. Projects that already have a custom DRF exception handler
can wrap it in an application module::

    from myapp.api import existing_exception_handler
    from posthog.integrations.drf import create_exception_handler

    exception_handler = create_exception_handler(existing_exception_handler)

Then point ``REST_FRAMEWORK["EXCEPTION_HANDLER"]`` at that application-level
``exception_handler``. DRF is imported lazily, so importing the PostHog SDK does
not require DRF to be installed.
"""

import logging
from typing import Any, Callable, Mapping, Optional, cast

from ..client import Client
from ..exception_utils import (
    _capture_exception_with_metadata,
    exception_is_already_captured as _exception_is_already_captured,
)

_logger = logging.getLogger("posthog")

_CAPTURE_METADATA = {
    "level": "error",
    "source": "django_rest_framework.exception_handler",
    "mechanism": {"type": "django_rest_framework", "handled": True},
}


def _default_exception_handler(exc: Exception, context: Mapping[str, Any]) -> Any:
    from rest_framework.views import exception_handler as drf_exception_handler

    return drf_exception_handler(exc, context)


def _configured_client(client: Optional[Client]) -> Optional[Client]:
    if client is not None:
        return client

    try:
        from django.conf import settings

        for setting_name in ("POSTHOG_DRF_CLIENT", "POSTHOG_MW_CLIENT"):
            configured_client = getattr(settings, setting_name, None)
            if isinstance(configured_client, Client):
                return configured_client
    except Exception:
        # Django may not be configured when a handler is created at import time.
        pass

    return None


def _capture_exception(client: Optional[Client], exc: Exception) -> None:
    if _exception_is_already_captured(exc):
        return

    resolved_client = _configured_client(client)
    if resolved_client is not None:
        _capture_exception_with_metadata(resolved_client, exc, _CAPTURE_METADATA)
    else:
        from .. import capture_exception

        cast(Any, capture_exception)(exc, _capture_metadata=_CAPTURE_METADATA)


def create_exception_handler(
    handler: Optional[Callable[[Exception, Mapping[str, Any]], Any]] = None,
    *,
    client: Optional[Client] = None,
    capture_4xx: bool = False,
    exception_filter: Optional[
        Callable[[Exception, Any, Mapping[str, Any]], bool]
    ] = None,
) -> Callable[[Exception, Mapping[str, Any]], Any]:
    """Create a PostHog-instrumented DRF exception handler.

    Args:
        handler: Handler to delegate to. Defaults to DRF's standard exception
            handler. Its return value and raised exceptions are preserved.
        client: Optional PostHog client. When omitted, ``POSTHOG_DRF_CLIENT`` or
            ``POSTHOG_MW_CLIENT`` is used when configured, then the global
            PostHog client.
        capture_4xx: Also capture handled 4xx responses. Disabled by default to
            avoid reporting expected API errors.
        exception_filter: Optional final filter called with ``(exception,
            response, context)``. Returning ``False`` suppresses capture.

    The delegated handler is called first. A ``None`` response is never
    captured here because DRF will re-raise that exception, allowing Django's
    middleware to capture it as unhandled.
    """
    delegate = handler or _default_exception_handler

    def posthog_exception_handler(exc: Exception, context: Mapping[str, Any]) -> Any:
        response = delegate(exc, context)
        if response is None:
            return None

        try:
            status_code = int(response.status_code)
            should_capture = status_code >= 500 or (
                capture_4xx and 400 <= status_code < 500
            )
            if should_capture and exception_filter is not None:
                should_capture = bool(exception_filter(exc, response, context))
            if should_capture:
                _capture_exception(client, exc)
        except Exception:
            # Error tracking must never alter DRF's exception response.
            _logger.exception("Failed to capture Django REST Framework exception")

        return response

    return posthog_exception_handler


def exception_handler(exc: Exception, context: Mapping[str, Any]) -> Any:
    """Capture handled DRF 5xx exceptions using DRF's default handler."""
    return _DEFAULT_EXCEPTION_HANDLER(exc, context)


_DEFAULT_EXCEPTION_HANDLER = create_exception_handler()
