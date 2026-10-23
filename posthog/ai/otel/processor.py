"""PostHog span processor for OpenTelemetry.

Provides a self-contained SpanProcessor that filters AI-related spans and
exports them to PostHog's OTLP endpoint. This is the recommended integration
for setups using TracerProvider.add_span_processor().
"""

import logging
import os
import threading
import weakref
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from ..gateway import warn_if_posthog_ai_gateway_otel_attributes
from ._request_context import (
    RequestContext,
    capture_request_context,
    with_request_context,
)
from ._privacy import _otel_privacy_mode_enabled, _redacted_spans
from .spans import DEFAULT_HOST, is_ai_span

if TYPE_CHECKING:
    from ...client import Client

log = logging.getLogger("posthog")


def _span_key(span: ReadableSpan) -> Tuple[int, int]:
    # The SDK passes on_end a ReadableSpan snapshot, not the Span object on_start saw.
    context = span.get_span_context()
    return (context.trace_id, context.span_id)


_processors: "weakref.WeakSet[PostHogSpanProcessor]" = weakref.WeakSet()


def _discard_abandoned(
    processor_ref: "weakref.ref[PostHogSpanProcessor]", key: Tuple[int, int]
) -> None:
    processor = processor_ref()
    if processor is not None:
        processor._discard_captured(key)


def _reset_processors_after_fork() -> None:
    for processor in list(_processors):
        processor._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_processors_after_fork)


class PostHogSpanProcessor(SpanProcessor):
    """Span processor that filters AI spans and exports them to PostHog.

    Wraps a BatchSpanProcessor and OTLPSpanExporter internally, configured
    to send to PostHog's OTLP traces endpoint. Only spans identified as
    AI-related (by name or attribute prefix) are forwarded for export.

    Usage::

        from opentelemetry.sdk.trace import TracerProvider
        from posthog.ai.otel import PostHogSpanProcessor

        provider = TracerProvider()
        provider.add_span_processor(
            PostHogSpanProcessor(api_key="phc_...")
        )

        # Per request: AI spans started inside a context export with its user, session and tags.
        with posthog.new_context():
            posthog.identify_context(user_id)
            posthog.tag("plan", "pro")
            handle_request()

    Attributes already on a span win over explicit tags, which win over the
    context's distinct ID and session ID (exported as ``$session_id``). Threads that don't copy contextvars
    carry no user.

    The context is captured when a span starts, but tag values are read when the
    span ends. Mutating a tagged object while a span is open shows up in the export.
    """

    def __init__(
        self,
        api_key: str,
        host: str = DEFAULT_HOST,
        *,
        privacy_mode: bool = False,
        client: Optional["Client"] = None,
    ):
        """
        Args:
            api_key: PostHog project API key.
            host: PostHog host URL. Defaults to US cloud.
            privacy_mode: Drop prompts, completions, tool arguments and results,
                and error messages from exported spans.
            client: PostHog client whose ``privacy_mode`` also applies. Without
                one, the module-level ``posthog.privacy_mode`` setting applies.
        """
        self._api_key = api_key
        self._host = host.rstrip("/")
        self._privacy_mode = privacy_mode
        self._client = client

        exporter = OTLPSpanExporter(
            endpoint=f"{self._host}/i/v0/ai/otel",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        self._processor = BatchSpanProcessor(exporter)
        self._captured: Dict[
            Tuple[int, int], Tuple[RequestContext, weakref.finalize]
        ] = {}
        # Reentrant: cyclic GC can run a span finalizer on a thread that already holds the lock.
        self._captured_lock = threading.RLock()
        _processors.add(self)

    def _reset_after_fork(self) -> None:
        # A lock held by another thread at fork time would never release in the child.
        self._captured_lock = threading.RLock()
        self._captured = {}

    def _discard_captured(self, key: Tuple[int, int]) -> None:
        with self._captured_lock:
            self._captured.pop(key, None)

    def _take_captured(self, key: Tuple[int, int]) -> Optional[RequestContext]:
        with self._captured_lock:
            entry = self._captured.pop(key, None)
        if entry is None:
            return None
        captured, finalizer = entry
        finalizer.detach()
        return captured

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        """
        Remember the active PostHog context for the span.

        The span itself is left untouched, so other processors on the provider
        never see the context. The values are added to the copy exported to
        PostHog in ``on_end``.
        """
        try:
            captured = capture_request_context()
            if captured:
                key = _span_key(span)
                finalizer = weakref.finalize(
                    span, _discard_abandoned, weakref.ref(self), key
                )
                with self._captured_lock:
                    self._captured[key] = (captured, finalizer)
        except Exception:
            log.debug("Failed to capture PostHog context for span", exc_info=True)

    def on_end(self, span: ReadableSpan) -> None:
        """
        Export an ended span if it is AI-related.

        Args:
            span: The ended OpenTelemetry span.
        """
        captured = self._take_captured(_span_key(span))
        if not is_ai_span(span):
            return
        warn_if_posthog_ai_gateway_otel_attributes(span.attributes)
        if captured:
            try:
                span = with_request_context(span, captured)
            except Exception:
                log.debug("Failed to add PostHog context to span", exc_info=True)
        privacy_mode = _otel_privacy_mode_enabled(self._privacy_mode, self._client)
        for redacted in _redacted_spans([span], privacy_mode):
            self._processor.on_end(redacted)

    def shutdown(self) -> None:
        """Shut down the underlying batch span processor."""
        self._processor.shutdown()
        with self._captured_lock:
            for _, finalizer in self._captured.values():
                finalizer.detach()
            self._captured.clear()

    def force_flush(self, timeout_millis: Optional[int] = None) -> bool:
        """
        Flush pending spans from the underlying batch span processor.

        Args:
            timeout_millis: Optional flush timeout in milliseconds.

        Returns:
            True if the flush succeeded within the timeout, False otherwise.
        """
        if timeout_millis is not None:
            return self._processor.force_flush(timeout_millis)
        return self._processor.force_flush()
