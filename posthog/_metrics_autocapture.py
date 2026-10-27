"""Metrics autocapture (``metrics={"autocapture": True}``) — alpha.

HTTP, database and runtime metrics from the official OpenTelemetry
instrumentations, exported to PostHog's ``/i/v1/metrics`` with no
instrumentation code in the app.

The OpenTelemetry packages come from the ``posthog[metrics]`` extra and are
imported only when autocapture starts. Library instrumentors (requests, Django,
psycopg, ...) are found through the ``opentelemetry_instrumentor`` entry points,
the same way ``opentelemetry-instrument`` finds them. An instrumentor changes the
library classes when it starts, so objects created before that, such as a Flask
or FastAPI app, are not measured: create the client before the web app. Django
is instrumented only when its settings are configured, as in ``AppConfig.ready()``.

The meter and tracer providers are private: nothing is set on the OpenTelemetry
globals, and the app's ``OTEL_EXPORTER_OTLP_*`` settings are not used. The
library instrumentors are process-wide singletons, though, so an app cannot
instrument a library again with its own OpenTelemetry setup while autocapture
instruments it. Apps with their own OpenTelemetry instrumentation should export
their metrics to PostHog and not turn on autocapture.

Spans are only used to derive database metrics and are never exported. The HTTP
instrumentors get no tracer, and the instrumentors that autocapture starts do not
propagate context, so no trace context or baggage goes to other services.
"""

import importlib
import logging
import os
import re
import sys
import threading
import weakref
from contextlib import contextmanager
from importlib.metadata import entry_points
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

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

# The OpenTelemetry SDK keeps every attribute set it has seen, also with delta
# temporality, and has no limit of its own. Some attributes, such as the
# `server.address` of an outgoing request, can come from user input. So each
# instrument keeps at most this many attribute sets. Other measurements go to one
# overflow series, as the OpenTelemetry cardinality limit specifies.
_MAX_ATTRIBUTE_SETS = 2000
_OVERFLOW_ATTRIBUTES = {"otel.metric.overflow": True}

_SEMCONV_OPT_IN = "OTEL_SEMCONV_STABILITY_OPT_IN"
# The app's OTLP settings (headers with other vendors' keys, endpoint, TLS files)
# are for its own collector. The exporter reads them when it is created.
_OTLP_ENVIRONMENT_PREFIXES = ("OTEL_EXPORTER_OTLP_", "OTEL_PYTHON_EXPORTER_OTLP_")
_EXPORT_TIMEOUT_SECONDS = 10.0
_DEFAULT_PORTS = {"http": 80, "https": 443}

# The `metrics.before_send` sample type of each instrument kind.
_SAMPLE_TYPES = {
    "create_counter": "count",
    "create_up_down_counter": "count",
    "create_histogram": "histogram",
    "create_gauge": "gauge",
    "create_observable_counter": "count",
    "create_observable_up_down_counter": "count",
    "create_observable_gauge": "gauge",
}

_lock = threading.Lock()
_active: Optional["MetricsAutocapture"] = None
_warned_about_missing_packages = False
# Flask apps created while autocapture runs. FlaskInstrumentor.uninstrument()
# restores only the Flask class, so shutdown uninstruments these apps and the
# next autocapture instruments them again.
_flask_apps: "weakref.WeakSet[Any]" = weakref.WeakSet()
# The `inject` functions that autocapture replaced in instrumentor modules,
# by module name.
_replaced_injects: Dict[str, Any] = {}


def _always_enabled() -> bool:
    return True


def _apply_before_send(
    before_send: Callable,
    name: str,
    metric_type: str,
    unit: str,
    value: Any,
    attributes: Any,
) -> Optional[Tuple[Any, Any]]:
    """Runs the ``metrics.before_send`` hook on one measurement, like the
    ``client.metrics`` API does. Returns the value and attributes to record, or
    ``None`` to drop it. An instrument's name, type and unit are fixed, so the
    hook can change only the value and the attributes."""
    sample = {
        "name": name,
        "type": metric_type,
        "value": value,
        "unit": unit,
        "attributes": dict(attributes) if attributes else {},
    }
    try:
        filtered = before_send(sample)
    except Exception as e:
        log.error("Error in metrics before_send: %s", e)
        return None
    if not filtered:
        return None
    if not isinstance(filtered, dict):
        log.warning(
            "Dropping metric: before_send must return the sample dict or a falsy value"
        )
        return None
    value = filtered.get("value")
    attributes = filtered.get("attributes")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isinstance(attributes, (dict, type(None)))
    ):
        log.warning(
            "Dropping metric '%s': before_send returned an invalid value or attributes",
            name,
        )
        return None
    return value, attributes


def _instrument_name_and_unit(args: tuple, kwargs: dict, unit_index: int):
    name = kwargs.get("name", args[0] if args else "")
    unit = kwargs.get("unit", args[unit_index] if len(args) > unit_index else "")
    return name, unit


class _BoundedInstrument:
    """A synchronous instrument that records only while autocapture is enabled,
    applies ``before_send``, and sends new attribute sets to the overflow series
    once it has seen ``limit`` of them."""

    def __init__(
        self,
        instrument: Any,
        limit: int,
        is_enabled: Callable[[], bool] = _always_enabled,
        before_send: Optional[Callable] = None,
        name: str = "",
        metric_type: str = "",
        unit: str = "",
        stopped: Optional[threading.Event] = None,
    ):
        self._instrument = instrument
        self._stopped = stopped
        self._limit = limit
        self._is_enabled = is_enabled
        self._before_send = before_send
        self._name = name
        self._metric_type = metric_type
        self._unit = unit
        # No lock: a race can only go over the limit by the number of threads.
        self._seen: set = set()

    def _bounded(self, attributes: Any) -> Any:
        try:
            key = frozenset(attributes.items()) if attributes else frozenset()
        except (AttributeError, TypeError):
            return attributes
        if key in self._seen:
            return attributes
        if len(self._seen) < self._limit:
            self._seen.add(key)
            return attributes
        return _OVERFLOW_ATTRIBUTES

    def _record(self, method: str, amount, attributes, args, kwargs):
        # Like `client.metrics`, nothing is recorded while the client is disabled,
        # so that period is not exported once the client is enabled again.
        # Instrumentation that shutdown leaves in place records nothing more.
        if (
            self._stopped is not None and self._stopped.is_set()
        ) or not self._is_enabled():
            return None
        if self._before_send is not None:
            measurement = _apply_before_send(
                self._before_send,
                self._name,
                self._metric_type,
                self._unit,
                amount,
                attributes,
            )
            if measurement is None:
                return None
            amount, attributes = measurement
        return getattr(self._instrument, method)(
            amount, self._bounded(attributes), *args, **kwargs
        )

    def add(self, amount, attributes=None, *args, **kwargs):
        return self._record("add", amount, attributes, args, kwargs)

    def record(self, amount, attributes=None, *args, **kwargs):
        return self._record("record", amount, attributes, args, kwargs)

    def set(self, amount, attributes=None, *args, **kwargs):
        return self._record("set", amount, attributes, args, kwargs)

    def __getattr__(self, name):
        return getattr(self._instrument, name)


class _BoundedMeter:
    def __init__(
        self,
        meter: Any,
        limit: int,
        is_enabled: Callable[[], bool] = _always_enabled,
        before_send: Optional[Callable] = None,
        stopped: Optional[threading.Event] = None,
    ):
        self._meter = meter
        self._limit = limit
        self._is_enabled = is_enabled
        self._before_send = before_send
        self._stopped = stopped

    def _bounded(self, factory: str, args, kwargs) -> _BoundedInstrument:
        name, unit = _instrument_name_and_unit(args, kwargs, unit_index=1)
        return _BoundedInstrument(
            getattr(self._meter, factory)(*args, **kwargs),
            self._limit,
            self._is_enabled,
            self._before_send,
            name,
            _SAMPLE_TYPES[factory],
            unit,
            self._stopped,
        )

    def _observable(self, factory: str, args, kwargs):
        # Observable instruments report from bounded callbacks (runtime metrics),
        # so only the enabled check and `before_send` apply to them. They still
        # report in the last collection of shutdown.
        name, unit = _instrument_name_and_unit(args, kwargs, unit_index=2)
        args = list(args)
        if len(args) > 1:
            args[1] = self._gated_callbacks(args[1], name, _SAMPLE_TYPES[factory], unit)
        elif "callbacks" in kwargs:
            kwargs["callbacks"] = self._gated_callbacks(
                kwargs["callbacks"], name, _SAMPLE_TYPES[factory], unit
            )
        return getattr(self._meter, factory)(*args, **kwargs)

    def _gated_callbacks(self, callbacks, name: str, metric_type: str, unit: str):
        if not callbacks:
            return callbacks
        is_enabled, before_send = self._is_enabled, self._before_send
        observation_class = _import_module("opentelemetry.metrics").Observation

        def gated(callback):
            def observe(options):
                if not is_enabled():
                    return []
                observations = callback(options)
                if before_send is None:
                    return observations
                kept = []
                for observation in observations or ():
                    measurement = _apply_before_send(
                        before_send,
                        name,
                        metric_type,
                        unit,
                        observation.value,
                        observation.attributes,
                    )
                    if measurement is not None:
                        kept.append(observation_class(*measurement))
                return kept

            return observe

        # Generator objects are also valid callbacks; they are passed on as they are.
        return [
            gated(callback) if callable(callback) else callback
            for callback in callbacks
        ]

    def create_counter(self, *args, **kwargs):
        return self._bounded("create_counter", args, kwargs)

    def create_up_down_counter(self, *args, **kwargs):
        return self._bounded("create_up_down_counter", args, kwargs)

    def create_histogram(self, *args, **kwargs):
        return self._bounded("create_histogram", args, kwargs)

    def create_gauge(self, *args, **kwargs):
        return self._bounded("create_gauge", args, kwargs)

    def create_observable_counter(self, *args, **kwargs):
        return self._observable("create_observable_counter", args, kwargs)

    def create_observable_up_down_counter(self, *args, **kwargs):
        return self._observable("create_observable_up_down_counter", args, kwargs)

    def create_observable_gauge(self, *args, **kwargs):
        return self._observable("create_observable_gauge", args, kwargs)

    def __getattr__(self, name):
        return getattr(self._meter, name)


class _BoundedMeterProvider:
    """The meter provider given to the instrumentors. Its instruments record only
    while autocapture is enabled, apply ``before_send``, and limit the number of
    attribute sets of each synchronous instrument."""

    def __init__(
        self,
        meter_provider: Any,
        limit: int,
        is_enabled: Callable[[], bool] = _always_enabled,
        before_send: Optional[Callable] = None,
        stopped: Optional[threading.Event] = None,
    ):
        self._meter_provider = meter_provider
        self._limit = limit
        self._is_enabled = is_enabled
        self._before_send = before_send
        self._stopped = stopped

    def get_meter(self, *args, **kwargs):
        return _BoundedMeter(
            self._meter_provider.get_meter(*args, **kwargs),
            self._limit,
            self._is_enabled,
            self._before_send,
            self._stopped,
        )

    def __getattr__(self, name):
        return getattr(self._meter_provider, name)


@contextmanager
def _stable_http_semconv() -> Iterator[None]:
    """Selects the stable HTTP names (`http.server.request.duration`, seconds),
    the same names posthog-node sends, while the instrumentors load.

    OpenTelemetry reads the opt-in once per process. If another instrumentor
    read it first, without an opt-in, the HTTP mode is set here, so the names
    do not depend on import order. The process environment is restored
    afterwards, so subprocesses and other code do not see the change.
    An opt-in that the app set is kept."""
    previous = os.environ.get(_SEMCONV_OPT_IN)
    if previous is None:
        os.environ[_SEMCONV_OPT_IN] = "http"
    try:
        try:
            semconv = _import_module("opentelemetry.instrumentation._semconv")
            stability = semconv._OpenTelemetrySemanticConventionStability
            stability._initialize()
            if previous is None:
                stability._OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING[
                    semconv._OpenTelemetryStabilitySignalType.HTTP
                ] = semconv._StabilityMode.HTTP
        except Exception:  # older instrumentation packages have no opt-in
            pass
        yield
    finally:
        if previous is None:
            os.environ.pop(_SEMCONV_OPT_IN, None)


@contextmanager
def _patched_environ(values: Dict[str, Optional[str]]) -> Iterator[None]:
    """Sets the environment variables (``None`` removes one) while OpenTelemetry
    reads them, then restores the process environment."""
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _posthog_url_pattern(host: str) -> str:
    """An ``excluded_urls`` pattern for the PostHog host, in every form the HTTP
    instrumentors see it: in any case, and with or without the default port."""
    parts = urlsplit(host)
    scheme = parts.scheme.lower()
    hostname = parts.hostname
    if not scheme or not hostname:
        pattern = "^" + re.escape(host) + "(?:[/?#]|$)"
    else:
        if ":" in hostname:
            hostname = f"[{hostname}]"
        default_port = _DEFAULT_PORTS.get(scheme)
        if parts.port is not None and parts.port != default_port:
            port = f":{parts.port}"
        else:
            port = f"(?::{default_port})?" if default_port else ""
        pattern = (
            "^(?i:"
            + re.escape(f"{scheme}://{hostname}")
            + ")"
            + port
            + re.escape(parts.path.rstrip("/"))
            + "(?:[/?#]|$)"
        )
    # The instrumentors split the excluded URLs at commas.
    return pattern.replace(",", r"\x2c")


def _excluded_urls(instrumentor_name: str, posthog_pattern: str) -> Tuple[str, str]:
    """The excluded URLs for an HTTP client instrumentor: the PostHog host, and
    the URLs that the app excludes in ``OTEL_PYTHON_*_EXCLUDED_URLS``. Returns
    the instrumentor's environment variable too, which httpx and aiohttp read
    in place of the ``excluded_urls`` argument."""
    variable = (
        f"OTEL_PYTHON_{instrumentor_name.upper().replace('-', '_')}_EXCLUDED_URLS"
    )
    excluded = os.environ.get(variable, os.environ.get("OTEL_PYTHON_EXCLUDED_URLS", ""))
    return variable, ",".join(url for url in (posthog_pattern, excluded) if url)


def _track_flask_apps(instrumentor_class: Any, providers: Dict[str, Any]) -> None:
    flask: Any = _import_module("flask")
    instrumented_flask = flask.Flask

    class _TrackedFlask(instrumented_flask):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # FlaskInstrumentor.uninstrument_app() needs `_teardown_request`, which
            # only instrument_app() sets. The instrumented class registers that
            # hook last.
            teardown_funcs = self.teardown_request_funcs.get(None)
            if not hasattr(self, "_teardown_request") and teardown_funcs:
                self._teardown_request = teardown_funcs[-1]
            self._posthog_wsgi_app = self.wsgi_app
            _flask_apps.add(self)

    flask.Flask = _TrackedFlask
    for app in list(_flask_apps):
        if not getattr(app, "_is_instrumented_by_opentelemetry", False):
            instrumentor_class.instrument_app(app, **providers)
            app._posthog_wsgi_app = app.wsgi_app


def _uninstrument_flask_apps() -> None:
    flask_instrumentation = sys.modules.get("opentelemetry.instrumentation.flask")
    if flask_instrumentation is None:
        return
    for app in list(_flask_apps):
        if not getattr(app, "_is_instrumented_by_opentelemetry", False):
            continue
        # uninstrument_app() restores the WSGI app saved when the app was
        # instrumented, which would remove middleware that the app added later,
        # such as authentication. Such an app keeps the instrumentation, which
        # records nothing after shutdown, and is not measured again.
        if getattr(app, "wsgi_app", None) is not getattr(
            app, "_posthog_wsgi_app", None
        ):
            log.debug(
                "Metrics autocapture left the instrumentation of %r in place", app
            )
            continue
        flask_instrumentation.FlaskInstrumentor.uninstrument_app(app)


def _django_settings_configured() -> bool:
    conf = sys.modules.get("django.conf")
    return bool(getattr(getattr(conf, "settings", None), "configured", False))


def _no_inject(*args: Any, **kwargs: Any) -> None:
    return None


def _disable_propagation(instrumentor_class: Any) -> None:
    """Replaces ``inject`` in the instrumentor's modules, which would send the
    current trace context and baggage to other services. The instrumentors call
    it through their module global, and autocapture owns these instrumentors
    while it runs."""
    inject = _import_module("opentelemetry.propagate").inject
    package = instrumentor_class.__module__
    if package.startswith("opentelemetry.instrumentation."):
        # Such as `opentelemetry.instrumentation.tornado`, with its client module.
        package = ".".join(package.split(".")[:3])
    for name, module in list(sys.modules.items()):
        if name != package and not name.startswith(package + "."):
            continue
        if getattr(module, "inject", None) is inject:
            _replaced_injects[name] = inject
            setattr(module, "inject", _no_inject)


def _restore_propagation() -> None:
    while _replaced_injects:
        name, inject = _replaced_injects.popitem()
        module = sys.modules.get(name)
        if module is not None and getattr(module, "inject", None) is _no_inject:
            setattr(module, "inject", inject)


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
        else:
            # Only the leading keyword is bounded, as for a statement.
            words = operation.split()
            operation = words[0] if words else None
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

    def __init__(
        self,
        meter_provider,
        tracer_provider,
        instrumentors,
        reader,
        stopped: Optional[threading.Event] = None,
    ):
        self._meter_provider = meter_provider
        self._tracer_provider = tracer_provider
        self._instrumentors = instrumentors
        self._reader = reader
        self._stopped = stopped if stopped is not None else threading.Event()
        self._flush_lock = threading.Lock()
        self._flush_thread: Optional[threading.Thread] = None
        self._flush_again = False

    def force_flush(self, timeout_millis: int = 10_000) -> None:
        self._meter_provider.force_flush(timeout_millis)

    def start_flush(self) -> Optional[Callable[[Optional[float]], None]]:
        """Starts an export of the current window. Returns a function that waits
        for it, at most the given seconds, or ``None`` when there is nothing to
        wait for.

        One thread exports at a time. A call made while it exports asks it for
        one more export, so the measurements recorded before the call are also
        sent, and callers with short timeouts do not start a thread each."""
        with self._flush_lock:
            flusher = self._flush_thread
            if flusher is not None:
                self._flush_again = True
                # From the export itself, waiting would never end.
                return None if flusher is threading.current_thread() else flusher.join
            flusher = threading.Thread(
                target=self._flush_until_done,
                name="posthog-metrics-autocapture-flush",
                daemon=True,
            )
            try:
                flusher.start()
            except RuntimeError:  # no new threads at interpreter shutdown
                return None
            self._flush_thread = flusher
            return flusher.join

    def _flush_until_done(self) -> None:
        while True:
            self._flush_quietly()
            with self._flush_lock:
                if not self._flush_again:
                    self._flush_thread = None
                    return
                self._flush_again = False

    def _flush_quietly(self) -> None:
        try:
            self._meter_provider.force_flush()
        except Exception:
            log.debug("Metrics autocapture failed to flush", exc_info=True)

    def _reinit_after_fork(self) -> None:
        # The instrumentation and the providers stay active in the child, and the
        # OpenTelemetry metric reader restarts its export thread there. The reader
        # does not replace its export lock, which the parent's export thread can
        # hold at fork time.
        if getattr(self._reader, "_export_lock", None) is not None:
            self._reader._export_lock = threading.Lock()
        # The parent's flush thread does not exist in the child.
        self._flush_lock = threading.Lock()
        self._flush_thread = None
        self._flush_again = False
        try:
            _replace_collection_locks(self._meter_provider)
        except Exception:
            log.debug(
                "Metrics autocapture failed to reset locks after fork", exc_info=True
            )
        # The runtime instrumentor keeps the process it was created in, so it
        # would report the parent process.
        for instrumentor in self._instrumentors:
            try:
                psutil = _import_module("psutil")
                if isinstance(getattr(instrumentor, "_proc", None), psutil.Process):
                    instrumentor._proc = psutil.Process(os.getpid())
            except Exception:
                log.debug(
                    "Metrics autocapture failed to update %s after fork",
                    instrumentor,
                    exc_info=True,
                )

    def shutdown(self) -> None:
        global _active
        with _lock:
            if _active is not self:
                return
            self._stopped.set()
            # Uninstrument before another client can start. It would skip the
            # libraries that are still instrumented, and then lose them here.
            _uninstrument(self._instrumentors)
            _active = None
        # The last export can take a while, so it runs outside the lock.
        _shut_down(self._tracer_provider, self._meter_provider)


def _uninstrument(instrumentors: List[Any]) -> None:
    for instrumentor in instrumentors:
        try:
            instrumentor.uninstrument()
        except Exception:
            log.debug(
                "Metrics autocapture failed to uninstrument %s",
                instrumentor,
                exc_info=True,
            )
    try:
        _uninstrument_flask_apps()
    except Exception:
        log.debug(
            "Metrics autocapture failed to uninstrument Flask apps", exc_info=True
        )
    _restore_propagation()


def _fresh_lock(owner: Any, name: str = "_lock") -> None:
    lock = getattr(owner, name, None)
    if lock is None:
        return
    rlock_type = type(threading.RLock())
    setattr(
        owner,
        name,
        threading.RLock() if isinstance(lock, rlock_type) else threading.Lock(),
    )


def _replace_collection_locks(meter_provider: Any) -> None:
    """Replaces the locks of the metrics SDK that a parent thread can hold at
    fork time, while it collects or records. The SDK replaces only the locks of
    the meter provider itself. In the child, no thread holds these locks."""
    for meter in list(getattr(meter_provider, "_meters", {}).values()):
        _fresh_lock(meter, "_instrument_registration_lock")
    consumer = getattr(meter_provider, "_measurement_consumer", None)
    if consumer is None:
        return
    _fresh_lock(consumer)
    for storage in list(getattr(consumer, "_reader_storages", {}).values()):
        _fresh_lock(storage)
        matches_by_instrument = getattr(
            storage, "_instrument_view_instrument_matches", {}
        )
        for matches in list(matches_by_instrument.values()):
            for match in matches:
                _fresh_lock(match)
                for aggregation in list(
                    getattr(match, "_attributes_aggregation", {}).values()
                ):
                    _fresh_lock(aggregation)
                    _fresh_lock(getattr(aggregation, "_reservoir", None))


def _shut_down(*components: Any) -> None:
    for component in components:
        if component is None:
            continue
        try:
            component.shutdown()
        except Exception:
            log.debug(
                "Metrics autocapture failed to shut down %s",
                component,
                exc_info=True,
            )


def _reinit_after_fork() -> None:
    global _lock
    # A parent thread can hold the lock at fork time. Replace it, never acquire it.
    _lock = threading.Lock()
    active = _active
    if active is not None:
        active._reinit_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reinit_after_fork)


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
    before_send: Optional[Callable] = None,
    _metric_reader: Any = None,
) -> Optional[MetricsAutocapture]:
    """Starts metrics autocapture. Returns ``None``, after one warning, when the
    OpenTelemetry packages are missing, when OpenTelemetry is already set up in
    the process, or when autocapture is already running. Never raises.

    ``before_send`` is the ``metrics.before_send`` hook. It gets each
    measurement as a ``client.metrics`` sample and can change its value and
    attributes, or drop it."""
    global _active, _warned_about_missing_packages
    host = host.rstrip("/")
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

        reader: Any = None
        meter_provider: Any = None
        tracer_provider: Any = None
        instrumentors: List[Any] = []
        try:
            if not _is_default_provider(
                metrics_api.get_meter_provider()
            ) or not _is_default_provider(trace_api.get_tracer_provider()):
                log.warning(
                    "OpenTelemetry is already set up in this process, so metrics autocapture does not start. "
                    "Point your OTLP metrics exporter at PostHog instead: https://posthog.com/docs/metrics"
                )
                return None

            # The same resource as `client.metrics`: the OTEL_SERVICE_NAME and
            # OTEL_RESOURCE_ATTRIBUTES of the app are not read.
            resource = sdk_resources.Resource(
                {
                    **resource_attributes,
                    "service.name": service_name or "unknown_service",
                    "telemetry.sdk.language": "python",
                    "telemetry.sdk.name": "opentelemetry",
                    "telemetry.sdk.version": _import_module(
                        "opentelemetry.sdk.version"
                    ).__version__,
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

                with _patched_environ(
                    {
                        key: None
                        for key in os.environ
                        if key.startswith(_OTLP_ENVIRONMENT_PREFIXES)
                    }
                ):
                    exporter = _GatedExporter(
                        endpoint=f"{host}/i/v1/metrics",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=_EXPORT_TIMEOUT_SECONDS,
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
                    )
                reader = sdk_metrics_export.PeriodicExportingMetricReader(
                    exporter,
                    export_interval_millis=(export_interval_seconds or 10.0) * 1000,
                )
            # The client exports the last window at exit, within its exit budget.
            # The provider's own exit hook waits up to the export timeout, also
            # when the PostHog host does not answer.
            meter_provider = sdk_metrics.MeterProvider(
                resource=resource, metric_readers=[reader], shutdown_on_exit=False
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
                    if kind != trace_api.SpanKind.CLIENT:
                        return sampling.SamplingResult(sampling.Decision.DROP)
                    # A recorded span starts with these attributes only. Some
                    # instrumentors, such as asyncpg, pass `db.system` here.
                    return sampling.SamplingResult(
                        sampling.Decision.RECORD_ONLY, attributes
                    )

                def get_description(self):
                    return "PostHogClientSpansOnly"

            stopped = threading.Event()
            bounded_meter_provider = _BoundedMeterProvider(
                meter_provider, _MAX_ATTRIBUTE_SETS, is_enabled, before_send, stopped
            )
            tracer_provider = sdk_trace.TracerProvider(
                resource=resource, sampler=_ClientSpansOnly(), shutdown_on_exit=False
            )
            # A span of the private provider is still the current span, so an HTTP
            # client would send its trace context to other services (as "not
            # sampled", which makes them drop their traces). HTTP metrics do not
            # need spans.
            no_op_tracer_provider = trace_api.NoOpTracerProvider()
            if areas.get("db"):
                tracer_provider.add_span_processor(
                    DbSpanMetricsProcessor(
                        bounded_meter_provider.get_meter(
                            "posthog.metrics_autocapture", VERSION
                        )
                    )
                )

            posthog_url_pattern = _posthog_url_pattern(host)
            wanted = set(_instrumentor_names(areas))
            with _stable_http_semconv():
                for entry_point in entry_points(group="opentelemetry_instrumentor"):
                    if entry_point.name not in wanted:
                        continue
                    if (
                        entry_point.name == "django"
                        and not _django_settings_configured()
                    ):
                        # Without settings, the Django instrumentor configures empty
                        # ones, and the app's own settings.configure() then fails.
                        # In AppConfig.ready() the settings are configured.
                        log.debug(
                            "Metrics autocapture skipped django: its settings are not configured yet"
                        )
                        continue
                    try:
                        kwargs: Dict[str, Any] = {
                            "tracer_provider": tracer_provider
                            if entry_point.name in _DB_INSTRUMENTORS
                            else no_op_tracer_provider,
                            "meter_provider": bounded_meter_provider,
                        }
                        environ: Dict[str, Optional[str]] = {}
                        if entry_point.name in _HTTP_CLIENT_INSTRUMENTORS:
                            # PostHog's own uploads, including this exporter's, are not app traffic.
                            variable, excluded = _excluded_urls(
                                entry_point.name, posthog_url_pattern
                            )
                            kwargs["excluded_urls"] = excluded
                            environ[variable] = excluded
                        with _patched_environ(environ):
                            instrumentor_class = entry_point.load()
                            # Instrumentors are singletons, and creating one runs
                            # `__init__` again on the existing instance. That would
                            # reset the config of an instance that the app uses.
                            existing = getattr(instrumentor_class, "_instance", None)
                            if (
                                existing is not None
                                and existing.is_instrumented_by_opentelemetry
                            ):
                                continue
                            instrumentor = (
                                instrumentor_class(config=_RUNTIME_CONFIG)
                                if entry_point.name == "system_metrics"
                                else instrumentor_class()
                            )
                            if instrumentor.is_instrumented_by_opentelemetry:
                                # The app instrumented this library itself.
                                continue
                            instrumentor.instrument(**kwargs)
                        if instrumentor.is_instrumented_by_opentelemetry:
                            instrumentors.append(instrumentor)
                            _disable_propagation(instrumentor_class)
                            if entry_point.name == "flask":
                                _track_flask_apps(
                                    instrumentor_class,
                                    {
                                        "tracer_provider": no_op_tracer_provider,
                                        "meter_provider": bounded_meter_provider,
                                    },
                                )
                    except Exception:
                        log.debug(
                            "Metrics autocapture skipped %s",
                            entry_point.name,
                            exc_info=True,
                        )

            _active = MetricsAutocapture(
                meter_provider, tracer_provider, instrumentors, reader, stopped
            )
            return _active
        except Exception:
            log.exception("Metrics autocapture failed to start")
            # Each start attempt would otherwise leave an export thread behind.
            _uninstrument(instrumentors)
            _shut_down(tracer_provider, meter_provider or reader)
            return None
