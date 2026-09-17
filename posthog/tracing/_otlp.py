"""OTLP/JSON encoding for spans.

Attribute values come from application code, and one the server refuses
rejects the whole request, so they are encoded to an acceptable payload
whatever they are. Ids and timestamps are trusted: the span pipeline only
hands over ones it generated or validated.
"""

import logging
import math
import platform
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..version import VERSION
from ._sanitize import FUNCTION_VALUE, UNSERIALIZABLE_VALUE, attribute_key, safe_str

log = logging.getLogger("posthog")

SCOPE_NAME = "posthog-python"

SPAN_KIND_TO_OTLP = {
    "internal": 1,
    "server": 2,
    "client": 3,
    "producer": 4,
    "consumer": 5,
}
SPAN_STATUS_TO_OTLP = {"ok": 1, "error": 2}

# The W3C trace flags are the low byte; OTel's parent-remoteness bits sit above.
SAMPLED_FLAG_BIT = 0x01
SPAN_FLAGS_CONTEXT_HAS_IS_REMOTE = 0x100
SPAN_FLAGS_CONTEXT_IS_REMOTE = 0x200

INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)

# Bounds on the value walk, so a self-referencing value terminates.
MAX_VALUE_DEPTH = 20
MAX_VALUE_ITEMS = 1000
MAX_VALUE_NODES = 10000
CIRCULAR_VALUE = "[Circular]"
TRUNCATED_VALUE = "[Truncated]"


@dataclass
class SpanEventRecord:
    name: str
    timestamp_ns: int
    attributes: Optional[Dict[str, Any]] = None
    dropped_attributes_count: int = 0


@dataclass
class SpanStatus:
    code: str  # "ok" | "error"
    message: Optional[str] = None


@dataclass
class SpanRecord:
    """A completed span in plain, pre-encoding form."""

    trace_id: str
    span_id: str
    name: str
    start_ns: int
    end_ns: int
    parent_span_id: Optional[str] = None
    trace_state: Optional[str] = None
    trace_flags: str = "01"
    # True when the parent came from a traceparent header.
    parent_is_remote: bool = False
    kind: str = "internal"
    status: Optional[SpanStatus] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[SpanEventRecord] = field(default_factory=list)
    # User attributes and events the per-span caps refused.
    dropped_attributes_count: int = 0
    dropped_events_count: int = 0


MAX_UINT32 = 0xFFFFFFFF


def non_negative_count(value: Any) -> int:
    """A dropped count as the ``uint32`` the wire declares, or 0 for anything else.

    One that overflows the field is refused for the whole request.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value) or value <= 0:
        return 0
    return min(int(value), MAX_UINT32)


def sanitize_string(value: str) -> str:
    """Replace unpaired surrogates, which the service rejects, with U+FFFD."""
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def wire_string(value: Any) -> str:
    return sanitize_string(safe_str(value))


class _EncodeState:
    __slots__ = ("ancestors", "remaining_nodes")

    def __init__(self) -> None:
        # Containers on the current path; a back-reference becomes a marker.
        self.ancestors: set = set()
        self.remaining_nodes = MAX_VALUE_NODES


def to_any_value(value: Any) -> dict:
    """Encode one attribute value as an OTLP ``AnyValue``."""
    try:
        return _encode(value, _EncodeState(), 0)
    except Exception:
        return {"stringValue": UNSERIALIZABLE_VALUE}


def encode_attributes(attributes: Any) -> Tuple[list, int]:
    """An attribute mapping as an OTLP ``KeyValue`` list, and how many entries
    the encoder's budget cut. ``None`` values are dropped and not counted."""
    try:
        return _encode_key_value_list(attributes, _EncodeState(), 0)
    except Exception:
        return [], 0


def to_key_value_list(attributes: Any) -> list:
    """Encode an attribute mapping as an OTLP ``KeyValue`` list; ``None`` values are dropped."""
    return encode_attributes(attributes)[0]


def _encode(value: Any, state: _EncodeState, depth: int) -> dict:
    if state.remaining_nodes <= 0:
        return {"stringValue": TRUNCATED_VALUE}
    state.remaining_nodes -= 1

    # bool is an int subclass.
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        if value > INT64_MAX or value < INT64_MIN:
            log.debug(
                "Attribute %s is outside the int64 range; encoding it as a string",
                value,
            )
            return {"stringValue": str(value)}
        # int(): an IntEnum stringifies as its name.
        return {"intValue": str(int(value))}
    if isinstance(value, float):
        if not math.isfinite(value):
            if math.isnan(value):
                return {"stringValue": "NaN"}
            return {"stringValue": "Infinity" if value > 0 else "-Infinity"}
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": sanitize_string(value)}
    if isinstance(value, (datetime, date)):
        return {"stringValue": value.isoformat()}
    if isinstance(value, Mapping) or isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in state.ancestors:
            return {"stringValue": CIRCULAR_VALUE}
        if depth >= MAX_VALUE_DEPTH:
            return {"stringValue": TRUNCATED_VALUE}
        state.ancestors.add(marker)
        try:
            if isinstance(value, Mapping):
                return {
                    "kvlistValue": {
                        "values": _encode_key_value_list(value, state, depth + 1)[0]
                    }
                }
            return {"arrayValue": {"values": _encode_array(value, state, depth + 1)}}
        finally:
            # Siblings sharing one object are not a cycle.
            state.ancestors.discard(marker)
    if callable(value):
        return {"stringValue": FUNCTION_VALUE}
    return {"stringValue": wire_string(value)}


def _encode_array(values: Any, state: _EncodeState, depth: int) -> list:
    result: list = []
    count = 0
    truncated = False
    for element in values:
        if count >= MAX_VALUE_ITEMS or state.remaining_nodes <= 0:
            truncated = True
            break
        count += 1
        # proto3 JSON has no null AnyValue.
        if element is None:
            continue
        try:
            result.append(_encode(element, state, depth))
        except Exception:
            result.append({"stringValue": UNSERIALIZABLE_VALUE})
    if truncated:
        result.append({"stringValue": TRUNCATED_VALUE})
    return result


def _encode_key_value_list(
    attributes: Any, state: _EncodeState, depth: int
) -> Tuple[list, int]:
    result: list = []
    if not isinstance(attributes, Mapping):
        return result, 0
    keys = list(attributes.keys())
    for index, key in enumerate(keys):
        key_str = attribute_key(key)
        if key_str is None:
            continue
        if not key_str:
            log.debug("Dropping an attribute with an empty key")
            continue
        if len(result) >= MAX_VALUE_ITEMS or state.remaining_nodes <= 0:
            log.debug("Attributes truncated: the value exceeds the OTLP encoder budget")
            return result, _emittable_count(attributes, keys[index:])
        try:
            value = attributes[key]
            if value is None:
                continue
            result.append(
                {"key": sanitize_string(key_str), "value": _encode(value, state, depth)}
            )
        except Exception:
            result.append(
                {
                    "key": sanitize_string(key_str),
                    "value": {"stringValue": UNSERIALIZABLE_VALUE},
                }
            )
    return result, 0


def _emittable_count(attributes: Mapping, keys: List[Any]) -> int:
    """How many of ``keys`` the encoder would have emitted: a non-empty key with a value."""
    count = 0
    for key in keys:
        key_str = attribute_key(key)
        if not key_str:
            continue
        try:
            if attributes[key] is None:
                continue
        except Exception:
            pass
        count += 1
    return count


def span_kind_to_otlp(kind: Any) -> int:
    if isinstance(kind, str) and kind in SPAN_KIND_TO_OTLP:
        return SPAN_KIND_TO_OTLP[kind]
    return SPAN_KIND_TO_OTLP["internal"]


def _span_flags(record: SpanRecord) -> int:
    try:
        w3c = int(record.trace_flags, 16) & 0xFF
    except (TypeError, ValueError):
        w3c = SAMPLED_FLAG_BIT
    return (
        w3c
        | SPAN_FLAGS_CONTEXT_HAS_IS_REMOTE
        | (SPAN_FLAGS_CONTEXT_IS_REMOTE if record.parent_is_remote else 0)
    )


def _to_otlp_event(event: SpanEventRecord) -> dict:
    encoded: dict = {
        "name": wire_string(event.name),
        "timeUnixNano": str(event.timestamp_ns),
    }
    cut = 0
    if event.attributes:
        attributes, cut = encode_attributes(event.attributes)
        if attributes:
            encoded["attributes"] = attributes
    dropped = non_negative_count(
        non_negative_count(event.dropped_attributes_count) + cut
    )
    if dropped:
        encoded["droppedAttributesCount"] = dropped
    return encoded


def build_otlp_span(record: SpanRecord) -> dict:
    span: dict = {
        "traceId": record.trace_id,
        "spanId": record.span_id,
        "name": wire_string(record.name),
        "kind": span_kind_to_otlp(record.kind),
        "startTimeUnixNano": str(record.start_ns),
        "endTimeUnixNano": str(record.end_ns),
        "flags": _span_flags(record),
    }
    if record.parent_span_id:
        span["parentSpanId"] = record.parent_span_id
    if record.trace_state:
        span["traceState"] = wire_string(record.trace_state)
    attributes, cut = encode_attributes(record.attributes)
    if attributes:
        span["attributes"] = attributes
    if record.events:
        span["events"] = [_to_otlp_event(event) for event in record.events]
    dropped_attributes = non_negative_count(
        non_negative_count(record.dropped_attributes_count) + cut
    )
    if dropped_attributes:
        span["droppedAttributesCount"] = dropped_attributes
    dropped_events = non_negative_count(record.dropped_events_count)
    if dropped_events:
        span["droppedEventsCount"] = dropped_events
    if record.status is not None and record.status.code in SPAN_STATUS_TO_OTLP:
        status: dict = {"code": SPAN_STATUS_TO_OTLP[record.status.code]}
        if record.status.message:
            status["message"] = wire_string(record.status.message)
        span["status"] = status
    return span


def build_traces_payload(spans: List[dict], resource: list) -> dict:
    """Wrap spans in the OTLP envelope: one resource, one scope, N spans per batch.

    ``resource`` is the ``to_resource_key_value_list`` output, encoded once by
    the caller since it is the same for every batch.
    """
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource},
                "scopeSpans": [
                    {
                        "scope": {"name": SCOPE_NAME, "version": VERSION},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def build_resource_attributes(
    service_name: Optional[str],
    service_version: Optional[str],
    environment: Optional[str],
    resource_attributes: Mapping,
) -> dict:
    """OTLP resource attributes for every batch; the SDK's identity keys win.

    ``service.name`` is always emitted: the server attributes spans by it alone.
    """
    attributes: dict = dict(resource_attributes)
    attributes["service.name"] = service_name or "unknown_service"
    if environment:
        attributes["deployment.environment"] = environment
    if service_version:
        attributes["service.version"] = service_version
    attributes["telemetry.sdk.name"] = SCOPE_NAME
    attributes["telemetry.sdk.version"] = VERSION
    return attributes


_SDK_RESOURCE_KEYS = (
    "service.name",
    "deployment.environment",
    "service.version",
    "telemetry.sdk.name",
    "telemetry.sdk.version",
)


def to_resource_key_value_list(attributes: Mapping) -> list:
    """Encode resource attributes, the SDK's keys last on a budget of their own,
    so a huge user value cannot cost the resource its ``service.name``."""
    user = dict(attributes)
    sdk = {key: user.pop(key) for key in _SDK_RESOURCE_KEYS if key in user}
    return to_key_value_list(user) + to_key_value_list(sdk)


# platform.system() spellings that differ from the os.name the other PostHog
# SDKs send; every other spelling already matches.
_OS_NAMES = {"Darwin": "macOS"}

# POSIX layers over Windows report e.g. "CYGWIN_NT-10.0-19045"; they belong
# under the same filter as Windows, as in posthog-node.
_WINDOWS_LAYER_PREFIXES = ("CYGWIN", "MSYS", "MINGW")


def _os_name(system: str) -> str:
    if system.upper().startswith(_WINDOWS_LAYER_PREFIXES):
        return "Windows"
    return _OS_NAMES.get(system, system)


def host_resource_attributes() -> Dict[str, str]:
    """The ``os.name`` / ``os.version`` pair, each omitted when the host cannot say."""
    attributes: Dict[str, str] = {}
    try:
        name = platform.system()
        if name:
            attributes["os.name"] = _os_name(name)
        # On Windows release() is just "10" or "11"; version() is the build,
        # e.g. "10.0.22631", which is what posthog-node sends.
        version = platform.version() if name == "Windows" else platform.release()
        if version:
            attributes["os.version"] = version
    except Exception:
        pass
    return attributes
