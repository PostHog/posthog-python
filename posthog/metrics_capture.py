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

Delivery is at-least-once: a request that succeeds server-side but fails
client-side (e.g. a read timeout) is retried with the next window, which can
double-count that window's deltas. Failed flushes retry with exponential
backoff capped at ``_MAX_RETRY_BACKOFF_MULTIPLIER`` times the flush interval
(the policy the shared JS logs implementation uses); the window is dropped
loudly once ``_MAX_CONSECUTIVE_SEND_FAILURES`` consecutive flushes have failed.
"""

import copy
import gzip
import json
import logging
import math
import os
import threading
import time
from typing import Any, Callable, Optional, Union
from urllib.parse import quote

import requests

from posthog.request import _get_session
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
_VALID_METRIC_TYPES = ("count", "gauge", "histogram")
# Consecutive failed flushes before the buffered window is dropped (loudly) — bounds
# memory and payload growth against a permanently unreachable endpoint. The series
# cap already bounds the buffered window, and backoff spaces the attempts out, so
# the budget covers a real outage (~21 min at the default 10s interval).
_MAX_CONSECUTIVE_SEND_FAILURES = 8
# Retry delays grow 2x per consecutive failure, capped at this multiple of the
# flush interval — the same ceiling the shared JS logs implementation uses.
_MAX_RETRY_BACKOFF_MULTIPLIER = 64
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
        # proto3 JSON has no representation for non-finite floats; encode the proto3
        # literal strings (not Python's "inf"/"nan") so both SDKs emit identical bytes.
        if not math.isfinite(value):
            if math.isnan(value):
                return {"stringValue": "NaN"}
            return {"stringValue": "Infinity" if value > 0 else "-Infinity"}
        # Integral floats encode as intValue, matching the JS Number.isInteger branch.
        if value.is_integer():
            return {"intValue": int(value)}
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
    # str(key): OTLP KeyValue.key is a string field — strict decoders reject numeric
    # keys — and the series identity already stringifies keys the same way.
    return [
        {"key": str(key), "value": _to_otlp_any_value(value)}
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


def _series_key(
    metric_type: str, name: str, unit: Optional[str], attributes: Optional[dict]
) -> str:
    """Canonical, total series identity: JSON-encoded like the JS core's seriesKey, so any
    attribute value the encoder accepts (lists, dicts, mixed keys) produces a hashable key,
    and bool/int values stay distinct (json encodes true vs 1)."""
    attrs_part = ""
    if attributes:
        items = sorted(
            ((str(k), v) for k, v in attributes.items()), key=lambda kv: kv[0]
        )
        attrs_part = ",".join(
            f"{json.dumps(k)}:{json.dumps(v, sort_keys=True, default=str)}"
            for k, v in items
        )
    return "\x00".join((metric_type, name, unit or "", attrs_part))


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
        # Deep snapshot: the series key was computed from these values, so a caller
        # mutating the dict — or a nested list/dict value — after capture must not
        # change the stored series.
        if attributes:
            try:
                self.attributes: Optional[dict] = copy.deepcopy(attributes)
            except Exception:
                # Un-copyable exotic values: a shallow snapshot still isolates the
                # top-level dict, and the encoder stringifies whatever remains.
                self.attributes = dict(attributes)
        else:
            self.attributes = None
        self.window_start_ms = int(time.time() * 1000)
        self.total: Optional[float] = None
        self.last: Optional[float] = None
        self.hist: Optional[dict] = None


class PostHogMetrics:
    """The ``client.metrics`` API: ``count``, ``gauge``, ``histogram``, ``flush``.

    Thread-safe; safe to call from hot paths. Configure via the ``metrics``
    client option (``flush_interval`` is in seconds, matching the client's own
    ``flush_interval`` — unlike posthog-js, whose ``flushIntervalMs`` is milliseconds)::

        client = Client("phc_...", metrics={"service_name": "billing-worker"})
        client.metrics.count("invoices.processed", 1, attributes={"plan": "pro"})
        client.metrics.gauge("queue.depth", 42)
        client.metrics.histogram("job.duration", 187, unit="ms")
    """

    def __init__(self, client, config: Optional[dict] = None):
        self._client = client
        # client.metrics sits outside the client's no-throw guards, so invalid nested
        # config must degrade to defaults (with a warning) instead of raising into
        # the host application from the first metrics.count() call. The Any-typed
        # local keeps the runtime defense visible to mypy despite the annotation.
        raw_config: Any = config
        if not isinstance(raw_config, dict):
            if raw_config is not None:
                log.warning(
                    "Ignoring metrics config: expected a dict, got %s",
                    type(raw_config).__name__,
                )
            raw_config = {}
        config = raw_config
        resource_attributes = config.get("resource_attributes")
        if not isinstance(resource_attributes, dict):
            if resource_attributes is not None:
                log.warning(
                    "Ignoring metrics resource_attributes: expected a dict, got %s",
                    type(resource_attributes).__name__,
                )
            resource_attributes = {}
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
        flush_interval = config.get("flush_interval", _DEFAULT_FLUSH_INTERVAL_SECONDS)
        if (
            not isinstance(flush_interval, (int, float))
            or isinstance(flush_interval, bool)
            or not flush_interval > 0
        ):
            log.warning(
                "Ignoring metrics flush_interval %r: expected a positive number of seconds",
                flush_interval,
            )
            flush_interval = _DEFAULT_FLUSH_INTERVAL_SECONDS
        self._flush_interval: float = float(flush_interval)
        max_series = config.get("max_series_per_flush", _DEFAULT_MAX_SERIES_PER_FLUSH)
        if (
            not isinstance(max_series, int)
            or isinstance(max_series, bool)
            or max_series <= 0
        ):
            log.warning(
                "Ignoring metrics max_series_per_flush %r: expected a positive integer",
                max_series,
            )
            max_series = _DEFAULT_MAX_SERIES_PER_FLUSH
        self._max_series_per_flush: int = max_series
        before_send = config.get("before_send")
        if before_send is not None and not callable(before_send):
            log.warning("Ignoring metrics before_send: expected a callable")
            before_send = None
        self._before_send: Optional[Callable] = before_send

        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._consecutive_send_failures = 0
        self._capture_error_warned = False
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
        self._guarded_capture("count", name, value, unit, attributes)

    def gauge(
        self,
        name: str,
        value: float,
        unit: Optional[str] = None,
        attributes: Optional[dict] = None,
    ) -> None:
        """Record the current value of something that goes up and down."""
        self._guarded_capture("gauge", name, value, unit, attributes)

    def histogram(
        self,
        name: str,
        value: float,
        unit: Optional[str] = None,
        attributes: Optional[dict] = None,
    ) -> None:
        """Record one observation of a distribution (durations, sizes)."""
        self._guarded_capture("histogram", name, value, unit, attributes)

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

    def _guarded_capture(
        self,
        metric_type: str,
        name: str,
        value: float,
        unit: Optional[str],
        attributes: Optional[dict],
    ) -> None:
        # A telemetry call must never raise into the host application, whatever the input.
        try:
            self._capture(metric_type, name, value, unit, attributes)
        except Exception as e:
            if not self._capture_error_warned:
                self._capture_error_warned = True
                log.warning("Dropping metric '%s': %s", name, e)

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
            if not isinstance(filtered, dict):
                log.warning(
                    "Dropping metric: before_send must return the sample dict or a falsy value"
                )
                return
            sample_dict: dict[str, Any] = filtered
            # Defaults keep the static types closed; a hook that removed the field
            # produces a value the validation below drops.
            name = sample_dict.get("name", "")
            metric_type = sample_dict.get("type", metric_type)
            value = sample_dict.get("value", math.nan)
            unit = sample_dict.get("unit")
            attributes = sample_dict.get("attributes")

        if metric_type not in _VALID_METRIC_TYPES:
            log.warning(
                "Dropping metric '%s': unknown metric type '%s'", name, metric_type
            )
            return

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

        if attributes:
            # None-valued attributes are stripped from the wire, so strip them from the
            # series identity too — otherwise two indistinguishable data points emit.
            attributes = {k: v for k, v in attributes.items() if v is not None}
        key = _series_key(metric_type, name, unit, attributes)

        with self._lock:
            self._reset_after_fork_locked()

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

            # Bookkeeping only for admitted samples, so name-cardinality misuse (IDs in
            # metric names) can't grow this map past the series cap.
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

            self._fold(state, float(value))
            self._arm_flush_timer()

    def _reinit_after_fork(self) -> None:
        # Runs in a forked child (via the client's os.register_at_fork hook) before
        # user code. The inherited locks may be held by parent threads that do not
        # exist in the child, so replace them without ever acquiring them.
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._pid = os.getpid()
        self._drop_inherited_window()

    def _reset_after_fork_locked(self) -> None:
        # PID-guard fallback for platforms without os.register_at_fork: a forked child
        # inherits the parent's window and a timer handle whose thread does not exist
        # in the child — without this, the child never flushes (silent total loss) and
        # would duplicate the parent's samples if it ever did. Drop both.
        pid = os.getpid()
        if pid == self._pid:
            return
        self._pid = pid
        self._drop_inherited_window()

    def _drop_inherited_window(self) -> None:
        self._flush_timer = None
        self._series = {}
        self._series_cap_warned = False
        self._type_by_name = {}
        self._type_collision_warned = set()
        self._consecutive_send_failures = 0

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

    def _arm_flush_timer(self, delay: Optional[float] = None) -> None:
        if self._flush_timer is not None:
            return
        timer = threading.Timer(
            delay if delay is not None else self._flush_interval, self._timer_flush
        )
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

        # send=False mirrors event capture: recording succeeds locally, but
        # nothing is transmitted — the flushed window is discarded.
        if not getattr(self._client, "send", True):
            return

        payload = self._build_payload(window)
        outcome = self._send(payload)
        if outcome == "retry-later":
            with self._lock:
                self._consecutive_send_failures += 1
                if self._consecutive_send_failures >= _MAX_CONSECUTIVE_SEND_FAILURES:
                    # A persistently unreachable endpoint must not buffer forever: drop the
                    # window loudly instead of growing until a too-large drop loses more.
                    log.error(
                        "Dropping %s metric series after %s consecutive failed flushes — "
                        "check the endpoint and network configuration",
                        len(window),
                        self._consecutive_send_failures,
                    )
                    self._consecutive_send_failures = 0
                    return
                # Transient failure: merge the unsent window back so the data rides the
                # next flush instead of being lost — and re-arm the timer with capped
                # exponential backoff, so a real outage isn't hammered at the base
                # cadence. New captures see the armed timer and don't shorten it.
                delay = self._flush_interval * min(
                    2**self._consecutive_send_failures, _MAX_RETRY_BACKOFF_MULTIPLIER
                )
                log.warning(
                    "Metrics flush failed (attempt %s of %s); retrying in %.0fs",
                    self._consecutive_send_failures,
                    _MAX_CONSECUTIVE_SEND_FAILURES,
                    delay,
                )
                self._merge_window_back(window)
                self._arm_flush_timer(delay)
        elif outcome == "too-large":
            log.warning(
                "Metrics batch exceeded the server size limit and was dropped. "
                "Reduce series count or attribute cardinality."
            )
            with self._lock:
                self._consecutive_send_failures = 0
        else:
            with self._lock:
                self._consecutive_send_failures = 0

    def _send(self, payload: dict) -> str:
        url = "{}/i/v1/metrics?token={}".format(
            remove_trailing_slash(self._client.host),
            quote(self._client.api_key, safe=""),
        )
        body = gzip.compress(json.dumps(payload).encode("utf-8"))
        timeout = getattr(self._client, "timeout", 15) or 15
        try:
            # The shared pooled session: keepalive between the 10s flushes, fork-safe
            # reset, and the same adapter/proxy configuration as event capture.
            response = _get_session().post(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                timeout=timeout,
            )
        except requests.exceptions.RequestException:
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
        dropped = 0
        for key, old in window.items():
            current = self._series.get(key)
            if current is None:
                # The cap applies through merge-back too, or a long outage with attribute
                # churn grows the live window (and the retried payload) without bound.
                if len(self._series) >= self._max_series_per_flush:
                    dropped += 1
                    continue
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
        if dropped:
            log.warning(
                "Dropped %s unsent metric series while merging a failed flush back (series cap %s)",
                dropped,
                self._max_series_per_flush,
            )

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
