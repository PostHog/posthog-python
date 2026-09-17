"""The ``before_span_send`` hook chain, where a finished span is scrubbed or dropped.

The hook receives a plain dict, like the events ``before_send`` hook. It is the
scrubbing point, so a hook that raises drops the span rather than letting the
unscrubbed record through.
"""

import inspect
import logging
from typing import Any, Dict, Mapping, Optional

from ._config import MAX_ATTRIBUTES_PER_EVENT, ResolvedTracesConfig
from ._drops import DropLog
from ._limits import apply_span_limits
from ._otlp import SpanEventRecord, SpanRecord, SpanStatus, non_negative_count
from ._sanitize import (
    MAX_TIMESTAMP_NS,
    MIN_TIMESTAMP_NS,
    clamp_end_ns,
    copy_user_attributes,
    safe_str,
    sanitize_name,
)

log = logging.getLogger("posthog")

_READ_ONLY_ID_KEYS = ("trace_id", "span_id", "parent_span_id")


def run_before_span_send(
    record: SpanRecord, config: ResolvedTracesConfig, drops: DropLog
) -> Optional[SpanRecord]:
    """Run the hook chain, returning the span to queue or ``None`` to drop it.

    The ids are read-only: they are restored after every hook, since a
    rewritten id would orphan children already sent.
    """
    if not config.before_span_send:
        return record

    identity = (record.trace_id, record.span_id, record.parent_span_id)
    # The span's own write order, so the caps keep the earliest-set entries.
    keys_before_hook = list(record.attributes)
    data = _hook_view(record)
    try:
        for hook in config.before_span_send:
            result: Any = hook(data)
            if result is None:
                # The documented way to filter, so not a drop worth warning about.
                log.debug("before_span_send dropped the span")
                return None
            if inspect.iscoroutine(result):
                # Not awaited; closed so it does not warn.
                result.close()
                drops.record(1, "before_span_send is async, which is not supported")
                return None
            if not isinstance(result, Mapping):
                log.debug(
                    "before_span_send did not return a span dict; dropping the span"
                )
                drops.record(1, "before_span_send returned an unusable record")
                return None
            data = _keep_identity(result, identity)
        rebuilt = _rebuild(record, data, config)
        if rebuilt is None:
            log.debug("before_span_send did not return a span dict; dropping the span")
            drops.record(1, "before_span_send returned an unusable record")
            return None
        apply_span_limits(
            rebuilt,
            record.auto_attribute_keys,
            config.max_attributes_per_span,
            config.max_events_per_span,
            MAX_ATTRIBUTES_PER_EVENT,
            config.max_attribute_value_length,
            keys_before_hook,
        )
        return rebuilt
    except Exception:
        drops.warn_failure(
            "before_span_send raised; dropping the span rather than exporting it "
            "unscrubbed"
        )
        drops.record(1, "before_span_send failed")
        return None


def _hook_view(record: SpanRecord) -> Dict[str, Any]:
    return {
        "trace_id": record.trace_id,
        "span_id": record.span_id,
        "parent_span_id": record.parent_span_id,
        "name": record.name,
        "kind": record.kind,
        "status": (
            {"code": record.status.code, "message": record.status.message}
            if record.status is not None
            else None
        ),
        "attributes": dict(record.attributes),
        "events": [
            {
                "name": event.name,
                "timestamp_ns": event.timestamp_ns,
                "attributes": dict(event.attributes or {}),
                "dropped_attributes_count": event.dropped_attributes_count,
            }
            for event in record.events
        ],
        "start_time_ns": record.start_ns,
        "end_time_ns": record.end_ns,
    }


def _keep_identity(result: Mapping, identity: tuple) -> Dict[str, Any]:
    data = result if isinstance(result, dict) else dict(result)
    if any(data.get(key) != value for key, value in zip(_READ_ONLY_ID_KEYS, identity)):
        log.debug(
            "before_span_send changed a span identity field; keeping the original ids"
        )
        data.update(zip(_READ_ONLY_ID_KEYS, identity))
    return data


def _rebuild(
    record: SpanRecord, data: Mapping, config: ResolvedTracesConfig
) -> Optional[SpanRecord]:
    """A span record from what the chain returned, sanitized as ``end()`` would.

    ``None`` when it is not a span dict, rather than exporting a span of
    fallbacks joinable to nothing. A field the hook left out or made unusable
    keeps the original's value.
    """
    attributes = data.get("attributes")
    events = data.get("events")
    if not isinstance(attributes, Mapping) or not isinstance(events, (list, tuple)):
        return None

    max_length = config.max_attribute_value_length
    start_ns = _valid_ns(data.get("start_time_ns"), record.start_ns)
    end_ns = clamp_end_ns(_valid_ns(data.get("end_time_ns"), record.end_ns), start_ns)
    kind = data.get("kind")

    rebuilt_events = []
    for event in events:
        try:
            event_attributes = event.get("attributes")
            rebuilt_events.append(
                SpanEventRecord(
                    name=sanitize_name(
                        event.get("name"), "Span event name", max_length
                    ),
                    timestamp_ns=_valid_ns(event.get("timestamp_ns"), start_ns),
                    attributes=copy_user_attributes({}, event_attributes)
                    if event_attributes is not None
                    else None,
                    dropped_attributes_count=non_negative_count(
                        event.get("dropped_attributes_count")
                    ),
                )
            )
        except Exception:
            log.debug("before_span_send left an unreadable span event; dropping it")

    return SpanRecord(
        trace_id=record.trace_id,
        span_id=record.span_id,
        parent_span_id=record.parent_span_id,
        # SDK bookkeeping the hook is not shown, so it cannot erase it.
        trace_state=record.trace_state,
        trace_flags=record.trace_flags,
        parent_is_remote=record.parent_is_remote,
        dropped_attributes_count=record.dropped_attributes_count,
        dropped_events_count=record.dropped_events_count,
        auto_attribute_keys=record.auto_attribute_keys,
        name=sanitize_name(data.get("name", record.name), "Span name", max_length),
        kind=kind if isinstance(kind, str) else record.kind,
        status=_hook_status(data.get("status"), record.status),
        attributes=copy_user_attributes({}, attributes),
        events=rebuilt_events,
        start_ns=start_ns,
        end_ns=end_ns,
    )


def _hook_status(value: Any, original: Optional[SpanStatus]) -> Optional[SpanStatus]:
    if value is None:
        return None
    if isinstance(value, Mapping) and value.get("code") in ("ok", "error"):
        message = value.get("message")
        text = None if message is None else safe_str(message)
        return SpanStatus(value["code"], text or None)
    # An unknown code would lose an error the span really had.
    log.debug("before_span_send set an unknown span status; keeping the original")
    return original


def _valid_ns(value: Any, fallback: int) -> int:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_TIMESTAMP_NS <= value <= MAX_TIMESTAMP_NS
    ):
        return value
    log.debug(
        "before_span_send set a time that is not an epoch-nanosecond int; keeping the original"
    )
    return fallback
