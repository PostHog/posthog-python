"""Event shaping for the capture v1 wire protocol.

Transforms a legacy-shaped queued message into a v1 wire event and assembles
the batch envelope. :mod:`posthog.capture_send` posts the result.

The v1 contract (see ``rust/capture/src/v1/analytics/types.rs``) differs from
the legacy queued-message shape in a few load-bearing ways that this module
encodes:

- An ``options`` object carries per-event processing options. Options the
  caller sets are sent as given, for PostHog to validate. Four legacy ``$``
  properties fill the matching option when the caller left it unset, and are
  always removed from ``properties``.
- ``$set``/``$set_once`` have no top-level form in v1; the server reads them
  from ``properties``. The legacy ``set()``/``set_once()`` builders emit them at
  the top level, so they are relocated into ``properties`` here.
- ``$lib``/``$lib_version`` are injected server-side from the required
  ``PostHog-Sdk-Info`` header and are stripped from v1 properties.
"""

import logging
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from posthog.utils import _normalize_timestamp
from posthog.utils import clean as _clean

log = logging.getLogger("posthog")

# Sentinel properties lifted to top-level string fields on the event.
_TOPLEVEL_SENTINELS: tuple[tuple[str, str], ...] = (
    ("$session_id", "session_id"),
    ("$window_id", "window_id"),
)

# Top-level legacy keys relocated into properties (v1 has no top-level form).
_RELOCATE_TO_PROPERTIES = ("$set", "$set_once")

# Properties dropped from v1 events (server injects them from PostHog-Sdk-Info).
_STRIP_FROM_PROPERTIES = ("$lib", "$lib_version")

# Legacy properties and the option each one fills. The order matches posthog-rs
# and posthog-go.
_LEGACY_OPTION_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("$cookieless_mode", "cookieless_mode"),
    ("$ignore_sent_at", "disable_skew_correction"),
    ("$product_tour_id", "product_tour_id"),
    ("$process_person_profile", "process_person_profile"),
)

# The uuid forms Go's uuid.Validate accepts. Python's UUID() also accepts
# misplaced hyphens and a bare "uuid:" prefix, which other SDKs reject.
_EVENT_UUID_PATTERN = re.compile(
    r"(?:urn:uuid:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}"
    r"|[0-9a-f]{32}",
    re.IGNORECASE,
)


def _canonical_event_uuid(value: Any) -> Optional[str]:
    """Return the canonical form of a caller's event uuid, or None if invalid.

    Capture keys per-event results by the canonical lowercase hyphenated form,
    so a uuid sent in any other form would never match its result.
    """
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str) or not _EVENT_UUID_PATTERN.fullmatch(value):
        return None
    return str(UUID(value.lower()))


def _event_options(value: Any) -> dict[str, Any]:
    """Return a copy of a caller's ``options``, or ``{}`` when it is not a dict."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        log.error(
            "options must be a dict, got %s. Sending the event without them.",
            type(value).__name__,
        )
        return {}
    return dict(value)


@dataclass(frozen=True)
class _EventDefaults:
    """Context, global and SDK-derived values for one event, highest layer first.

    They fill in after ``before_send``, so the hook never sees them, and the
    event's own values and the hook's changes always win.
    """

    property_layers: tuple[Mapping[str, Any], ...] = ()
    option_layers: tuple[Mapping[str, Any], ...] = ()
    property_allowlist: Optional[Collection[str]] = None


def _build_event_defaults(
    *,
    super_properties: Optional[Mapping[str, Any]],
    super_options: Any,
    release_id: Optional[str],
    context_properties: Optional[Mapping[str, Any]] = None,
    context_options: Optional[Mapping[str, Any]] = None,
    derived_options: Optional[Mapping[str, Any]] = None,
    property_allowlist: Optional[Collection[str]] = None,
) -> _EventDefaults:
    """Order the layers: context, then global, then values the SDK derives."""
    # An explicit `$release_id` in the event or in super properties wins over
    # the environment value.
    release = {"$release_id": release_id} if release_id is not None else {}
    return _EventDefaults(
        property_layers=(context_properties or {}, super_properties or {}, release),
        option_layers=(
            context_options or {},
            _event_options(super_options),
            derived_options or {},
        ),
        property_allowlist=property_allowlist,
    )


def _fill_event_defaults(
    msg: dict[str, Any], defaults: Optional[_EventDefaults]
) -> None:
    """Fill the keys an event left unset from ``defaults``, layer by layer.

    A property is unset only when its key is missing. An option is unset when
    it is missing or ``None``, the same rule hoisting uses.
    """
    if defaults is None:
        return
    allowlist = defaults.property_allowlist
    properties = msg.get("properties")
    if not isinstance(properties, dict):
        properties = {}
        msg["properties"] = properties
    for property_layer in defaults.property_layers:
        for key, value in property_layer.items():
            if key in properties or (allowlist is not None and key not in allowlist):
                continue
            properties[key] = _clean(value)
    options = _event_options(msg.get("options"))
    for option_layer in defaults.option_layers:
        for key, value in option_layer.items():
            if options.get(key) is None:
                options[key] = _clean(value)
    msg["options"] = options


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

    options = _event_options(msg.get("options"))
    for prop_key, option_key in _LEGACY_OPTION_PROPERTIES:
        if prop_key not in properties:
            continue
        legacy = properties.pop(prop_key)
        # A null option counts as unset, so the legacy value fills it.
        if options.get(option_key) is None:
            options[option_key] = legacy

    top_level: dict[str, str] = {}
    for prop_key, field_name in _TOPLEVEL_SENTINELS:
        if prop_key not in properties:
            continue
        # Always removed. A non-string value would fail the whole batch, so it
        # is dropped.
        value = properties.pop(prop_key)
        if isinstance(value, str):
            top_level[field_name] = value

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
