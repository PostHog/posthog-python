"""Automatic duration metrics for the HTTP requests an application makes.

Enabled with the ``metrics={"network": ...}`` client option. Wraps
``requests.Session.send`` and, when httpx is installed, ``httpx.Client.send``
and ``httpx.AsyncClient.send``. The wrappers only observe: they call the
original with the same arguments, return its result or re-raise its error,
and never let a recording failure reach the caller. The SDK marks its own
sessions with ``_mark_internal`` so PostHog's uploads are not recorded.
"""

import contextvars
import functools
import logging
import re
import time
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

log = logging.getLogger("posthog")

DEFAULT_METRIC_NAME = "http.client.request.duration"

_INTERNAL_MARKER = "_posthog_internal"

# ``requests`` sends each redirect hop through ``Session.send`` again. Only the
# outermost send in a thread or task is recorded, so a redirected request counts once.
_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "posthog_network_metrics_in_flight", default=False
)

_ALL_DIGITS = re.compile(r"^\d+$")
_HEX_WITH_A_DIGIT = re.compile(r"^[0-9a-f-]*\d[0-9a-f-]*$", re.IGNORECASE)


def _mark_internal(http_client):
    """Marks a ``requests.Session`` or httpx client as the SDK's own.

    Its requests are never recorded as network metrics.
    """
    setattr(http_client, _INTERNAL_MARKER, True)
    return http_client


def _is_internal(http_client) -> bool:
    return getattr(http_client, _INTERNAL_MARKER, False) is True


def _is_id_like(segment: str) -> bool:
    return bool(_ALL_DIGITS.match(segment)) or (
        len(segment) >= 8 and bool(_HEX_WITH_A_DIGIT.match(segment))
    )


def _template_path(path: str) -> str:
    """Replaces each all-digit or uuid-like path segment with ``:id``."""
    return "/".join(
        ":id" if _is_id_like(segment) else segment for segment in path.split("/")
    )


def _status_class(status: Optional[int]) -> str:
    return "{}xx".format(status // 100) if status else "missing"


def _parse_config(config: Any) -> Tuple[Any, Optional[Callable]]:
    if config is True:
        config = {}
    if not isinstance(config, dict):
        log.warning(
            "Ignoring metrics network config: expected True or a dict, got %s",
            type(config).__name__,
        )
        config = {}
    name = config.get("name", DEFAULT_METRIC_NAME)
    if not (isinstance(name, str) or callable(name)):
        log.warning("Ignoring metrics network name: expected a string or a callable")
        name = DEFAULT_METRIC_NAME
    attributes = config.get("attributes")
    if attributes is not None and not callable(attributes):
        log.warning("Ignoring metrics network attributes: expected a callable")
        attributes = None
    return name, attributes


def _patch(target, attribute: str, make_wrapper: Callable) -> Callable[[], None]:
    original = getattr(target, attribute)
    wrapper = make_wrapper(original)
    setattr(target, attribute, wrapper)

    def restore() -> None:
        # Another wrapper layered on top keeps ours in place as a pass-through.
        if getattr(target, attribute) is wrapper:
            setattr(target, attribute, original)

    return restore


class _NetworkMetrics:
    """Installs the request wrappers for one metrics client; ``stop()`` removes them."""

    def __init__(self, metrics, config: Any):
        self._metrics = metrics
        self._name, self._attributes = _parse_config(config)
        self._active = True
        self._record_error_warned = False
        self._restores: List[Callable[[], None]] = [
            _patch(requests.Session, "send", self._wrap_sync)
        ]
        if httpx is not None:
            self._restores.append(_patch(httpx.Client, "send", self._wrap_sync))
            self._restores.append(_patch(httpx.AsyncClient, "send", self._wrap_async))

    def stop(self) -> None:
        self._active = False
        for restore in self._restores:
            restore()

    def _observes(self, http_client) -> bool:
        return self._active and not _in_flight.get() and not _is_internal(http_client)

    def _wrap_sync(self, original: Callable) -> Callable:
        @functools.wraps(original)
        def send(http_client, request, *args, **kwargs):
            if not self._observes(http_client):
                return original(http_client, request, *args, **kwargs)
            token = _in_flight.set(True)
            start = time.perf_counter()
            try:
                response = original(http_client, request, *args, **kwargs)
            except Exception:
                self._record(request, None, start)
                raise
            finally:
                _in_flight.reset(token)
            self._record(request, response, start)
            return response

        return send

    def _wrap_async(self, original: Callable) -> Callable:
        @functools.wraps(original)
        async def send(http_client, request, *args, **kwargs):
            if not self._observes(http_client):
                return await original(http_client, request, *args, **kwargs)
            token = _in_flight.set(True)
            start = time.perf_counter()
            try:
                response = await original(http_client, request, *args, **kwargs)
            except Exception:
                self._record(request, None, start)
                raise
            finally:
                _in_flight.reset(token)
            self._record(request, response, start)
            return response

        return send

    def _record(self, request, response, start: float) -> None:
        try:
            duration_ms = (time.perf_counter() - start) * 1000
            url = str(request.url)
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https"):
                return
            observed = {"url": url, "method": str(request.method).upper()}
            name = self._name(observed) if callable(self._name) else self._name
            if not name:
                return
            status = getattr(response, "status_code", None)
            attributes = {
                "method": observed["method"],
                "host": parts.hostname or "",
                "path": _template_path(parts.path),
                "status_class": _status_class(status),
            }
            if self._attributes is not None:
                extra = self._attributes(
                    observed, {"status": status, "duration_ms": duration_ms}
                )
                attributes.update(extra or {})
            self._metrics.histogram(name, duration_ms, unit="ms", attributes=attributes)
        except Exception as e:
            if not self._record_error_warned:
                self._record_error_warned = True
                log.warning("Failed to record network metric: %s", e)
