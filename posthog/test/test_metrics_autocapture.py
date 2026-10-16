import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest
import requests
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    InMemoryMetricReader,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind, Status, StatusCode

from posthog import metrics_autocapture
from posthog.client import Client
from posthog.metrics_autocapture import (
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

        with mock.patch("posthog.metrics_autocapture._import_module", missing):
            with mock.patch("posthog.metrics_autocapture.log") as log:
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
            with mock.patch("posthog.metrics_autocapture.log") as log:
                assert _start(posthog_host, started) is None

        assert "already set up" in str(log.warning.call_args)

    def test_runs_once_per_process_and_can_start_again_after_shutdown(
        self, posthog_host, started
    ):
        first = _start(posthog_host, started, InMemoryMetricReader())
        with mock.patch("posthog.metrics_autocapture.log") as log:
            assert _start(posthog_host, started) is None
        assert log.warning.call_count == 1

        first.shutdown()
        reader = InMemoryMetricReader()
        assert _start(posthog_host, started, reader) is not None

        connection = sqlite3.connect(":memory:")
        connection.cursor().execute("SELECT 1")
        connection.close()
        assert _points(reader, "db.client.operation.duration")

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

        # A parent thread can hold the lock at fork time.
        with metrics_autocapture._lock:
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
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if os.waitpid(pid, os.WNOHANG) != (0, 0):
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        result = os.read(read_fd, 16)
        os.close(read_fd)
        assert result == b"ok"
        # The child's shutdown does not stop the parent's autocapture.
        assert metrics_autocapture._active is handle


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
        with mock.patch("posthog.metrics_autocapture.log"):
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
