import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import flask
import httpx
import opentelemetry.propagate
import pytest
import requests
from opentelemetry import baggage, context, trace
from opentelemetry.instrumentation._semconv import (
    _OpenTelemetrySemanticConventionStability,
)
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    InMemoryMetricReader,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.util.http import parse_excluded_urls

from posthog import _metrics_autocapture as metrics_autocapture
from posthog.client import Client
from posthog._metrics_autocapture import (
    DbSpanMetricsProcessor,
    _BoundedMeterProvider,
    start_metrics_autocapture,
)

FAKE_API_KEY = "phc_test_key"


class _Server:
    def __init__(self):
        received = self.received = []

        class Handler(BaseHTTPRequestHandler):
            def _respond(self):
                length = int(self.headers.get("content-length") or 0)
                received.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": self.rfile.read(length),
                    }
                )
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = _respond
            do_POST = _respond

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def posthog_host():
    server = _Server()
    yield server
    server.close()


@pytest.fixture
def app_server():
    server = _Server()
    yield server
    server.close()


@pytest.fixture
def started():
    handles = []
    yield handles
    for handle in handles:
        handle.shutdown()


@pytest.fixture(autouse=True)
def _reset_autocapture():
    # A failed test must not leave autocapture running for the tests after it.
    yield
    active = metrics_autocapture._active
    if active is not None:
        active.shutdown()
    metrics_autocapture._warned_about_missing_packages = False


class _ObservedLock:
    """A lock that sets ``waited`` when a thread has to wait for it."""

    def __init__(self, waited: threading.Event):
        self._lock = threading.Lock()
        self._waited = waited

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self._waited.set()
            self._lock.acquire()
        return self

    def __exit__(self, *exc_info):
        self._lock.release()


@contextmanager
def _held_by_another_thread(lock):
    # A forked child has no copy of this thread, so the lock stays held there.
    held, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            held.set()
            release.wait()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    held.wait()
    try:
        yield
    finally:
        release.set()
        thread.join()


def _child_result(pid, read_fd, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.waitpid(pid, os.WNOHANG) != (0, 0):
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    result = os.read(read_fd, 16)
    os.close(read_fd)
    return result


def _metrics_exports(server):
    return [r for r in server.received if r["path"].endswith("/i/v1/metrics")]


def _start(posthog_host, started, reader=None, **overrides):
    options = dict(
        host=posthog_host.url,
        api_key=FAKE_API_KEY,
        areas={"http": True, "db": True, "runtime": True},
        service_name="checkout-api",
        resource_attributes={"deployment.environment": "test"},
        is_enabled=lambda: True,
    )
    options.update(overrides)
    handle = start_metrics_autocapture(**options, _metric_reader=reader)
    if handle is not None:
        started.append(handle)
    return handle


def _points(reader, name):
    data = reader.get_metrics_data()
    points = []
    for resource_metrics in data.resource_metrics if data else []:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points


def _names(reader):
    data = reader.get_metrics_data()
    return {
        metric.name
        for resource_metrics in (data.resource_metrics if data else [])
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


class TestStartMetricsAutocapture:
    def test_records_http_client_durations_but_not_requests_to_posthog(
        self, posthog_host, app_server, started
    ):
        reader = InMemoryMetricReader()
        assert _start(posthog_host, started, reader) is not None

        requests.get(f"{app_server.url}/health")
        requests.post(f"{posthog_host.url}/batch/", data="{}")

        ports = [
            point.attributes.get("server.port")
            for point in _points(reader, "http.client.request.duration")
        ]
        assert app_server.port in ports
        assert posthog_host.port not in ports

    def test_records_database_operations_without_the_statement(
        self, posthog_host, started
    ):
        reader = InMemoryMetricReader()
        _start(posthog_host, started, reader)

        # The instrumentor wraps cursors, which drivers and SQLAlchemy use.
        connection = sqlite3.connect(":memory:")
        cursor = connection.cursor()
        cursor.execute("CREATE TABLE orders (email TEXT)")
        cursor.execute("SELECT * FROM orders WHERE email = 'a@b.c'")
        connection.close()

        points = _points(reader, "db.client.operation.duration")
        operations = {point.attributes.get("db.operation.name") for point in points}
        assert {"CREATE", "SELECT"} <= operations
        for point in points:
            assert point.attributes["db.system.name"] == "sqlite"
            assert all("a@b.c" not in str(value) for value in point.attributes.values())

    def test_records_runtime_metrics(self, posthog_host, started):
        reader = InMemoryMetricReader()
        _start(
            posthog_host,
            started,
            reader,
            areas={"http": False, "db": False, "runtime": True},
        )

        names = _names(reader)
        assert "process.cpu.time" in names
        assert any(name.startswith("cpython.gc.") for name in names)
        assert "http.client.request.duration" not in names

    def test_exports_otlp_to_posthog_with_the_project_token(
        self, posthog_host, app_server, started
    ):
        handle = _start(posthog_host, started)
        requests.get(f"{app_server.url}/health")

        handle.force_flush()

        exports = [r for r in posthog_host.received if r["path"] == "/i/v1/metrics"]
        assert exports
        headers = {k.lower(): v for k, v in exports[0]["headers"].items()}
        assert headers["authorization"] == f"Bearer {FAKE_API_KEY}"
        assert headers["content-type"] == "application/x-protobuf"
        body = b"".join(r["body"] for r in exports)
        for expected in (
            b"http.client.request.duration",
            b"checkout-api",
            b"telemetry.distro.name",
            b"posthog-python",
            b"deployment.environment",
        ):
            assert expected in body

    def test_sends_nothing_while_opted_out(self, posthog_host, app_server, started):
        handle = _start(posthog_host, started, is_enabled=lambda: False)
        requests.get(f"{app_server.url}/health")

        handle.force_flush()

        assert [r for r in posthog_host.received if r["path"] == "/i/v1/metrics"] == []

    def test_warns_once_and_returns_none_when_packages_are_missing(
        self, posthog_host, started
    ):
        def missing(name):
            raise ImportError(f"No module named '{name}'")

        with mock.patch("posthog._metrics_autocapture._import_module", missing):
            with mock.patch("posthog._metrics_autocapture.log") as log:
                assert _start(posthog_host, started) is None
                assert _start(posthog_host, started) is None

        assert log.warning.call_count == 1
        assert "posthog[metrics]" in str(log.warning.call_args)

    def test_does_not_start_when_opentelemetry_is_already_set_up(
        self, posthog_host, started
    ):
        with mock.patch(
            "opentelemetry.metrics.get_meter_provider", return_value=MeterProvider()
        ):
            with mock.patch("posthog._metrics_autocapture.log") as log:
                assert _start(posthog_host, started) is None

        assert "already set up" in str(log.warning.call_args)

    def test_runs_once_per_process_and_can_start_again_after_shutdown(
        self, posthog_host, started
    ):
        first = _start(posthog_host, started, InMemoryMetricReader())
        with mock.patch("posthog._metrics_autocapture.log") as log:
            assert _start(posthog_host, started) is None
        assert log.warning.call_count == 1

        first.shutdown()
        reader = InMemoryMetricReader()
        assert _start(posthog_host, started, reader) is not None

        connection = sqlite3.connect(":memory:")
        connection.cursor().execute("SELECT 1")
        connection.close()
        assert _points(reader, "db.client.operation.duration")

    def test_does_not_reconfigure_runtime_metrics_that_the_app_set_up(
        self, posthog_host, started
    ):
        # The app's meter provider is private, so autocapture still starts.
        app_reader = InMemoryMetricReader()
        app_instrumentor = SystemMetricsInstrumentor(
            config={"system.cpu.time": ["idle", "user"]}
        )
        app_instrumentor.instrument(
            meter_provider=MeterProvider(metric_readers=[app_reader])
        )
        try:
            assert (
                _start(
                    posthog_host,
                    started,
                    InMemoryMetricReader(),
                    areas={"runtime": True},
                )
                is not None
            )
            assert "system.cpu.time" in _names(app_reader)
        finally:
            app_instrumentor.uninstrument()

    def test_a_client_that_starts_during_shutdown_records_http_metrics(
        self, posthog_host, app_server, started, monkeypatch
    ):
        # Set when the other client has started, or has to wait for this shutdown.
        progress = threading.Event()
        monkeypatch.setattr(metrics_autocapture, "_lock", _ObservedLock(progress))
        first = _start(posthog_host, started, areas={"http": True})
        reader = InMemoryMetricReader()
        replacement = []

        def start_replacement():
            replacement.append(
                _start(posthog_host, started, reader, areas={"http": True})
            )
            progress.set()

        thread = threading.Thread(target=start_replacement)

        class StartsAnotherClientDuringShutdown(list):
            def __iter__(self):
                thread.start()
                assert progress.wait(timeout=10)
                return super().__iter__()

        first._instrumentors = StartsAnotherClientDuringShutdown(first._instrumentors)
        first.shutdown()
        thread.join()

        assert replacement[0] is not None
        requests.get(f"{app_server.url}/health")
        assert _points(reader, "http.client.request.duration")

    def test_strips_a_trailing_slash_from_the_host(
        self, posthog_host, app_server, started
    ):
        # A reverse proxy path, as in `host="https://example.com/ingest/"`.
        handle = _start(posthog_host, started, host=f"{posthog_host.url}/ingest/")
        requests.get(f"{app_server.url}/health")

        handle.force_flush()

        paths = {r["path"] for r in posthog_host.received}
        assert "/ingest/i/v1/metrics" in paths
        assert "/ingest//i/v1/metrics" not in paths

    def test_does_not_change_the_process_environment(
        self, posthog_host, started, monkeypatch
    ):
        monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
        assert _start(posthog_host, started, InMemoryMetricReader()) is not None
        assert "OTEL_SEMCONV_STABILITY_OPT_IN" not in os.environ

    def test_keeps_an_opt_in_that_the_app_set(self, posthog_host, started, monkeypatch):
        monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http/dup")
        assert _start(posthog_host, started, InMemoryMetricReader()) is not None
        assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http/dup"

    def test_uses_stable_http_names_when_opentelemetry_read_the_opt_in_before(
        self, posthog_host, app_server, started, monkeypatch
    ):
        # Each OpenTelemetry instrumentor reads the opt-in, but only the first
        # read in the process counts.
        stability = _OpenTelemetrySemanticConventionStability
        monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
        monkeypatch.setattr(stability, "_initialized", False)
        monkeypatch.setattr(stability, "_OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING", {})
        stability._initialize()

        reader = InMemoryMetricReader()
        _start(posthog_host, started, reader, areas={"http": True})
        requests.get(f"{app_server.url}/health")

        assert _points(reader, "http.client.request.duration")
        assert not _points(reader, "http.client.duration")

    def test_limits_http_attribute_sets(self, posthog_host, app_server, started):
        reader = InMemoryMetricReader()
        with mock.patch.object(metrics_autocapture, "_MAX_ATTRIBUTE_SETS", 1):
            _start(posthog_host, started, reader)

        requests.get(f"{app_server.url}/a")
        requests.post(f"{app_server.url}/b", data="{}")

        attributes = [
            dict(point.attributes)
            for point in _points(reader, "http.client.request.duration")
        ]
        assert len(attributes) == 2
        assert {"otel.metric.overflow": True} in attributes

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
    def test_keeps_working_in_a_forked_child(self, posthog_host, app_server, started):
        reader = InMemoryMetricReader()
        handle = _start(posthog_host, started, reader)
        read_fd, write_fd = os.pipe()

        with _held_by_another_thread(metrics_autocapture._lock):
            pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            result = b"fail"
            try:
                runtime = [i for i in handle._instrumentors if hasattr(i, "_proc")]
                requests.get(f"{app_server.url}/child")
                if (
                    runtime
                    and all(i._proc.pid == os.getpid() for i in runtime)
                    and _points(reader, "http.client.request.duration")
                ):
                    handle.shutdown()
                    result = b"ok"
            finally:
                os.write(write_fd, result)
                os._exit(0)

        os.close(write_fd)
        assert _child_result(pid, read_fd) == b"ok"
        # The child's shutdown does not stop the parent's autocapture.
        assert metrics_autocapture._active is handle

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
    def test_exports_from_a_child_forked_during_an_export(self, posthog_host, started):
        handle = _start(posthog_host, started, export_interval_seconds=3600)
        read_fd, write_fd = os.pipe()

        # The export thread holds this lock while it sends.
        with _held_by_another_thread(handle._reader._export_lock):
            pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            result = b"fail"
            try:
                handle.force_flush()
                result = b"ok"
            finally:
                os.write(write_fd, result)
                os._exit(0)

        os.close(write_fd)
        assert _child_result(pid, read_fd, timeout=15) == b"ok"

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
    def test_records_in_a_child_forked_during_a_collection(
        self, posthog_host, app_server, started
    ):
        handle = _start(
            posthog_host, started, export_interval_seconds=3600, areas={"http": True}
        )
        requests.get(f"{app_server.url}/health")
        consumer = handle._meter_provider._measurement_consumer
        locks = [consumer._lock]
        for storage in consumer._reader_storages.values():
            locks.append(storage._lock)
            for matches in storage._instrument_view_instrument_matches.values():
                for match in matches:
                    locks.append(match._lock)
                    locks.extend(
                        aggregation._lock
                        for aggregation in match._attributes_aggregation.values()
                    )
        read_fd, write_fd = os.pipe()

        # Collection and recording hold these locks in other threads.
        with ExitStack() as stack:
            for lock in locks:
                stack.enter_context(_held_by_another_thread(lock))
            pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            result = b"fail"
            try:
                requests.get(f"{app_server.url}/health")
                handle.force_flush()
                result = b"ok"
            finally:
                os.write(write_fd, result)
                os._exit(0)

        os.close(write_fd)
        assert _child_result(pid, read_fd, timeout=15) == b"ok"

    def test_does_not_send_the_apps_otlp_settings_to_posthog(
        self, posthog_host, app_server, started, monkeypatch
    ):
        # These configure the app's own collector, not PostHog.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-vendor-key=vendor-secret")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "gzip")
        handle = _start(posthog_host, started)
        requests.get(f"{app_server.url}/health")

        handle.force_flush()

        exports = _metrics_exports(posthog_host)
        assert exports
        headers = {k.lower(): v for k, v in exports[0]["headers"].items()}
        assert "x-vendor-key" not in headers
        assert "content-encoding" not in headers
        assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "x-vendor-key=vendor-secret"

    def test_does_not_add_trace_context_to_app_requests(
        self, posthog_host, app_server, started
    ):
        # The spans are never exported, so a downstream service must not see them.
        reader = InMemoryMetricReader()
        _start(posthog_host, started, reader)

        requests.get(f"{app_server.url}/health")

        headers = {k.lower() for k in app_server.received[-1]["headers"]}
        assert "traceparent" not in headers
        [point] = _points(reader, "http.client.request.duration")
        assert point.attributes["http.response.status_code"] == 200

    def test_records_nothing_while_disabled(self, posthog_host, app_server, started):
        # Measurements from the disabled period must not go out once it is enabled.
        enabled = [False]
        reader = InMemoryMetricReader()
        _start(posthog_host, started, reader, is_enabled=lambda: enabled[0])

        requests.get(f"{app_server.url}/health")
        connection = sqlite3.connect(":memory:")
        connection.cursor().execute("SELECT 1")
        connection.close()
        enabled[0] = True

        assert not _points(reader, "http.client.request.duration")
        assert not _points(reader, "db.client.operation.duration")

    def test_applies_before_send_to_measurements(
        self, posthog_host, app_server, started
    ):
        samples = []

        def before_send(sample):
            samples.append(dict(sample))
            if sample["attributes"].get("http.request.method") == "POST":
                return None
            sample["attributes"] = {
                key: value
                for key, value in sample["attributes"].items()
                if key != "server.address"
            }
            return sample

        reader = InMemoryMetricReader()
        _start(
            posthog_host, started, reader, areas={"http": True}, before_send=before_send
        )

        requests.get(f"{app_server.url}/a")
        requests.post(f"{app_server.url}/b", data="{}")

        points = _points(reader, "http.client.request.duration")
        assert [point.attributes["http.request.method"] for point in points] == ["GET"]
        assert "server.address" not in points[0].attributes
        sample = next(s for s in samples if s["name"] == "http.client.request.duration")
        assert sample["type"] == "histogram"
        assert sample["unit"] == "s"
        assert isinstance(sample["value"], float)

    def test_drops_measurements_when_before_send_raises(
        self, posthog_host, app_server, started
    ):
        def before_send(sample):
            raise ValueError("bug in the hook")

        reader = InMemoryMetricReader()
        _start(
            posthog_host, started, reader, areas={"http": True}, before_send=before_send
        )

        with mock.patch("posthog._metrics_autocapture.log") as log:
            response = requests.get(f"{app_server.url}/health")

        assert response.status_code == 200
        assert not _points(reader, "http.client.request.duration")
        assert log.error.called

    def test_applies_before_send_to_runtime_metrics(self, posthog_host, started):
        reader = InMemoryMetricReader()
        _start(
            posthog_host,
            started,
            reader,
            areas={"runtime": True},
            before_send=lambda s: None if s["name"] == "process.cpu.time" else s,
        )

        names = _names(reader)
        assert "process.cpu.time" not in names
        assert "process.memory.usage" in names

    def test_does_not_record_requests_to_posthog_whatever_the_host_case(
        self, posthog_host, started
    ):
        reader = InMemoryMetricReader()
        host = f"http://LocalHost:{posthog_host.port}"
        _start(posthog_host, started, reader, host=host)

        requests.post(f"{host}/batch/", data="{}")

        assert not _points(reader, "http.client.request.duration")

    def test_keeps_the_apps_excluded_urls(
        self, posthog_host, app_server, started, monkeypatch
    ):
        monkeypatch.setenv("OTEL_PYTHON_REQUESTS_EXCLUDED_URLS", "healthz")
        reader = InMemoryMetricReader()
        _start(posthog_host, started, reader, areas={"http": True})

        requests.get(f"{app_server.url}/healthz")
        requests.post(f"{app_server.url}/orders", data="{}")

        methods = {
            point.attributes["http.request.method"]
            for point in _points(reader, "http.client.request.duration")
        }
        assert methods == {"POST"}
        assert os.environ["OTEL_PYTHON_REQUESTS_EXCLUDED_URLS"] == "healthz"

    def test_does_not_record_httpx_requests_to_posthog(
        self, posthog_host, app_server, started
    ):
        reader = InMemoryMetricReader()
        _start(posthog_host, started, reader, areas={"http": True})

        httpx.post(f"{posthog_host.url}/batch/", content="{}")
        httpx.get(f"{app_server.url}/health")

        ports = [
            point.attributes.get("server.port")
            for point in _points(reader, "http.client.request.duration")
        ]
        assert app_server.port in ports
        assert posthog_host.port not in ports

    def test_measures_an_existing_flask_app_again_after_a_restart(
        self, posthog_host, started
    ):
        first_reader = InMemoryMetricReader()
        first = _start(posthog_host, started, first_reader, areas={"http": True})
        app = flask.Flask(__name__)
        app.add_url_rule("/health", "health", lambda: "ok")
        app.test_client().get("/health")
        assert _points(first_reader, "http.server.request.duration")

        first.shutdown()
        reader = InMemoryMetricReader()
        _start(posthog_host, started, reader, areas={"http": True})
        app.test_client().get("/health")

        [point] = _points(reader, "http.server.request.duration")
        assert point.count == 1

    def test_shutdown_keeps_middleware_that_the_app_added(self, posthog_host, started):
        samples = []

        def before_send(sample):
            samples.append(sample["name"])
            return sample

        handle = _start(
            posthog_host,
            started,
            InMemoryMetricReader(),
            areas={"http": True},
            before_send=before_send,
        )
        app = flask.Flask(__name__)
        app.add_url_rule("/secret", "secret", lambda: "secret")
        protected = app.wsgi_app

        def require_auth(environ, start_response):
            if environ.get("HTTP_AUTHORIZATION") != "token":
                start_response("401 Unauthorized", [("Content-Length", "0")])
                return [b""]
            return protected(environ, start_response)

        app.wsgi_app = require_auth
        client = app.test_client()
        assert client.get("/secret").status_code == 401

        handle.shutdown()
        samples.clear()

        assert client.get("/secret").status_code == 401
        assert (
            client.get("/secret", headers={"Authorization": "token"}).status_code == 200
        )
        # The instrumentation left inside the middleware records nothing more.
        assert samples == []

    def test_does_not_propagate_context_to_other_services(
        self, posthog_host, app_server, started
    ):
        reader = InMemoryMetricReader()
        handle = _start(posthog_host, started, reader, areas={"http": True})
        remote = trace.SpanContext(
            trace_id=0x1234,
            span_id=0x5678,
            is_remote=True,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
        )
        # Baggage and an incoming trace, as a server instrumentor attaches them.
        current = baggage.set_baggage(
            "user.email",
            "a@b.c",
            trace.set_span_in_context(trace.NonRecordingSpan(remote)),
        )
        token = context.attach(current)
        try:
            requests.get(f"{app_server.url}/a")
            httpx.get(f"{app_server.url}/b")
        finally:
            context.detach(token)

        for request in app_server.received:
            headers = {key.lower() for key in request["headers"]}
            assert not headers & {"traceparent", "tracestate", "baggage"}
        assert len(_points(reader, "http.client.request.duration")) == 2

        handle.shutdown()
        requests_instrumentation = sys.modules["opentelemetry.instrumentation.requests"]
        assert requests_instrumentation.inject is opentelemetry.propagate.inject

    def test_keeps_the_attributes_a_database_span_starts_with(
        self, posthog_host, started
    ):
        # asyncpg passes `db.system` when it starts the span, not later.
        reader = InMemoryMetricReader()
        handle = _start(posthog_host, started, reader, areas={"db": True})
        tracer = handle._tracer_provider.get_tracer(
            "opentelemetry.instrumentation.asyncpg"
        )

        tracer.start_span(
            "SELECT",
            kind=SpanKind.CLIENT,
            attributes={"db.system": "postgresql", "db.operation": "SELECT"},
        ).end()

        [point] = _points(reader, "db.client.operation.duration")
        assert dict(point.attributes) == {
            "db.system.name": "postgresql",
            "db.operation.name": "SELECT",
        }

    def test_coalesces_concurrent_flushes(self, posthog_host, started, monkeypatch):
        handle = _start(posthog_host, started, InMemoryMetricReader())
        release = threading.Event()
        flushes = []

        def slow_force_flush(*args, **kwargs):
            flushes.append(threading.current_thread().name)
            release.wait(10)

        monkeypatch.setattr(handle._meter_provider, "force_flush", slow_force_flush)
        waiters = [handle.start_flush() for _ in range(5)]
        flushers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "posthog-metrics-autocapture-flush"
        ]
        release.set()
        for wait in waiters:
            wait(10)

        assert len(flushers) == 1
        # The calls made during the first flush get one more, so their
        # measurements are exported too.
        assert len(flushes) == 2

    def test_cleans_up_when_startup_fails(self, posthog_host, started):
        real_entry_points = metrics_autocapture.entry_points

        def failing_entry_points(**kwargs):
            yield from real_entry_points(**kwargs)
            raise RuntimeError("broken plugin")

        def reader_threads():
            return [
                thread
                for thread in threading.enumerate()
                if thread.name == "OtelPeriodicExportingMetricReader"
            ]

        before = len(reader_threads())
        with mock.patch.object(
            metrics_autocapture, "entry_points", failing_entry_points
        ):
            with mock.patch("posthog._metrics_autocapture.log"):
                assert _start(posthog_host, started) is None

        assert len(reader_threads()) == before
        assert not RequestsInstrumentor().is_instrumented_by_opentelemetry
        assert metrics_autocapture._active is None

    def test_uses_the_same_resource_as_the_manual_metrics_api(
        self, posthog_host, started, monkeypatch
    ):
        monkeypatch.setenv("OTEL_SERVICE_NAME", "from-env")
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=from-env")
        reader = InMemoryMetricReader()
        _start(
            posthog_host,
            started,
            reader,
            areas={"runtime": True},
            service_name=None,
            resource_attributes={},
        )

        resource = reader.get_metrics_data().resource_metrics[0].resource.attributes
        assert resource["service.name"] == "unknown_service"
        assert "team" not in resource
        assert resource["telemetry.distro.name"] == "posthog-python"

    def test_shutdown_twice_is_safe(self, posthog_host, started):
        handle = _start(posthog_host, started, InMemoryMetricReader())

        handle.shutdown()
        handle.shutdown()

        assert metrics_autocapture._active is None


class TestPostHogUrlExclusion:
    def _excluded(self, host, url):
        pattern = metrics_autocapture._posthog_url_pattern(host)
        return parse_excluded_urls(pattern).url_disabled(url)

    def test_matches_the_host_with_or_without_its_default_port(self):
        assert self._excluded(
            "https://us.i.posthog.com", "https://us.i.posthog.com:443/batch/"
        )
        assert self._excluded(
            "https://us.i.posthog.com:443", "https://us.i.posthog.com/batch/"
        )
        assert self._excluded("http://LocalHost:8000", "http://localhost:8000/batch/")

    def test_does_not_match_other_hosts_ports_or_paths(self):
        assert not self._excluded(
            "https://us.i.posthog.com", "https://us.i.posthog.com.evil.com/"
        )
        assert not self._excluded(
            "https://us.i.posthog.com", "https://us.i.posthog.com:8443/batch/"
        )
        assert not self._excluded(
            "https://example.com/ingest", "https://example.com/ingestion"
        )

    def test_keeps_a_comma_in_the_host_path(self):
        assert self._excluded(
            "https://example.com/a,b", "https://example.com/a,b/i/v1/metrics"
        )
        assert not self._excluded("https://example.com/a,b", "https://unrelated.com/b")


class TestBoundedMeterProvider:
    def test_sends_new_attribute_sets_to_an_overflow_series(self):
        # Delta, as autocapture exports: the SDK still keeps each attribute set.
        reader = InMemoryMetricReader(
            preferred_temporality={Histogram: AggregationTemporality.DELTA}
        )
        provider = _BoundedMeterProvider(MeterProvider(metric_readers=[reader]), 2)
        histogram = provider.get_meter("test").create_histogram("duration")

        for _ in range(2):
            for host in ("a", "b", "c", "d"):
                histogram.record(1, {"server.address": host})
            points = _points(reader, "duration")
            assert sorted(
                (str(dict(point.attributes)), point.count) for point in points
            ) == [
                ("{'otel.metric.overflow': True}", 2),
                ("{'server.address': 'a'}", 1),
                ("{'server.address': 'b'}", 1),
            ]


class TestDbSpanMetricsProcessor:
    def _record(self, scope, kind=SpanKind.CLIENT, attributes=None, error=False):
        reader = InMemoryMetricReader()
        meter = MeterProvider(metric_readers=[reader]).get_meter("test")
        provider = TracerProvider()
        provider.add_span_processor(DbSpanMetricsProcessor(meter))
        span = provider.get_tracer(scope).start_span("query", kind=kind)
        span.set_attributes(attributes or {})
        if error:
            span.set_status(Status(StatusCode.ERROR))
        span.end()
        return _points(reader, "db.client.operation.duration")

    def test_turns_a_driver_span_into_a_duration_histogram(self):
        points = self._record(
            "opentelemetry.instrumentation.psycopg2",
            attributes={
                "db.system": "postgresql",
                "db.statement": "select * from orders where id = 7",
                "db.name": "shop",
                "net.peer.name": "db.internal",
                "net.peer.port": 5432,
            },
        )

        assert len(points) == 1
        assert dict(points[0].attributes) == {
            "db.system.name": "postgresql",
            "db.operation.name": "SELECT",
            "db.namespace": "shop",
            "server.address": "db.internal",
            "server.port": 5432,
        }

    def test_marks_failures(self):
        points = self._record(
            "opentelemetry.instrumentation.pymongo",
            attributes={"db.system": "mongodb", "db.operation": "find"},
            error=True,
        )

        assert dict(points[0].attributes) == {
            "db.system.name": "mongodb",
            "db.operation.name": "find",
            "error.type": "_OTHER",
        }

    def test_keeps_only_the_first_word_of_the_operation(self):
        points = self._record(
            "opentelemetry.instrumentation.psycopg",
            attributes={
                "db.system": "postgresql",
                "db.operation.name": "SELECT orders",
            },
        )

        assert points[0].attributes["db.operation.name"] == "SELECT"

    def test_skips_orm_layers_and_other_spans(self):
        # SQLAlchemy wraps a driver span, so counting it would count the query twice.
        assert (
            self._record(
                "opentelemetry.instrumentation.sqlalchemy",
                attributes={"db.system": "postgresql"},
            )
            == []
        )
        assert (
            self._record(
                "opentelemetry.instrumentation.psycopg2",
                kind=SpanKind.SERVER,
                attributes={"db.system": "postgresql"},
            )
            == []
        )
        assert (
            self._record(
                "opentelemetry.instrumentation.requests",
                attributes={"http.request.method": "GET"},
            )
            == []
        )


class TestClientOption:
    def test_starts_with_metrics_autocapture_and_stops_on_shutdown(
        self, posthog_host, started
    ):
        client = Client(
            FAKE_API_KEY, host=posthog_host.url, metrics={"autocapture": True}
        )
        with mock.patch("posthog._metrics_autocapture.log"):
            assert _start(posthog_host, started) is None

        client.shutdown()
        assert _start(posthog_host, started) is not None

    def test_does_not_start_by_default_or_when_disabled(self, posthog_host, started):
        plain = Client(FAKE_API_KEY, host=posthog_host.url)
        disabled = Client(
            FAKE_API_KEY,
            host=posthog_host.url,
            disabled=True,
            metrics={"autocapture": True},
        )

        assert _start(posthog_host, started) is not None
        plain.shutdown()
        disabled.shutdown()

    def test_sends_nothing_when_send_is_false(self, posthog_host, app_server):
        client = Client(
            FAKE_API_KEY,
            host=posthog_host.url,
            send=False,
            metrics={"autocapture": True},
        )
        try:
            assert client._metrics_autocapture is not None
            requests.get(f"{app_server.url}/health")
            client._metrics_autocapture.force_flush()
            assert [
                r for r in posthog_host.received if r["path"] == "/i/v1/metrics"
            ] == []
        finally:
            client.shutdown()

    def test_passes_the_metrics_options_to_autocapture(self, posthog_host):
        hook_calls = []

        def before_send(sample):
            hook_calls.append(configured._lifecycle_callback_context.get())
            return {**sample, "value": 2.0}

        with mock.patch(
            "posthog._metrics_autocapture.start_metrics_autocapture", return_value=None
        ) as start:
            configured = Client(
                FAKE_API_KEY,
                host=posthog_host.url,
                metrics={
                    "autocapture": {"runtime": False},
                    "service_name": "billing-api",
                    "service_version": "1.2.3",
                    "environment": "staging",
                    "resource_attributes": {
                        "deployment.environment": "prod",
                        "team": "core",
                    },
                    "flush_interval": 30,
                    "before_send": before_send,
                },
            )
            configured.shutdown()
            Client(
                FAKE_API_KEY,
                host=posthog_host.url,
                metrics={
                    "autocapture": True,
                    "service_name": "billing-api",
                    "resource_attributes": {"service.name": "billing-worker"},
                },
            ).shutdown()

        first, second = (call.kwargs for call in start.call_args_list)
        assert first["host"] == posthog_host.url
        assert first["api_key"] == FAKE_API_KEY
        assert first["areas"] == {"http": True, "db": True, "runtime": False}
        assert first["service_name"] == "billing-api"
        assert first["resource_attributes"] == {
            "deployment.environment": "prod",
            "team": "core",
            "service.version": "1.2.3",
        }
        assert first["export_interval_seconds"] == 30
        # The hook runs as an SDK callback, so a flush() it calls is deferred.
        assert first["before_send"]({"name": "m", "value": 1.0}) == {
            "name": "m",
            "value": 2.0,
        }
        assert hook_calls == [True]
        assert second["areas"] == {"http": True, "db": True, "runtime": True}
        assert second["service_name"] == "billing-worker"
        assert second["export_interval_seconds"] == 10
        assert second["before_send"] is None

    @pytest.mark.parametrize("flush_interval", [-1, "5", True])
    def test_ignores_an_invalid_flush_interval(self, posthog_host, flush_interval):
        client = Client(
            FAKE_API_KEY,
            host=posthog_host.url,
            metrics={"autocapture": True, "flush_interval": flush_interval},
        )
        try:
            assert client._metrics_autocapture is not None
            assert client._metrics_autocapture._reader._export_interval_millis == 10_000
        finally:
            client.shutdown()

    def test_flush_exports_autocaptured_metrics(self, posthog_host, app_server):
        client = Client(
            FAKE_API_KEY,
            host=posthog_host.url,
            metrics={"autocapture": True, "flush_interval": 3600},
        )
        try:
            requests.get(f"{app_server.url}/health")
            client.flush()
            assert _metrics_exports(posthog_host)
        finally:
            client.shutdown()

    def test_shutdown_twice_is_safe(self, posthog_host):
        client = Client(
            FAKE_API_KEY, host=posthog_host.url, metrics={"autocapture": True}
        )

        client.shutdown()
        client.shutdown()

        assert metrics_autocapture._active is None

    def test_exit_does_not_wait_for_an_unreachable_host(self):
        # Accepts connections and never answers, like a host behind a dead proxy.
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        script = textwrap.dedent(
            f"""
            from posthog.client import Client
            Client(
                "{FAKE_API_KEY}",
                host="http://127.0.0.1:{server.getsockname()[1]}",
                metrics={{"autocapture": {{"http": False, "db": False}}}},
            )
            """
        )
        try:
            began = time.monotonic()
            subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                timeout=60,
                cwd=Path(__file__).resolve().parents[2],
            )
            elapsed = time.monotonic() - began
        finally:
            server.close()

        assert elapsed < 6

    def test_sync_mode_exports_the_last_window_at_exit(self, posthog_host):
        script = textwrap.dedent(
            f"""
            import sqlite3
            from posthog.client import Client
            Client(
                "{FAKE_API_KEY}",
                host="{posthog_host.url}",
                sync_mode=True,
                metrics={{
                    "autocapture": {{"http": False, "runtime": False}},
                    "flush_interval": 3600,
                }},
            )
            connection = sqlite3.connect(":memory:")
            connection.cursor().execute("SELECT 1")
            connection.close()
            """
        )
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            timeout=60,
            cwd=Path(__file__).resolve().parents[2],
        )

        exports = _metrics_exports(posthog_host)
        assert exports
        assert b"db.client.operation.duration" in b"".join(r["body"] for r in exports)

    def test_before_send_can_call_flush(self, posthog_host):
        # Runtime callbacks run while OpenTelemetry holds its collection lock.
        script = textwrap.dedent(
            f"""
            import os, threading
            from posthog.client import Client

            def before_send(sample):
                if sample["name"] == "process.cpu.time":
                    client.flush(timeout_seconds=None)
                return sample

            client = Client(
                "{FAKE_API_KEY}",
                host="{posthog_host.url}",
                metrics={{
                    "autocapture": {{"http": False, "db": False}},
                    "flush_interval": 3600,
                    "before_send": before_send,
                }},
            )
            flush = threading.Thread(
                target=client.flush, kwargs={{"timeout_seconds": None}}, daemon=True
            )
            flush.start()
            flush.join(10)
            print("deadlock" if flush.is_alive() else "ok", flush=True)
            os._exit(0)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=Path(__file__).resolve().parents[2],
        )

        assert result.stdout.strip() == "ok"
        assert _metrics_exports(posthog_host)

    def _run_django_script(self, posthog_host, body):
        script = textwrap.dedent(
            f"""
            import os
            os.environ.pop("DJANGO_SETTINGS_MODULE", None)
            from django.conf import settings
            from posthog.client import Client

            def start():
                return Client(
                    "{FAKE_API_KEY}",
                    host="{posthog_host.url}",
                    metrics={{"autocapture": {{"db": False, "runtime": False}}}},
                )
            """
        ) + textwrap.dedent(body)
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=Path(__file__).resolve().parents[2],
        )

    def test_does_not_configure_django_settings(self, posthog_host):
        # Django apps that configure settings in code do it after imports.
        result = self._run_django_script(
            posthog_host,
            """
            client = start()
            settings.configure(DEBUG=True)
            print("configured", settings.DEBUG)
            client.shutdown()
            """,
        )

        assert result.stdout.strip() == "configured True", result.stderr[-2000:]

    def test_instruments_django_once_its_settings_are_configured(self, posthog_host):
        # As in AppConfig.ready().
        result = self._run_django_script(
            posthog_host,
            """
            settings.configure(MIDDLEWARE=[])
            client = start()
            print("middleware", settings.MIDDLEWARE)
            client.shutdown()
            """,
        )

        assert "_DjangoMiddleware" in result.stdout, result.stderr[-2000:]

    def test_starts_on_a_later_module_setup(self, posthog_host, monkeypatch):
        # A Django ready() hook can set the option after setup() already ran.
        import posthog

        monkeypatch.setattr(posthog, "default_client", None)
        monkeypatch.setattr(posthog, "api_key", FAKE_API_KEY)
        monkeypatch.setattr(posthog, "host", posthog_host.url)
        monkeypatch.setattr(posthog, "metrics", None)
        client = posthog.setup()
        try:
            assert client._metrics_autocapture is None
            monkeypatch.setattr(posthog, "metrics", {"autocapture": True})
            assert posthog.setup() is client
            assert client._metrics_autocapture is not None
        finally:
            client.shutdown()

    def test_module_setup_tries_to_start_once(self, posthog_host, started, monkeypatch):
        import posthog

        assert _start(posthog_host, started, InMemoryMetricReader()) is not None
        monkeypatch.setattr(posthog, "default_client", None)
        monkeypatch.setattr(posthog, "api_key", FAKE_API_KEY)
        monkeypatch.setattr(posthog, "host", posthog_host.url)
        monkeypatch.setattr(posthog, "metrics", {"autocapture": True})
        with mock.patch("posthog._metrics_autocapture.log") as log:
            client = posthog.setup()
            posthog.setup()
            posthog.setup()
        client.shutdown()

        assert log.warning.call_count == 1
