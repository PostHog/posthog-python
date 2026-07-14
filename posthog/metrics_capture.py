"""Statsd-style pre-aggregating metrics client (`client.metrics`) — alpha.

Samples fold into per-series aggregates in memory (counts sum, gauges keep the
last value, histograms accumulate buckets) and flush as one OTLP/JSON data
point per series per window to ``/i/v1/metrics`` — a burst of 10k ``count()``
calls costs one data point on the wire. Sums and histograms use delta
temporality, so each data point stands alone and process restarts need no
cross-window state. Mirrors the ``posthog-js`` core implementation so every
SDK speaks the same wire shape.

Deliberately unlike event capture, no per-user context (distinct ID, session)
is attached: every attribute value creates a new series, and per-user series
are the canonical metrics-cardinality explosion.
"""

import json
import logging
import math
import threading
import time
from typing import Any, Callable, Optional, Union

import requests

from posthog.utils import remove_trailing_slash
from posthog.version import VERSION

log = logging.getLogger("posthog")

MetricAttributeValue = Union[str, int, float, bool]

# OpenTelemetry SDK default bucket boundaries — usable resolution for common
# latency/size ranges without per-metric configuration. Must match posthog-js.
DEFAULT_HISTOGRAM_BOUNDS = [
    0,
    5,
    10,
    25,
    50,
    75,
    100,
    250,
    500,
    750,
    1000,
    2500,
    5000,
    7500,
    10000,
]

_OTLP_TEMPORALITY_DELTA = 1
_DEFAULT_FLUSH_INTERVAL_SECONDS = 10.0
_DEFAULT_MAX_SERIES_PER_FLUSH = 1000
_SCOPE_NAME = "posthog-python"


def _to_otlp_any_value(value: Any) -> dict:
    # bool before int: Python bool is an int subclass and must not encode as intValue.
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        # proto3 JSON has no representation for non-finite floats; keep the
        # human-readable signal as a string regardless of downstream parser.
        if not math.isfinite(value):
            return {"stringValue": str(value)}
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_to_otlp_any_value(v) for v in value]}}
    try:
        return {"stringValue": json.dumps(value)}
    except (TypeError, ValueError):
        return {"stringValue": str(value)}


def _to_otlp_key_value_list(attributes: dict) -> list:
    return [
        {"key": key, "value": _to_otlp_any_value(value)}
        for key, value in attributes.items()
        if value is not None
    ]


def _ms_to_unix_nano(ms: int) -> str:
    # OTLP requires nanoseconds as a decimal string (uint64).
    return f"{ms}000000"


def _bucket_index_for(value: float, bounds: list) -> int:
    for i, bound in enumerate(bounds):
        if value <= bound:
            return i
    return len(bounds)


class _SeriesState:
    __slots__ = (
        "name",
        "type",
        "unit",
        "attributes",
        "window_start_ms",
        "total",
        "last",
        "hist",
    )

    def __init__(
        self,
        name: str,
        metric_type: str,
        unit: Optional[str],
        attributes: Optional[dict],
    ):
        self.name = name
        self.type = metric_type
        self.unit = unit
        # Snapshot: the series key was computed from these values, so a caller
        # mutating the dict after capture must not change the stored series.
        self.attributes = dict(attributes) if attributes else None
        self.window_start_ms = int(time.time() * 1000)
        self.total: Optional[float] = None
        self.last: Optional[float] = None
        self.hist: Optional[dict] = None


class PostHogMetrics:
    """The ``client.metrics`` API: ``count``, ``gauge``, ``histogram``, ``flush``.

    Thread-safe; safe to call from hot paths. Configure via the ``metrics``
    client option::

        client = Client("phc_...", metrics={"service_name": "billing-worker"})
        client.metrics.count("invoices.processed", 1, attributes={"plan": "pro"})
        client.metrics.gauge("queue.depth", 42)
        client.metrics.histogram("job.duration", 187, unit="ms")
    """

    def __init__(self, client, config: Optional[dict] = None):
        self._client = client
        config = config or {}
        resource_attributes = config.get("resource_attributes") or {}
        self._service_name: Optional[str] = resource_attributes.get(
            "service.name"
        ) or config.get("service_name")
        self._service_version: Optional[str] = resource_attributes.get(
            "service.version"
        ) or config.get("service_version")
        self._environment: Optional[str] = resource_attributes.get(
            "deployment.environment"
        ) or config.get("environment")
        self._resource_attributes: dict = resource_attributes
        self._flush_interval: float = config.get(
            "flush_interval", _DEFAULT_FLUSH_INTERVAL_SECONDS
        )
        self._max_series_per_flush: int = config.get(
            "max_series_per_flush", _DEFAULT_MAX_SERIES_PER_FLUSH
        )
        self._before_send: Optional[Callable] = config.get("before_send")

        self._lock = threading.RLock()
        # Serializes flushes so a manual flush() can't race a timer flush for the same window.
        self._flush_lock = threading.Lock()
        self._series: dict = {}
        self._flush_timer: Optional[threading.Timer] = None
        self._series_cap_warned = False
        self._type_by_name: dict = {}
        self._type_collision_warned: set = set()

    def count(
        self,
        name: str,
        value: float = 1,
        unit: Optional[str] = None,
        attributes: Optional[dict] = None,
    ) -> None:
        """Record an increment for a monotonic counter (things that only go up)."""
        self._capture("count", name, value, unit, attributes)

    def gauge(
        self,
        name: str,
        value: float,
        unit: Optional[str] = None,
        attributes: Optional[dict] = None,
    ) -> None:
        """Record the current value of something that goes up and down."""
        self._capture("gauge", name, value, unit, attributes)

    def histogram(
        self,
        name: str,
        value: float,
        unit: Optional[str] = None,
        attributes: Optional[dict] = None,
    ) -> None:
        """Record one observation of a distribution (durations, sizes)."""
        self._capture("histogram", name, value, unit, attributes)

    def flush(self) -> None:
        """Sends everything aggregated so far without waiting for the flush interval."""
        with self._flush_lock:
            self._do_flush()

    def reset(self) -> None:
        """Clears the flush timer and drops the current window."""
        with self._lock:
            self._clear_flush_timer()
            self._series = {}
            self._series_cap_warned = False
            self._type_by_name = {}
            self._type_collision_warned = set()

    def _capture(
        self,
        metric_type: str,
        name: str,
        value: float,
        unit: Optional[str],
        attributes: Optional[dict],
    ) -> None:
        if getattr(self._client, "disabled", False):
            return

        sample = {
            "name": name,
            "type": metric_type,
            "value": value,
            "unit": unit,
            "attributes": attributes,
        }
        if self._before_send is not None:
            try:
                filtered = self._before_send(sample)
            except Exception as e:
                log.error("Error in metrics before_send: %s", e)
                return
            if not filtered:
                return
            sample = filtered
            name = sample.get("name")
            metric_type = sample.get("type", metric_type)
            value = sample.get("value")
            unit = sample.get("unit")
            attributes = sample.get("attributes")

        if not name or not isinstance(name, str):
            log.warning("Dropping metric with empty name")
            return
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            log.warning("Dropping metric '%s': value must be a finite number", name)
            return
        if metric_type == "count" and value < 0:
            log.warning(
                "Dropping count '%s': counters are monotonic, value must be >= 0", name
            )
            return

        attrs_key = tuple(sorted(attributes.items())) if attributes else ()
        key = (metric_type, name, unit or "", attrs_key)

        with self._lock:
            seen_type = self._type_by_name.get(name)
            if seen_type is None:
                self._type_by_name[name] = metric_type
            elif seen_type != metric_type and name not in self._type_collision_warned:
                self._type_collision_warned.add(name)
                log.warning(
                    "Metric name '%s' is already used as a %s; recording it as a %s too will blend "
                    "both series in charts. Use a distinct name.",
                    name,
                    seen_type,
                    metric_type,
                )

            state = self._series.get(key)
            if state is None:
                if len(self._series) >= self._max_series_per_flush:
                    if not self._series_cap_warned:
                        self._series_cap_warned = True
                        log.warning(
                            "Metric series cap reached (%s per flush window); dropping new series "
                            "until the next flush. Reduce attribute cardinality.",
                            self._max_series_per_flush,
                        )
                    return
                state = _SeriesState(name, metric_type, unit, attributes)
                self._series[key] = state

            self._fold(state, float(value))
            self._arm_flush_timer()

    def _fold(self, state: _SeriesState, value: float) -> None:
        if state.type == "count":
            state.total = (state.total or 0.0) + value
        elif state.type == "gauge":
            state.last = value
        else:
            hist = state.hist
            if hist is None:
                hist = state.hist = {
                    "count": 0,
                    "sum": 0.0,
                    "min": value,
                    "max": value,
                    "bucket_counts": [0] * (len(DEFAULT_HISTOGRAM_BOUNDS) + 1),
                }
            hist["count"] += 1
            hist["sum"] += value
            hist["min"] = min(hist["min"], value)
            hist["max"] = max(hist["max"], value)
            hist["bucket_counts"][
                _bucket_index_for(value, DEFAULT_HISTOGRAM_BOUNDS)
            ] += 1

    def _arm_flush_timer(self) -> None:
        if self._flush_timer is not None:
            return
        timer = threading.Timer(self._flush_interval, self._timer_flush)
        timer.daemon = True
        self._flush_timer = timer
        timer.start()

    def _timer_flush(self) -> None:
        with self._lock:
            self._flush_timer = None
        try:
            self.flush()
        except Exception as e:
            log.error("Metrics flush failed: %s", e)

    def _clear_flush_timer(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None

    def _do_flush(self) -> None:
        # Snapshot and reset the window under the lock; send outside it so
        # captures during the request fold into a fresh window.
        with self._lock:
            if not self._series:
                return
            window = self._series
            self._series = {}
            self._series_cap_warned = False
            self._type_by_name = {}
            self._type_collision_warned = set()

        payload = self._build_payload(window)
        outcome = self._send(payload)
        if outcome == "retry-later":
            # Transient failure: merge the unsent window back so the data rides
            # the next flush instead of being lost — and re-arm the timer, since
            # with no new captures nothing else would schedule that flush.
            with self._lock:
                self._merge_window_back(window)
                self._arm_flush_timer()
        elif outcome == "too-large":
            log.warning("Metrics batch exceeded the server size limit and was dropped")

    def _send(self, payload: dict) -> str:
        url = "{}/i/v1/metrics?token={}".format(
            remove_trailing_slash(self._client.host), self._client.api_key
        )
        try:
            response = requests.post(
                url, json=payload, timeout=getattr(self._client, "timeout", 15)
            )
        except requests.RequestException:
            return "retry-later"
        if response.status_code < 300:
            return "ok"
        if response.status_code == 413:
            return "too-large"
        if response.status_code >= 500 or response.status_code == 429:
            return "retry-later"
        log.error("Failed to send metrics batch: HTTP %s", response.status_code)
        return "fatal"

    def _merge_window_back(self, window: dict) -> None:
        for key, old in window.items():
            current = self._series.get(key)
            if current is None:
                self._series[key] = old
                continue
            current.window_start_ms = min(current.window_start_ms, old.window_start_ms)
            if current.type == "count":
                current.total = (current.total or 0.0) + (old.total or 0.0)
            elif current.type == "histogram" and old.hist:
                if current.hist is None:
                    current.hist = old.hist
                else:
                    current.hist["count"] += old.hist["count"]
                    current.hist["sum"] += old.hist["sum"]
                    current.hist["min"] = min(current.hist["min"], old.hist["min"])
                    current.hist["max"] = max(current.hist["max"], old.hist["max"])
                    for i, count in enumerate(old.hist["bucket_counts"]):
                        current.hist["bucket_counts"][i] += count
            # Gauge: the live window's value is newer — keep it.

    def _build_payload(self, window: dict) -> dict:
        # User resource attributes first, SDK-controlled keys layered on top so
        # a stray user key can't clobber attribution.
        resource_attributes = dict(self._resource_attributes)
        resource_attributes["service.name"] = self._service_name or "unknown_service"
        if self._environment:
            resource_attributes["deployment.environment"] = self._environment
        if self._service_version:
            resource_attributes["service.version"] = self._service_version
        resource_attributes["telemetry.sdk.name"] = _SCOPE_NAME
        resource_attributes["telemetry.sdk.version"] = VERSION

        return {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": _to_otlp_key_value_list(resource_attributes)
                    },
                    "scopeMetrics": [
                        {
                            "scope": {"name": _SCOPE_NAME, "version": VERSION},
                            "metrics": self._build_metrics(window),
                        }
                    ],
                }
            ]
        }

    def _build_metrics(self, window: dict) -> list:
        # One OTLP metric entry per (type, name, unit), one data point per attribute set.
        now_nano = _ms_to_unix_nano(int(time.time() * 1000))
        by_metric: dict = {}

        for state in window.values():
            metric_key = (state.type, state.name, state.unit or "")
            metric = by_metric.get(metric_key)
            if metric is None:
                metric = {"name": state.name}
                if state.unit:
                    metric["unit"] = state.unit
                if state.type == "count":
                    metric["sum"] = {
                        "aggregationTemporality": _OTLP_TEMPORALITY_DELTA,
                        "isMonotonic": True,
                        "dataPoints": [],
                    }
                elif state.type == "gauge":
                    metric["gauge"] = {"dataPoints": []}
                else:
                    metric["histogram"] = {
                        "aggregationTemporality": _OTLP_TEMPORALITY_DELTA,
                        "dataPoints": [],
                    }
                by_metric[metric_key] = metric

            attributes = _to_otlp_key_value_list(state.attributes or {})
            start_nano = _ms_to_unix_nano(state.window_start_ms)

            if state.type == "count":
                metric["sum"]["dataPoints"].append(
                    {
                        "attributes": attributes,
                        "startTimeUnixNano": start_nano,
                        "timeUnixNano": now_nano,
                        "asDouble": state.total or 0.0,
                    }
                )
            elif state.type == "gauge":
                metric["gauge"]["dataPoints"].append(
                    {
                        "attributes": attributes,
                        "timeUnixNano": now_nano,
                        "asDouble": state.last or 0.0,
                    }
                )
            elif state.hist is not None:
                # Encoding pinned by the ingest's JSON deserializer: nano timestamps are decimal
                # strings, but count/bucketCounts are plain JSON numbers — string-encoded u64s in
                # those fields are silently dropped upstream (opentelemetry-rust#3328).
                metric["histogram"]["dataPoints"].append(
                    {
                        "attributes": attributes,
                        "startTimeUnixNano": start_nano,
                        "timeUnixNano": now_nano,
                        "count": state.hist["count"],
                        "sum": state.hist["sum"],
                        "min": state.hist["min"],
                        "max": state.hist["max"],
                        "bucketCounts": state.hist["bucket_counts"],
                        "explicitBounds": DEFAULT_HISTOGRAM_BOUNDS,
                    }
                )

        return list(by_metric.values())
