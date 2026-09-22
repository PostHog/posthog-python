"""Span handles: the recording span, and the inert handles returned when tracing cannot run."""

import logging
import threading
import time
import traceback
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from ._config import (
    DEFAULT_MAX_ATTRIBUTE_VALUE_LENGTH,
    DEFAULT_MAX_ATTRIBUTES_PER_SPAN,
    DEFAULT_MAX_EVENTS_PER_SPAN,
    MAX_ATTRIBUTES_PER_EVENT,
)
from ._limits import bound_attributes, truncate_attribute_value, truncate_string
from ._otlp import SpanEventRecord, SpanRecord, SpanStatus
from ._sanitize import (
    SpanTimeInput,
    attribute_key,
    clamp_end_ns,
    copy_user_attributes,
    resolve_supplied_ns,
    safe_str,
    sanitize_name,
)
from ._traceparent import (
    TRACE_FLAGS_SAMPLED,
    format_traceparent,
    normalize_traceparent,
    sanitize_tracestate,
    traceparent_header,
)
from .span import Span

log = logging.getLogger("posthog")


@dataclass(frozen=True)
class ClockAnchor:
    """A root span's start on both clocks, shared by its local descendants."""

    wall_ns: int
    mono_ns: int


@dataclass(frozen=True)
class ParentContext:
    """What a child span inherits from its parent."""

    trace_id: str
    parent_span_id: str
    trace_state: Optional[str]
    trace_flags: str
    # True when the parent came from a traceparent header.
    is_remote: bool = False
    # Set only for a local, non-backdated parent.
    clock_anchor: Optional[ClockAnchor] = None


class NoopSpan(Span):
    """Returned when tracing cannot run and there is no inbound trace to forward.

    Never activated; a child started under it is also a no-op.
    """


NOOP_SPAN = NoopSpan()


class _Activatable:
    """Mixin: entering the handle makes it the active span until exit."""

    _active_var: Optional[ContextVar]
    _tokens: List[Token]
    # Guards the tokens and, on a recording span, every write and end().
    _lock: threading.Lock

    def _activate(self) -> None:
        if self._active_var is not None:
            with self._lock:
                self._tokens.append(self._active_var.set(self))

    def _deactivate(self) -> None:
        if self._active_var is None:
            return
        # The same handle can be entered in several threads or tasks at once,
        # and a token only resets in the context that created it.
        with self._lock:
            for index in range(len(self._tokens) - 1, -1, -1):
                try:
                    self._active_var.reset(self._tokens[index])
                except ValueError:
                    continue
                del self._tokens[index]
                return
        log.debug(
            "Span exited in a context that never entered it; it stays active "
            "where it was entered"
        )


class PassThroughSpan(_Activatable, NoopSpan):
    """An inert handle that echoes an inbound ``traceparent`` and ``tracestate``.

    Records nothing, so a service with tracing off still forwards the trace it
    received. Entering it makes it the active span.
    """

    def __init__(
        self,
        traceparent: str,
        tracestate: Optional[str] = None,
        active_var: Optional[ContextVar] = None,
    ) -> None:
        self._traceparent = traceparent
        self._tracestate = tracestate
        self._active_var = active_var
        self._tokens = []
        self._lock = threading.Lock()

    def traceparent(self) -> Optional[str]:
        return self._traceparent

    def tracestate(self) -> Optional[str]:
        return self._tracestate

    def __enter__(self) -> "Span":
        self._activate()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._deactivate()


def inert_span(
    parent: Any = None,
    tracestate: Any = None,
    active_var: Optional[ContextVar] = None,
) -> Span:
    """The handle to return when a span cannot be recorded.

    A pass-through when an inbound context is available, from ``parent`` or
    else the active span, so the trace survives nesting; the no-op otherwise.
    """
    try:
        parent = traceparent_header(parent)
        if isinstance(parent, str) and not parent.strip():
            parent = None
        if parent is None and active_var is not None:
            parent = active_var.get(None)
        if isinstance(parent, Span):
            inbound: Any = parent.traceparent()
            tracestate = parent.tracestate()
        else:
            inbound = parent
        traceparent = normalize_traceparent(inbound)
        if not traceparent:
            return NOOP_SPAN
        return PassThroughSpan(traceparent, sanitize_tracestate(tracestate), active_var)
    except Exception:
        log.debug(
            "Could not read the span parent; returning a no-op span", exc_info=True
        )
        return NOOP_SPAN


def describe_error(error: Any) -> "tuple[str, str]":
    """The OTel ``exception.type`` / ``exception.message`` pair for a raised value."""
    try:
        return type(error).__name__, str(error)
    except Exception:
        return type(error).__name__, ""


def describe_stacktrace(error: Any) -> Optional[str]:
    """The OTel ``exception.stacktrace``, or ``None`` for an exception never raised."""
    try:
        if isinstance(error, BaseException) and error.__traceback__ is not None:
            return "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
    except Exception:
        pass
    return None


def _exception_event_attributes(
    error: Any, max_length: int
) -> "tuple[Dict[str, Any], str]":
    exc_type, message = describe_error(error)
    attributes: Dict[str, Any] = {
        "exception.type": exc_type,
        "exception.message": message,
    }
    # The tail is kept: Python lists the raising frame last. For a chained
    # exception that is the outermost one; a cause printed above it goes first.
    stacktrace = describe_stacktrace(error)
    if stacktrace:
        attributes["exception.stacktrace"] = stacktrace[-max_length:]
    return attributes, message


class RecordingSpan(_Activatable, Span):
    """A span that records and, on ``end()``, hands one record to the pipeline."""

    def __init__(
        self,
        *,
        trace_id: str,
        span_id: str,
        name: str,
        start_ns: int,
        backdated: bool,
        on_end: Callable[[SpanRecord], None],
        kind: str = "internal",
        attributes: Optional[Dict[str, Any]] = None,
        parent_span_id: Optional[str] = None,
        trace_state: Optional[str] = None,
        trace_flags: str = TRACE_FLAGS_SAMPLED,
        parent_is_remote: bool = False,
        active_var: Optional[ContextVar] = None,
        clock_anchor: Optional[ClockAnchor] = None,
        auto_attribute_keys: Iterable[str] = (),
        max_attributes: int = DEFAULT_MAX_ATTRIBUTES_PER_SPAN,
        max_events: int = DEFAULT_MAX_EVENTS_PER_SPAN,
        max_attribute_value_length: int = DEFAULT_MAX_ATTRIBUTE_VALUE_LENGTH,
    ) -> None:
        self._trace_id = trace_id
        self._span_id = span_id
        self._parent_span_id = parent_span_id
        self._trace_state = trace_state
        self._trace_flags = trace_flags
        self._parent_is_remote = parent_is_remote
        self._start_mono: Optional[int] = None if backdated else time.monotonic_ns()
        # A child of a local parent starts on its root's clock basis, so clock
        # rounding or a wall-clock step cannot place it outside its parent.
        if clock_anchor is not None and self._start_mono is not None:
            start_ns = clock_anchor.wall_ns + (self._start_mono - clock_anchor.mono_ns)
        self._start_ns = start_ns
        self._clock_anchor: Optional[ClockAnchor] = None
        if self._start_mono is not None:
            self._clock_anchor = clock_anchor or ClockAnchor(start_ns, self._start_mono)
        self._on_end = on_end
        self._active_var = active_var
        self._tokens = []
        self._lock = threading.Lock()

        self._name = name
        self._kind = kind
        # The SDK's own join keys, exempt from the attribute cap.
        self._auto_keys = frozenset(auto_attribute_keys)
        self._max_attributes = max_attributes
        self._max_events = max_events
        self._max_attribute_value_length = max_attribute_value_length
        self._user_attribute_count = 0
        self._user_event_count = 0
        self._dropped_attributes = 0
        self._dropped_events = 0
        self._attributes: Dict[str, Any] = {}
        self._events: List[SpanEventRecord] = []
        self._status: Optional[SpanStatus] = None
        self._ended = False
        for key, value in (attributes or {}).items():
            self._write_attribute(key, value)

    def _now_ns(self) -> int:
        """Now, on this span's clock basis: start plus monotonic elapsed, else wall clock."""
        if self._start_mono is not None:
            return self._start_ns + max(0, time.monotonic_ns() - self._start_mono)
        return time.time_ns()

    def _mutable(self, operation: str) -> bool:
        if self._ended:
            log.debug("Ignoring %s on a span that has already ended", operation)
            return False
        return True

    def _write_attribute(self, key: str, value: Any) -> None:
        """Write an attribute unless the span is at its cap of distinct user keys."""
        if not key:
            # The encoder drops it, so it must not spend a slot.
            log.debug("Dropping an attribute with an empty key")
            return
        with self._lock:
            if self._ended:
                return
            if value is None:
                # None removes the key, freeing its slot.
                if key in self._attributes and key not in self._auto_keys:
                    self._user_attribute_count -= 1
                self._attributes.pop(key, None)
                return
            # Checked before the value is walked, which is the costly part.
            if key not in self._auto_keys and key not in self._attributes:
                if self._user_attribute_count >= self._max_attributes:
                    self._dropped_attributes += 1
                    return
                self._user_attribute_count += 1
                # Reserved, so a concurrent write to the same key sees it taken.
                self._attributes[key] = None
        bounded = truncate_attribute_value(value, self._max_attribute_value_length)
        with self._lock:
            if not self._ended:
                self._attributes[key] = bounded

    def set_attribute(self, key: str, value: Any) -> "Span":
        if self._mutable("set_attribute"):
            key_str = attribute_key(key)
            if key_str is not None:
                self._write_attribute(key_str, value)
        return self

    def set_attributes(self, attributes: Mapping[str, Any]) -> "Span":
        if self._mutable("set_attributes"):
            for key, value in copy_user_attributes({}, attributes).items():
                self._write_attribute(key, value)
        return self

    def add_event(
        self,
        name: str,
        attributes: Optional[Mapping[str, Any]] = None,
        timestamp: Optional[SpanTimeInput] = None,
    ) -> "Span":
        if self._mutable("add_event"):
            with self._lock:
                if self._ended:
                    return self
                # A recorded exception spends a slot like any other event.
                if self._user_event_count >= self._max_events:
                    self._dropped_events += 1
                    return self
                self._user_event_count += 1
            bounded: Optional[Dict[str, Any]] = None
            dropped = 0
            if attributes is not None:
                bounded, dropped = bound_attributes(
                    copy_user_attributes({}, attributes),
                    MAX_ATTRIBUTES_PER_EVENT,
                    self._max_attribute_value_length,
                )
            event = SpanEventRecord(
                name=sanitize_name(
                    name, "Span event name", self._max_attribute_value_length
                ),
                timestamp_ns=resolve_supplied_ns(
                    timestamp, self._now_ns(), "event timestamp"
                ),
                attributes=bounded,
                dropped_attributes_count=dropped,
            )
            with self._lock:
                if not self._ended:
                    self._events.append(event)
        return self

    def set_status(self, code: str, message: Optional[str] = None) -> "Span":
        if self._mutable("set_status"):
            if not isinstance(code, str) or code not in ("ok", "error"):
                log.debug('Ignoring an unknown span status; expected "ok" or "error"')
                return self
            text = None if message is None else safe_str(message)
            status = SpanStatus(
                code,
                truncate_string(text, self._max_attribute_value_length)
                if text
                else None,
            )
            with self._lock:
                if not self._ended:
                    self._status = status
        return self

    @property
    def _status_is_explicitly_ok(self) -> bool:
        return self._status is not None and self._status.code == "ok"

    def record_exception(self, exception: BaseException) -> "Span":
        if self._mutable("record_exception"):
            self._record_exception(exception, keep_ok=False)
        return self

    def _record_exception(self, exception: BaseException, keep_ok: bool) -> None:
        attributes, message = _exception_event_attributes(
            exception, self._max_attribute_value_length
        )
        self.add_event("exception", attributes)
        # Only the scoped form treats an explicit `ok` as final.
        if not (keep_ok and self._status_is_explicitly_ok):
            self.set_status("error", message)

    def update_name(self, name: str) -> "Span":
        if self._mutable("update_name"):
            sanitized = sanitize_name(
                name, "Span name", self._max_attribute_value_length
            )
            with self._lock:
                if not self._ended:
                    self._name = sanitized
        return self

    def traceparent(self) -> Optional[str]:
        return format_traceparent(self._trace_id, self._span_id, self._trace_flags)

    def tracestate(self) -> Optional[str]:
        return self._trace_state

    def _child_context(self) -> ParentContext:
        return ParentContext(
            trace_id=self._trace_id,
            parent_span_id=self._span_id,
            trace_state=self._trace_state,
            trace_flags=self._trace_flags,
            clock_anchor=self._clock_anchor,
        )

    def end(self, end_time: Optional[SpanTimeInput] = None) -> None:
        with self._lock:
            if self._ended:
                log.debug("Ignoring end() on a span that has already ended")
                return
            self._ended = True
            # Snapshotted under the lock: a write that lost the race to end()
            # must not land in the record or after it.
            attributes = {
                key: value
                for key, value in self._attributes.items()
                if value is not None
            }
            events = list(self._events)
            name, status = self._name, self._status
            dropped_attributes = self._dropped_attributes
            dropped_events = self._dropped_events

        derived = self._now_ns()
        resolved = resolve_supplied_ns(end_time, derived, "end time")
        record = SpanRecord(
            trace_id=self._trace_id,
            span_id=self._span_id,
            parent_span_id=self._parent_span_id,
            trace_state=self._trace_state,
            trace_flags=self._trace_flags,
            parent_is_remote=self._parent_is_remote,
            name=name,
            kind=self._kind,
            status=status,
            attributes=attributes,
            events=events,
            start_ns=self._start_ns,
            end_ns=clamp_end_ns(resolved, self._start_ns),
            dropped_attributes_count=dropped_attributes,
            dropped_events_count=dropped_events,
            auto_attribute_keys=self._auto_keys,
        )
        try:
            self._on_end(record)
        except Exception:
            log.debug("Failed to enqueue span", exc_info=True)

    def __enter__(self) -> "Span":
        self._activate()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Detached before it ends, so a span started from the on_end path
        # does not become a child of one that is already over.
        self._deactivate()
        # GeneratorExit, CancelledError and the like are control flow, not
        # failures of the span's work.
        if isinstance(exc, Exception) and not self._ended:
            self._record_exception(exc, keep_ok=True)
        self.end()
