"""Per-span limits: the attribute and event caps, and the value-length bound.

The length bound reaches every string a value contains, nested ones included:
one huge value otherwise gets the whole span dropped as too large. The walk
mirrors ``_otlp._encode`` (depth, item and node budgets, cycle marker, skipped
keys) so it bounds exactly what the encoder will emit.
"""

from datetime import date
from typing import AbstractSet, Any, Dict, List, Mapping, Sequence, Tuple

from ._otlp import (
    CIRCULAR_VALUE,
    MAX_VALUE_DEPTH,
    MAX_VALUE_ITEMS,
    MAX_VALUE_NODES,
    TRUNCATED_VALUE,
    SpanRecord,
    SpanStatus,
    non_negative_count,
)
from ._sanitize import UNSERIALIZABLE_VALUE, attribute_key


class _WalkState:
    __slots__ = ("ancestors", "remaining_nodes")

    def __init__(self) -> None:
        self.ancestors: set = set()
        self.remaining_nodes = MAX_VALUE_NODES


def truncate_string(value: str, max_length: int) -> str:
    return value[:max_length] if len(value) > max_length else value


def truncate_attribute_value(value: Any, max_length: int) -> Any:
    """Bound every string reachable from ``value`` to ``max_length`` characters.

    A value that cannot be walked is returned as it is.
    """
    # The common case never allocates a walk.
    if isinstance(value, str):
        return truncate_string(value, max_length)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return _truncate(value, max_length, _WalkState(), 0)
    except Exception:
        return value


def _truncate(value: Any, max_length: int, state: _WalkState, depth: int) -> Any:
    if value is None:
        return None
    is_container = isinstance(value, (Mapping, list, tuple, set, frozenset))
    if not is_container:
        # Leaves are charged too: a shared subtree is walked once per path.
        if state.remaining_nodes <= 0:
            return value
        state.remaining_nodes -= 1
        if isinstance(value, str):
            return truncate_string(value, max_length)
        if isinstance(value, (bool, int, float, date)) or callable(value):
            return value
        # The encoder stringifies anything else, so bound that text.
        try:
            return truncate_string(str(value), max_length)
        except Exception:
            return UNSERIALIZABLE_VALUE

    marker = id(value)
    if marker in state.ancestors:
        # The marker, not the ancestor: inside a copied parent the encoder would
        # no longer see the cycle.
        return CIRCULAR_VALUE
    if state.remaining_nodes <= 0 or depth >= MAX_VALUE_DEPTH:
        return value
    state.remaining_nodes -= 1
    state.ancestors.add(marker)
    try:
        if isinstance(value, Mapping):
            return _truncate_mapping(value, max_length, state, depth)
        return _truncate_items(value, max_length, state, depth)
    finally:
        state.ancestors.discard(marker)


def _truncate_mapping(
    value: Mapping, max_length: int, state: _WalkState, depth: int
) -> dict:
    bounded: dict = {}
    emittable = 0
    for key in list(value.keys()):
        if emittable >= MAX_VALUE_ITEMS:
            break
        key_str = attribute_key(key)
        if not key_str:
            continue
        try:
            item = _truncate(value[key], max_length, state, depth + 1)
        except Exception:
            item = UNSERIALIZABLE_VALUE
        if item is not None:
            emittable += 1
        bounded[key] = item
    return bounded


def _truncate_items(value: Any, max_length: int, state: _WalkState, depth: int) -> list:
    bounded: list = []
    for index, element in enumerate(value):
        if index >= MAX_VALUE_ITEMS:
            bounded.append(TRUNCATED_VALUE)
            break
        try:
            bounded.append(_truncate(element, max_length, state, depth + 1))
        except Exception:
            bounded.append(UNSERIALIZABLE_VALUE)
    return bounded


def bound_attributes(
    source: Mapping[str, Any], max_count: int, max_length: int
) -> Tuple[Dict[str, Any], int]:
    """A copy of a ``copy_user_attributes`` result with at most ``max_count``
    entries, each bounded, and how many entries the cap refused. An empty key
    or a ``None`` value spends no slot."""
    attributes: Dict[str, Any] = {}
    dropped = 0
    for key, value in source.items():
        if not key or value is None:
            continue
        if len(attributes) >= max_count and key not in attributes:
            dropped += 1
            continue
        attributes[key] = truncate_attribute_value(value, max_length)
    return attributes, dropped


def truncate_attributes(attributes: Mapping, max_length: int) -> Dict[str, Any]:
    """``truncate_attribute_value`` across an attribute mapping, as a copy."""
    return {
        key: truncate_attribute_value(value, max_length)
        for key, value in attributes.items()
    }


def _ordered_keys(attributes: Mapping, keys_before_hook: Sequence[str]) -> List[Any]:
    """The keys the span set first, in its order, so the earliest-set entries win."""
    keys = list(attributes.keys())
    present = set(keys)
    before = [key for key in keys_before_hook if key in present]
    seen = set(before)
    return before + [key for key in keys if key not in seen]


def apply_span_limits(
    record: SpanRecord,
    auto_keys: AbstractSet[str],
    max_attributes: int,
    max_events: int,
    max_attributes_per_event: int,
    max_length: int,
    keys_before_hook: Sequence[str] = (),
    bounded_before_hook: Mapping[str, Any] = {},
) -> None:
    """Re-apply the per-span caps after a ``before_span_send`` hook, which
    bypasses the span's own writer. Counts add to what the span already dropped.
    A value still the object the span bounded at write time is not walked again."""
    attributes: Dict[str, Any] = {}
    kept = 0
    dropped_attributes = 0
    for key in _ordered_keys(record.attributes, keys_before_hook):
        value = record.attributes[key]
        if value is None or not key:
            continue
        if key not in auto_keys:
            if kept >= max_attributes:
                dropped_attributes += 1
                continue
            kept += 1
        if bounded_before_hook.get(key) is value:
            attributes[key] = value
        else:
            attributes[key] = truncate_attribute_value(value, max_length)
    record.attributes = attributes
    if dropped_attributes:
        record.dropped_attributes_count = (
            non_negative_count(record.dropped_attributes_count) + dropped_attributes
        )

    kept_events: list = []
    dropped_events = 0
    for event in record.events:
        if len(kept_events) >= max_events:
            dropped_events += 1
            continue
        if event.attributes:
            event.attributes, dropped = bound_attributes(
                event.attributes, max_attributes_per_event, max_length
            )
            if dropped:
                event.dropped_attributes_count = (
                    non_negative_count(event.dropped_attributes_count) + dropped
                )
        kept_events.append(event)
    record.events = kept_events
    if dropped_events:
        record.dropped_events_count = (
            non_negative_count(record.dropped_events_count) + dropped_events
        )

    if record.status is not None and record.status.message:
        record.status = SpanStatus(
            record.status.code, truncate_string(record.status.message, max_length)
        )
