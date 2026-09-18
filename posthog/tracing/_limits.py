"""Per-span limits: the attribute and event caps, and the value-length bound.

The length bound reaches every string a value contains, nested ones included:
one huge value otherwise gets the whole span dropped as too large. The walk
mirrors ``_otlp._encode`` (depth, item and node budgets, cycle marker, skipped
keys) so it bounds exactly what the encoder will emit.
"""

from datetime import date
from typing import Any, Dict, Mapping, Tuple

from ._otlp import (
    CIRCULAR_VALUE,
    MAX_VALUE_DEPTH,
    MAX_VALUE_ITEMS,
    MAX_VALUE_NODES,
    TRUNCATED_VALUE,
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
