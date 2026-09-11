"""Span creation, parenting and the end-of-span gates.

Ended spans go to the exporter. Every public method is safe from any thread and
never raises into the host.
"""

import logging
import threading
import time
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol

from ._config import ResolvedTracesConfig
from ._drops import DropLog
from ._ids import new_span_id, new_trace_id
from ._otlp import SpanRecord
from ._sanitize import (
    copy_user_attributes,
    resolve_start_ns,
    sanitize_name,
    to_epoch_ns,
)
from ._span import (
    ParentContext,
    PassThroughSpan,
    RecordingSpan,
    Span,
    inert_span,
)
from ._traceparent import parse_traceparent, sanitize_tracestate, traceparent_header

log = logging.getLogger("posthog")

_CONTEXT_ATTRIBUTE_KEYS = (
    ("distinct_id", "posthogDistinctId"),
    ("session_id", "sessionId"),
)

GetContextFn = Callable[[], Mapping[str, Any]]


class Exporter(Protocol):
    def enqueue(self, record: SpanRecord) -> None: ...

    def flush(self, timeout: Optional[float] = None) -> None: ...

    def close(self) -> None: ...

    def warn_if_queued(self) -> None: ...

    def reinit_after_fork(self) -> None: ...


class PostHogTraces:
    def __init__(
        self,
        client: Any,
        config: ResolvedTracesConfig,
        get_context: GetContextFn,
        active_var: ContextVar,
        exporter: Exporter,
        drops: DropLog,
    ) -> None:
        self._client = client
        self._config = config
        self._get_context = get_context
        self._active_var = active_var
        self._exporter = exporter
        self._drops = drops
        self._lock = threading.Lock()
        self._closed = False
        # span id -> monotonic start. Never the span itself, so a dropped handle
        # stays collectable. Insertion order is start order.
        self._live_spans: Dict[str, float] = {}

    def start_span(
        self,
        name: str,
        *,
        kind: Optional[str] = None,
        attributes: Optional[Mapping[str, Any]] = None,
        parent: Any = None,
        tracestate: Any = None,
        start_time: Any = None,
    ) -> Span:
        """Start a span without making it active; always returns a handle."""
        try:
            return self._start_span(
                name, kind, attributes, parent, tracestate, start_time
            )
        except Exception:
            log.debug("start_span failed; returning an inert span", exc_info=True)
            return inert_span(parent, tracestate, self._active_var)
        finally:
            self._drops.warn_if_due()

    def flush(self, timeout: Optional[float] = None) -> None:
        self._exporter.flush(timeout)

    def close(self) -> None:
        """Stop tracing: later spans are inert, and open ones are dropped when they end."""
        with self._lock:
            self._closed = True
            open_spans = len(self._live_spans)
            self._live_spans.clear()
        if open_spans:
            self._drops.record(open_spans, "they were still open at shutdown")
        self._exporter.close()
        self._drops.warn_if_due(force=True)

    def warn_if_queued(self) -> None:
        """Warn about spans still queued at exit, without discarding them."""
        self._exporter.warn_if_queued()
        self._drops.warn_if_due(force=True)

    def reinit_after_fork(self) -> None:
        # Runs in the forked child before user code; the parent's spans stay
        # with the parent.
        self._lock = threading.Lock()
        self._live_spans.clear()
        self._drops.reinit_after_fork()
        self._exporter.reinit_after_fork()

    def _start_span(
        self,
        name: str,
        kind: Optional[str],
        attributes: Optional[Mapping[str, Any]],
        parent: Any,
        tracestate: Any,
        start_time: Any,
    ) -> Span:
        if self._closed or getattr(self._client, "disabled", False):
            return inert_span(parent, tracestate, self._active_var)

        parent = traceparent_header(parent)
        if parent is not None and not isinstance(parent, (str, RecordingSpan)):
            if isinstance(parent, Span):
                # A child of an inert handle is inert too, still forwarding any
                # inbound context.
                return inert_span(parent, tracestate, self._active_var)
            # Two header values, or another tracer's span.
            log.debug("Ignoring an unusable span parent")
            parent = None

        parent_context = self._resolve_parent(parent, tracestate)

        with self._lock:
            # Swept first, so a process that leaked its way to the bound
            # recovers once the leaks age out.
            aged = self._evict_aged_spans_locked()
            at_limit = len(self._live_spans) >= self._config.max_live_spans
            if not at_limit:
                span_id = new_span_id()
                # Aged from this call, not from a caller-supplied start_time.
                self._live_spans[span_id] = time.monotonic()
        if aged:
            self._drops.record(
                aged,
                "they were still live after {:g}s".format(self._config.max_span_age),
            )
        if at_limit:
            self._drops.record(
                1,
                "the live-span limit ({}) was reached; spans are being started "
                "and never ended".format(self._config.max_live_spans),
            )
            return inert_span(parent, tracestate, self._active_var)

        now_ns = time.time_ns()
        start_ns = resolve_start_ns(start_time, now_ns)
        auto_attributes = self._auto_context_attributes()
        span_attributes = copy_user_attributes(dict(auto_attributes), attributes)

        return RecordingSpan(
            trace_id=parent_context.trace_id if parent_context else new_trace_id(),
            span_id=span_id,
            name=sanitize_name(name, "Span name"),
            start_ns=start_ns,
            backdated=start_ns != now_ns,
            on_end=self._on_span_end,
            kind=kind if isinstance(kind, str) and kind else "internal",
            attributes=span_attributes,
            parent_span_id=parent_context.parent_span_id if parent_context else None,
            trace_state=parent_context.trace_state if parent_context else None,
            trace_flags=parent_context.trace_flags if parent_context else "01",
            parent_is_remote=parent_context.is_remote if parent_context else False,
            active_var=self._active_var,
            # The caller's own start_time wins over the parent's clock basis.
            clock_anchor=parent_context.clock_anchor
            if parent_context and to_epoch_ns(start_time) is None
            else None,
        )

    def _resolve_parent(self, parent: Any, tracestate: Any) -> Optional[ParentContext]:
        """An explicit parent, else the active span, else a fresh root."""
        if isinstance(parent, str):
            remote = self._remote_context(parent, sanitize_tracestate(tracestate))
            if remote is None:
                log.debug("Ignoring malformed traceparent; starting a new trace")
            return remote
        if isinstance(parent, RecordingSpan):
            # The child inherits the parent's tracestate; the argument is ignored.
            return parent._child_context()
        active = self._active_var.get(None)
        if isinstance(active, RecordingSpan):
            return active._child_context()
        if isinstance(active, PassThroughSpan):
            # An earlier span in this trace was not recorded; the trace goes on.
            return self._remote_context(active.traceparent(), active.tracestate())
        return None

    @staticmethod
    def _remote_context(
        traceparent: Any, tracestate: Optional[str]
    ) -> Optional[ParentContext]:
        remote = parse_traceparent(traceparent)
        if remote is None:
            return None
        return ParentContext(
            trace_id=remote.trace_id,
            parent_span_id=remote.span_id,
            trace_state=tracestate,
            trace_flags=remote.flags,
            is_remote=True,
        )

    def _auto_context_attributes(self) -> Dict[str, Any]:
        try:
            context = self._get_context() or {}
        except Exception:
            log.debug("Failed to read the request context for a span", exc_info=True)
            return {}
        attributes: Dict[str, Any] = {}
        for source_key, wire_key in _CONTEXT_ATTRIBUTE_KEYS:
            value = context.get(source_key)
            if value:
                attributes[wire_key] = value
        return attributes

    def _evict_aged_spans_locked(self) -> int:
        # An evicted span is never exported (its end() finds no entry), so a
        # leak returns its slot rather than disabling tracing.
        cutoff = time.monotonic() - self._config.max_span_age
        aged: List[str] = []
        for span_id, started_at in self._live_spans.items():
            if started_at > cutoff:
                break
            aged.append(span_id)
        for span_id in aged:
            del self._live_spans[span_id]
        return len(aged)

    def _on_span_end(self, record: SpanRecord) -> None:
        with self._lock:
            # A miss means it was evicted for age, or tracing was shut down.
            if self._live_spans.pop(record.span_id, None) is None:
                return
            disabled = getattr(self._client, "disabled", False)
        if disabled:
            self._drops.record(1, "the client is disabled")
        else:
            self._exporter.enqueue(record)
        self._drops.warn_if_due()
