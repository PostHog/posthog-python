"""Event shaping for the capture v1 wire protocol.

Transforms a legacy-shaped queued message into a v1 wire event and assembles
the batch envelope. :mod:`posthog.capture_send` posts the result.

The v1 contract (see ``rust/capture/src/v1/analytics/types.rs``) differs from
the legacy queued-message shape in a few load-bearing ways that this module
encodes:

- A typed ``options`` object carries a handful of sentinel properties, renamed
  and strictly typed. Wrong JSON types fail deserialization of the *whole
  batch*, so values are coerced to native types or omitted entirely.
- ``$set``/``$set_once`` have no top-level form in v1; the server reads them
  from ``properties``. The legacy ``set()``/``set_once()`` builders emit them at
  the top level, so they are relocated into ``properties`` here.
- ``$lib``/``$lib_version`` are injected server-side from the required
  ``PostHog-Sdk-Info`` header and are stripped from v1 properties.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Optional

from posthog.utils import _normalize_timestamp

# Sentinel properties lifted to top-level string fields on the event.
_TOPLEVEL_SENTINELS: tuple[tuple[str, str], ...] = (
    ("$session_id", "session_id"),
    ("$window_id", "window_id"),
)

# Top-level legacy keys relocated into properties (v1 has no top-level form).
_RELOCATE_TO_PROPERTIES = ("$set", "$set_once")

# Properties dropped from v1 events (server injects them from PostHog-Sdk-Info).
_STRIP_FROM_PROPERTIES = ("$lib", "$lib_version")


def _coerce_bool(value: Any) -> Optional[bool]:
    """Coerce a sentinel value to ``bool`` using the backend's truthiness rules.

    Native bool passes through; ``"true"``/``"1"`` and ``"false"``/``"0"``
    (case-insensitive, trimmed) map to the obvious bool; any other numeric value
    is nonzero-truthy. Anything else returns ``None`` so the option is omitted
    rather than sent with a type the strict v1 schema would reject.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1"):
            return True
        if normalized in ("false", "0"):
            return False
        return None
    if isinstance(value, (int, float)):
        return value != 0
    return None


def _coerce_str(value: Any) -> Optional[str]:
    """Accept only ``str`` (the backend's ``product_tour_id`` is ``Option<String>``)."""
    return value if isinstance(value, str) else None


# Sentinel properties lifted into the typed `options` object: legacy property
# key, the backend's field name, and the coercer enforcing its strict type
# (wrong JSON types fail deserialization of the whole batch, so a value that
# won't coerce is omitted). The coercer is stored directly to keep the dispatch
# type-checked rather than keyed by a stringly-typed name.
_OPTION_SENTINELS: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
    ("$cookieless_mode", "cookieless_mode", _coerce_bool),
    ("$ignore_sent_at", "disable_skew_correction", _coerce_bool),
    ("$product_tour_id", "product_tour_id", _coerce_str),
    ("$process_person_profile", "process_person_profile", _coerce_bool),
)


def _v1_timestamp(timestamp: Any) -> str:
    """Return a UTC RFC3339 timestamp string.

    Messages off the queue already carry a UTC ISO-8601 string (``_enqueue``
    normalizes canonical datetimes), so that is passed through. A ``datetime``
    is normalized to UTC and serialized; a missing value defaults to now in UTC.
    The v1 server parses strictly with ``DateTime::parse_from_rfc3339`` and
    rejects naive timestamps.
    """
    if timestamp is None:
        return datetime.now(timezone.utc).isoformat()
    return _normalize_timestamp(timestamp)


def _to_v1_event(msg: dict) -> dict:
    """Transform a legacy-shaped queued message into a v1 wire event.

    Pure: the input ``msg`` is not mutated (a fresh ``properties`` dict is
    built), so it remains safe to keep the original for retries or callbacks.
    """
    properties = dict(msg.get("properties") or {})

    # Relocate top-level $set/$set_once into properties; v1 has no top-level
    # form. On the unusual collision where properties already carries the key,
    # the properties value wins.
    for key in _RELOCATE_TO_PROPERTIES:
        top_val = msg.get(key)
        if top_val is None:
            continue
        existing = properties.get(key)
        if isinstance(top_val, dict) and isinstance(existing, dict):
            properties[key] = {**top_val, **existing}
        elif key not in properties:
            properties[key] = top_val

    for key in _STRIP_FROM_PROPERTIES:
        properties.pop(key, None)

    options: dict[str, Any] = {}
    for prop_key, wire_key, coercer in _OPTION_SENTINELS:
        if prop_key not in properties:
            continue
        # Always removed from properties — these sentinels must never reach v1
        # backend properties — but only emitted as an option when coercible.
        coerced = coercer(properties.pop(prop_key))
        if coerced is not None:
            options[wire_key] = coerced

    top_level: dict[str, str] = {}
    for prop_key, field_name in _TOPLEVEL_SENTINELS:
        if prop_key not in properties:
            continue
        coerced_str = _coerce_str(properties.pop(prop_key))
        if coerced_str is not None:
            top_level[field_name] = coerced_str

    event = {
        "event": msg["event"],
        "uuid": msg["uuid"],
        "distinct_id": msg["distinct_id"],
        "timestamp": _v1_timestamp(msg.get("timestamp")),
        # Always a dict so it serializes as "{}" rather than null when empty.
        "options": options,
        "properties": properties,
    }
    event.update(top_level)
    return event


def _build_v1_batch_body(
    events: list[dict],
    historical_migration: bool = False,
    created_at: Optional[str] = None,
) -> dict:
    """Assemble the v1 batch envelope.

    Carries no ``api_key`` (Bearer auth) and no ``sent_at``.
    ``historical_migration`` is omitted when False (the server defaults it).
    ``created_at`` defaults to now in UTC; :func:`_send_v1_batch` passes a value
    hoisted once so it stays stable across retry attempts.
    """
    body: dict[str, Any] = {
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "batch": events,
    }
    if historical_migration:
        body["historical_migration"] = True
    return body
