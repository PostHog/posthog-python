"""The span handle type returned by ``start_span``."""

from datetime import datetime
from typing import Any, Mapping, Optional, Union

__all__ = ["Span"]


class Span:
    """A handle to a span.

    Every method is safe to call on any handle, including after ``end()`` and
    on the inert handles returned when tracing is off, so calling code never
    branches on whether tracing is running. Entering a handle (``with span:``)
    makes it the active span for the block and ends it on exit, recording an
    ``Exception`` raised inside it. A ``BaseException`` that is not an
    ``Exception`` (``GeneratorExit``, ``CancelledError``, ``KeyboardInterrupt``)
    still ends the span but is not recorded as a failure.
    """

    def set_attribute(self, key: str, value: Any) -> "Span":
        """Set one attribute on the span; last write wins. Ignored after ``end()``.

        Returns the span, so calls chain. Prefer primitive values: strings,
        booleans, integers and floats keep their type. Lists and mappings are
        sent as OTLP arrays and maps, but PostHog stores them as serialized
        strings. ``None`` removes the key; anything else is stringified.

        Examples:
            ```python
            span.set_attribute("http.status_code", 200).set_attribute("cache.hit", True)
            ```
        """
        return self

    def set_attributes(self, attributes: Mapping[str, Any]) -> "Span":
        """Set several attributes at once; last write wins per key. Ignored after ``end()``.

        Returns the span, so calls chain. Same encoding as ``set_attribute()``.

        Examples:
            ```python
            span.set_attributes({"db.system": "postgresql", "db.rows": 12})
            ```
        """
        return self

    def add_event(
        self,
        name: str,
        attributes: Optional[Mapping[str, Any]] = None,
        timestamp: Union[datetime, float, None] = None,
    ) -> "Span":
        """Add a timestamped event to the span. Ignored after ``end()``.

        ``timestamp`` defaults to now; it accepts a ``datetime``, or seconds
        since the epoch as a ``float`` or ``int``. Returns the span, so calls
        chain.

        Examples:
            ```python
            span.add_event("cache.miss", {"key": "user:42"})
            ```
        """
        return self

    def set_status(self, code: str, message: Optional[str] = None) -> "Span":
        """Set the span status to ``"ok"`` or ``"error"``. Ignored after ``end()``.

        Unset by default. Any other ``code`` is ignored. ``ok`` is final for the
        scoped form: an ``Exception`` raised inside ``with span:`` does not override
        it. Returns the span, so calls chain.

        Examples:
            ```python
            span.set_status("error", "upstream timed out")
            ```
        """
        return self

    def record_exception(self, exception: BaseException) -> "Span":
        """Record an exception as an ``exception`` event and mark the span ``error``.

        Ignored after ``end()``. The event carries ``exception.type`` and
        ``exception.message``. Returns the span, so calls chain. Inside
        ``with span:`` a raised ``Exception`` is recorded automatically, so
        this is for exceptions that are caught and handled.

        Examples:
            ```python
            try:
                charge(card)
            except PaymentError as e:
                span.record_exception(e)
            ```
        """
        return self

    def update_name(self, name: str) -> "Span":
        """Rename the span, for a name only known after it started. Ignored after ``end()``.

        Returns the span, so calls chain.

        Examples:
            ```python
            span = posthog.start_span("http.request")
            span.update_name(f"{request.method} {route.pattern}")
            ```
        """
        return self

    def traceparent(self) -> Optional[str]:
        """The W3C ``traceparent`` header value to propagate, or ``None``."""
        return None

    def tracestate(self) -> Optional[str]:
        """The W3C ``tracestate`` header value to propagate, or ``None``."""
        return None

    def end(self, end_time: Union[datetime, float, None] = None) -> None:
        """End the span and queue it for export. Idempotent.

        ``end_time`` defaults to now; it accepts a ``datetime``, or seconds
        since the epoch as a ``float`` or ``int``, and is never earlier than
        the start. Later calls, and later mutations, no-op. ``with span:``
        ends the span on exit, so call this only for spans started manually.

        Examples:
            ```python
            span = posthog.start_span("job")
            try:
                run_job()
            finally:
                span.end()
            ```
        """
        return None

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None
