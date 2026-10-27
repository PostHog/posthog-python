"""Metrics autocapture (``metrics={"autocapture": True}``) — alpha.

HTTP, database and runtime metrics from the official OpenTelemetry
instrumentations, exported to PostHog's ``/i/v1/metrics`` with no
instrumentation code in the app.

The OpenTelemetry packages come from the ``posthog[metrics]`` extra and are
imported only when autocapture starts. Library instrumentors (requests, Django,
psycopg, ...) are found through the ``opentelemetry_instrumentor`` entry points,
the same way ``opentelemetry-instrument`` finds them.

The meter and tracer providers are private: nothing is set on the OpenTelemetry
globals, so an app that adds its own OpenTelemetry setup later is not changed.
Spans are only used to derive database metrics and are never exported.
"""

import importlib
import logging
import os
import re
import threading
from importlib.metadata import entry_points
from typing import Any, Callable, Dict, List, Optional

from .version import VERSION

try:
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessorBase
except ImportError:  # pragma: no cover - the processor is only built once OTel imports
    _SpanProcessorBase = object

log = logging.getLogger("posthog")

_import_module = importlib.import_module

_INSTALL_HINT = 'pip install "posthog[metrics]" && opentelemetry-bootstrap -a install'

_HTTP_CLIENT_INSTRUMENTORS = (
    "requests",
    "urllib",
    "urllib3",
    "httpx",
    "aiohttp-client",
)
_HTTP_SERVER_INSTRUMENTORS = (
    "django",
    "flask",
    "fastapi",
    "starlette",
    "falcon",
    "pyramid",
    "tornado",
    "aiohttp-server",
)
_DB_DRIVER_INSTRUMENTORS = (
    "psycopg2",
    "psycopg",
    "asyncpg",
    "pymysql",
    "mysql",
    "mysqlclient",
    "sqlite3",
    "pymongo",
    "redis",
    "pymssql",
    "cassandra",
)
# SQLAlchemy adds connection pool metrics. Its spans wrap a driver span, so they
# are not turned into operation metrics: that would count one query twice.
_DB_INSTRUMENTORS = _DB_DRIVER_INSTRUMENTORS + ("sqlalchemy",)
_DERIVED_DB_METRIC_SCOPES = frozenset(
    f"opentelemetry.instrumentation.{name}" for name in _DB_DRIVER_INSTRUMENTORS
)

# Process-level metrics only; the host-wide system.* metrics are left out.
_RUNTIME_CONFIG: Dict[str, Optional[List[str]]] = {
    "process.cpu.time": ["user", "system"],
    "process.cpu.utilization": ["user", "system"],
    "process.memory.usage": None,
    "process.memory.virtual": None,
    "process.open_file_descriptor.count": None,
    "process.thread.count": None,
    "process.context_switches": ["involuntary", "voluntary"],
    "cpython.gc.collections": None,
    "cpython.gc.collected_objects": None,
    "cpython.gc.uncollectable_objects": None,
}

_DB_OPERATION_DURATION = "db.client.operation.duration"
# OpenTelemetry semantic convention advice for `db.client.operation.duration`.
_DB_DURATION_BUCKETS = [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10]
# The leading keyword (`SELECT`, `GET`) is bounded; the rest of a statement is not.
_STATEMENT_KEYWORD = re.compile(r"^\s*([A-Za-z]{2,20})\b")

_lock = threading.Lock()
_active: Optional["MetricsAutocapture"] = None
_warned_about_missing_packages = False


def _first_string(attributes: Any, keys: tuple) -> Optional[str]:
    for key in keys:
        value = attributes.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class DbSpanMetricsProcessor(_SpanProcessorBase):
    """Turns driver-level database client spans into a
    ``db.client.operation.duration`` histogram.

    Only bounded attributes are kept: never the statement, which holds values,
    and never anything per-user.
    """

    def __init__(self, meter):
        try:
            self._histogram = meter.create_histogram(
                _DB_OPERATION_DURATION,
                unit="s",
                description="Duration of database client operations.",
                explicit_bucket_boundaries_advisory=_DB_DURATION_BUCKETS,
            )
        except TypeError:
            # opentelemetry-api before 1.23 has no bucket advice.
            self._histogram = meter.create_histogram(
                _DB_OPERATION_DURATION,
                unit="s",
                description="Duration of database client operations.",
            )

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        try:
            self._record(span)
        except Exception:  # never raise into the instrumented call
            log.debug(
                "Metrics autocapture failed to record a database span", exc_info=True
            )

    def _record(self, span):
        from opentelemetry.trace import SpanKind, StatusCode

        scope = getattr(span, "instrumentation_scope", None)
        if (
            span.kind != SpanKind.CLIENT
            or getattr(scope, "name", None) not in _DERIVED_DB_METRIC_SCOPES
        ):
            return
        attributes = span.attributes or {}
        system = _first_string(attributes, ("db.system.name", "db.system"))
        if not system or span.start_time is None or span.end_time is None:
            return
        metric_attributes: Dict[str, Any] = {"db.system.name": system}
        operation = _first_string(attributes, ("db.operation.name", "db.operation"))
        if operation is None:
            statement = _first_string(attributes, ("db.query.text", "db.statement"))
            match = _STATEMENT_KEYWORD.match(statement or "")
            operation = match.group(1).upper() if match else None
        collection = _first_string(
            attributes,
            (
                "db.collection.name",
                "db.mongodb.collection",
                "db.sql.table",
                "db.cassandra.table",
            ),
        )
        namespace = _first_string(attributes, ("db.namespace", "db.name"))
        address = _first_string(attributes, ("server.address", "net.peer.name"))
        port = attributes.get("server.port", attributes.get("net.peer.port"))
        if operation:
            metric_attributes["db.operation.name"] = operation
        if collection:
            metric_attributes["db.collection.name"] = collection
        if namespace:
            metric_attributes["db.namespace"] = namespace
        if address:
            metric_attributes["server.address"] = address
        if isinstance(port, int) and not isinstance(port, bool):
            metric_attributes["server.port"] = port
        if span.status.status_code == StatusCode.ERROR:
            metric_attributes["error.type"] = (
                _first_string(attributes, ("error.type",)) or "_OTHER"
            )
        self._histogram.record(
            (span.end_time - span.start_time) / 1e9, metric_attributes
        )

    def _on_ending(self, span):
        pass

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class MetricsAutocapture:
    """The running autocapture. ``shutdown()`` exports the last window and
    removes the instrumentation."""

    def __init__(self, meter_provider, tracer_provider, instrumentors):
        self._meter_provider = meter_provider
        self._tracer_provider = tracer_provider
        self._instrumentors = instrumentors

    def force_flush(self, timeout_millis: int = 10_000) -> None:
        self._meter_provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        global _active
        with _lock:
            if _active is not self:
                return
            _active = None
        for instrumentor in self._instrumentors:
            try:
                instrumentor.uninstrument()
            except Exception:
                log.debug(
                    "Metrics autocapture failed to uninstrument %s",
                    instrumentor,
                    exc_info=True,
                )
        for provider in (self._tracer_provider, self._meter_provider):
            try:
                provider.shutdown()
            except Exception:
                log.debug(
                    "Metrics autocapture failed to shut down %s",
                    provider,
                    exc_info=True,
                )


def _is_default_provider(provider: Any) -> bool:
    name = type(provider).__name__
    return name.startswith(("_Proxy", "Proxy", "NoOp", "_NoOp", "Default"))


def _instrumentor_names(areas: Dict[str, bool]) -> List[str]:
    names: List[str] = []
    if areas.get("http"):
        names.extend(_HTTP_CLIENT_INSTRUMENTORS + _HTTP_SERVER_INSTRUMENTORS)
    if areas.get("db"):
        names.extend(_DB_INSTRUMENTORS)
    if areas.get("runtime"):
        names.append("system_metrics")
    return names


def start_metrics_autocapture(
    *,
    host: str,
    api_key: str,
    areas: Dict[str, bool],
    service_name: Optional[str],
    resource_attributes: Dict[str, Any],
    is_enabled: Callable[[], bool],
    export_interval_seconds: Optional[float] = None,
    _metric_reader: Any = None,
) -> Optional[MetricsAutocapture]:
    """Starts metrics autocapture. Returns ``None``, after one warning, when the
    OpenTelemetry packages are missing, when OpenTelemetry is already set up in
    the process, or when autocapture is already running. Never raises."""
    global _active, _warned_about_missing_packages
    with _lock:
        if _active is not None:
            log.warning(
                "Metrics autocapture is already running in this process, so this client does not start it again."
            )
            return None
        try:
            metrics_api = _import_module("opentelemetry.metrics")
            trace_api = _import_module("opentelemetry.trace")
            sdk_metrics = _import_module("opentelemetry.sdk.metrics")
            sdk_metrics_export = _import_module("opentelemetry.sdk.metrics.export")
            sdk_trace = _import_module("opentelemetry.sdk.trace")
            sampling = _import_module("opentelemetry.sdk.trace.sampling")
            sdk_resources = _import_module("opentelemetry.sdk.resources")
            otlp = _import_module(
                "opentelemetry.exporter.otlp.proto.http.metric_exporter"
            )
            _import_module("opentelemetry.instrumentation")
        except ImportError as e:
            if not _warned_about_missing_packages:
                _warned_about_missing_packages = True
                log.warning(
                    "Metrics autocapture needs the OpenTelemetry packages. Install them with: %s (%s)",
                    _INSTALL_HINT,
                    e,
                )
            return None

        try:
            if not _is_default_provider(
                metrics_api.get_meter_provider()
            ) or not _is_default_provider(trace_api.get_tracer_provider()):
                log.warning(
                    "OpenTelemetry is already set up in this process, so metrics autocapture does not start. "
                    "Point your OTLP metrics exporter at PostHog instead: https://posthog.com/docs/metrics"
                )
                return None

            # Stable HTTP names (`http.server.request.duration`, seconds), the same
            # names posthog-node sends. Read once, when the first instrumentor loads.
            os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

            resource = sdk_resources.Resource.create(
                {
                    "service.name": service_name
                    or os.environ.get("OTEL_SERVICE_NAME")
                    or "unknown_service",
                    **resource_attributes,
                    "telemetry.distro.name": "posthog-python",
                    "telemetry.distro.version": VERSION,
                }
            )

            reader = _metric_reader
            if reader is None:
                exporter_base: Any = otlp.OTLPMetricExporter
                delta = sdk_metrics_export.AggregationTemporality.DELTA
                cumulative = sdk_metrics_export.AggregationTemporality.CUMULATIVE

                class _GatedExporter(exporter_base):
                    # The exporter still sends while the client is disabled unless it is gated here.
                    def export(self, metrics_data, timeout_millis=10_000, **kwargs):
                        if not is_enabled():
                            return sdk_metrics_export.MetricExportResult.SUCCESS
                        return super().export(
                            metrics_data, timeout_millis=timeout_millis, **kwargs
                        )

                reader = sdk_metrics_export.PeriodicExportingMetricReader(
                    _GatedExporter(
                        endpoint=f"{host}/i/v1/metrics",
                        headers={"Authorization": f"Bearer {api_key}"},
                        # Delta, like the `posthog.metrics` wire shape; values that go
                        # up and down stay cumulative.
                        preferred_temporality={
                            sdk_metrics.Counter: delta,
                            sdk_metrics.Histogram: delta,
                            sdk_metrics.ObservableCounter: delta,
                            sdk_metrics.UpDownCounter: cumulative,
                            sdk_metrics.ObservableUpDownCounter: cumulative,
                            sdk_metrics.ObservableGauge: cumulative,
                        },
                    ),
                    export_interval_millis=(export_interval_seconds or 10.0) * 1000,
                )
            meter_provider = sdk_metrics.MeterProvider(
                resource=resource, metric_readers=[reader]
            )

            sampler_base: Any = sampling.Sampler

            class _ClientSpansOnly(sampler_base):
                # Records only client spans, the ones a database call makes. Database
                # instrumentors set `db.system` after start, so the kind is all a
                # sampler can check. Server spans stay no-ops.
                def should_sample(
                    self,
                    parent_context,
                    trace_id,
                    name,
                    kind=None,
                    attributes=None,
                    links=None,
                    trace_state=None,
                ):
                    decision = (
                        sampling.Decision.RECORD_ONLY
                        if kind == trace_api.SpanKind.CLIENT
                        else sampling.Decision.DROP
                    )
                    return sampling.SamplingResult(decision)

                def get_description(self):
                    return "PostHogClientSpansOnly"

            tracer_provider = sdk_trace.TracerProvider(
                resource=resource, sampler=_ClientSpansOnly()
            )
            if areas.get("db"):
                tracer_provider.add_span_processor(
                    DbSpanMetricsProcessor(
                        meter_provider.get_meter("posthog.metrics_autocapture", VERSION)
                    )
                )

            excluded_urls = "^" + re.escape(host.rstrip("/")) + "(/|$)"
            wanted = set(_instrumentor_names(areas))
            instrumentors = []
            for entry_point in entry_points(group="opentelemetry_instrumentor"):
                if entry_point.name not in wanted:
                    continue
                try:
                    instrumentor_class = entry_point.load()
                    instrumentor = (
                        instrumentor_class(config=_RUNTIME_CONFIG)
                        if entry_point.name == "system_metrics"
                        else instrumentor_class()
                    )
                    if instrumentor.is_instrumented_by_opentelemetry:
                        # The app instrumented this library itself.
                        continue
                    kwargs: Dict[str, Any] = {
                        "tracer_provider": tracer_provider,
                        "meter_provider": meter_provider,
                    }
                    if entry_point.name in _HTTP_CLIENT_INSTRUMENTORS:
                        # PostHog's own uploads, including this exporter's, are not app traffic.
                        kwargs["excluded_urls"] = excluded_urls
                    instrumentor.instrument(**kwargs)
                    if instrumentor.is_instrumented_by_opentelemetry:
                        instrumentors.append(instrumentor)
                except Exception:
                    log.debug(
                        "Metrics autocapture skipped %s",
                        entry_point.name,
                        exc_info=True,
                    )

            _active = MetricsAutocapture(meter_provider, tracer_provider, instrumentors)
            return _active
        except Exception:
            log.exception("Metrics autocapture failed to start")
            return None
