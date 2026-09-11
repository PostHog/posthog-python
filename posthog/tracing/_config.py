"""Resolution of the ``traces={...}`` client option.

An unusable value falls back to its documented default with a warning.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from ._sanitize import attribute_key

log = logging.getLogger("posthog")

# OpenTelemetry's BatchSpanProcessor defaults; a full batch stays well under the
# ingestion service's default 2 MiB body limit.
DEFAULT_FLUSH_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_EXPORT_BATCH_SIZE = 512
DEFAULT_MAX_QUEUE_SIZE = 2048

DEFAULT_MAX_ATTRIBUTES_PER_SPAN = 128
DEFAULT_MAX_EVENTS_PER_SPAN = 128
MAX_ATTRIBUTES_PER_EVENT = 128
# OpenTelemetry leaves this unlimited, but one huge value gets the whole span
# dropped as too large. 8192 fits a deep stack trace.
DEFAULT_MAX_ATTRIBUTE_VALUE_LENGTH = 8192

# Well above realistic concurrency; production traces routinely exceed ten minutes.
DEFAULT_MAX_LIVE_SPANS = 10_000
DEFAULT_MAX_SPAN_AGE_SECONDS = 3600.0

# Resource attributes the server attributes spans by; they must be strings.
_RESOURCE_IDENTITY_KEYS = ("service.name", "service.version", "deployment.environment")


@dataclass(frozen=True)
class ResolvedTracesConfig:
    service_name: Optional[str] = None
    service_version: Optional[str] = None
    environment: Optional[str] = None
    resource_attributes: Dict[str, Any] = field(default_factory=dict)
    flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS
    max_export_batch_size: int = DEFAULT_MAX_EXPORT_BATCH_SIZE
    max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE
    max_live_spans: int = DEFAULT_MAX_LIVE_SPANS
    max_span_age: float = DEFAULT_MAX_SPAN_AGE_SECONDS
    max_attributes_per_span: int = DEFAULT_MAX_ATTRIBUTES_PER_SPAN
    max_events_per_span: int = DEFAULT_MAX_EVENTS_PER_SPAN
    max_attribute_value_length: int = DEFAULT_MAX_ATTRIBUTE_VALUE_LENGTH


def _positive_number(config: Mapping, key: str, default: float) -> float:
    value = config.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not value > 0
        # An infinite interval cannot be armed as a timer.
        or not math.isfinite(value)
    ):
        log.warning(
            "Ignoring traces %s %r: expected a positive number of seconds", key, value
        )
        return default
    return float(value)


def _positive_int(config: Mapping, key: str, default: int) -> int:
    value = config.get(key, default)
    is_integer = isinstance(value, int) or (
        isinstance(value, float) and value.is_integer()
    )
    if isinstance(value, bool) or not is_integer or not value >= 1:
        log.warning("Ignoring traces %s %r: expected a positive integer", key, value)
        return default
    return int(value)


def _optional_string(config: Mapping, key: str) -> Optional[str]:
    value = config.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        log.warning("Ignoring traces %s %r: expected a string", key, value)
        return None
    return value


def _usable_resource_attributes(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        log.warning(
            "Ignoring traces resource_attributes: expected a dict, got %s",
            type(value).__name__,
        )
        return {}
    attributes: Dict[str, Any] = {}
    try:
        keys = list(value.keys())
    except Exception:
        return {}
    for key in keys:
        try:
            item = value[key]
        except Exception:
            continue
        key_str = attribute_key(key)
        if key_str is None:
            continue
        if key_str in _RESOURCE_IDENTITY_KEYS and not isinstance(item, str):
            log.warning(
                "Ignoring traces resource attribute %s: expected a string", key_str
            )
            continue
        attributes[key_str] = item
    return attributes


def resolve_traces_config(
    config: Any, host_resource_attributes: Optional[Mapping[str, str]] = None
) -> ResolvedTracesConfig:
    """Validate the ``traces`` option, falling back to documented defaults per key.

    Host resource attributes (``os.name``, ``os.version``) merge first so user
    ``resource_attributes`` win; a string ``service.name`` / ``service.version``
    / ``deployment.environment`` there wins over the named fields.
    """
    if not isinstance(config, Mapping):
        if config is not None:
            log.warning(
                "Ignoring traces config: expected a dict, got %s", type(config).__name__
            )
        config = {}

    resource_attributes: Dict[str, Any] = dict(host_resource_attributes or {})
    resource_attributes.update(
        _usable_resource_attributes(config.get("resource_attributes"))
    )

    max_export_batch_size = _positive_int(
        config, "max_export_batch_size", DEFAULT_MAX_EXPORT_BATCH_SIZE
    )
    # The queue must hold at least one full batch, or the depth trigger never fires.
    max_queue_size = max(
        _positive_int(config, "max_queue_size", DEFAULT_MAX_QUEUE_SIZE),
        max_export_batch_size,
    )

    return ResolvedTracesConfig(
        service_name=resource_attributes.get("service.name")
        or _optional_string(config, "service_name"),
        service_version=resource_attributes.get("service.version")
        or _optional_string(config, "service_version"),
        environment=resource_attributes.get("deployment.environment")
        or _optional_string(config, "environment"),
        resource_attributes=resource_attributes,
        flush_interval=_positive_number(
            config, "flush_interval", DEFAULT_FLUSH_INTERVAL_SECONDS
        ),
        max_export_batch_size=max_export_batch_size,
        max_queue_size=max_queue_size,
        max_live_spans=_positive_int(config, "max_live_spans", DEFAULT_MAX_LIVE_SPANS),
        max_span_age=_positive_number(
            config, "max_span_age", DEFAULT_MAX_SPAN_AGE_SECONDS
        ),
        max_attributes_per_span=_positive_int(
            config, "max_attributes_per_span", DEFAULT_MAX_ATTRIBUTES_PER_SPAN
        ),
        max_events_per_span=_positive_int(
            config, "max_events_per_span", DEFAULT_MAX_EVENTS_PER_SPAN
        ),
        max_attribute_value_length=_positive_int(
            config, "max_attribute_value_length", DEFAULT_MAX_ATTRIBUTE_VALUE_LENGTH
        ),
    )
