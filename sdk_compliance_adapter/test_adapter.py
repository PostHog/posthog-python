"""Regression tests for adapter configuration and the real SDK transport."""

import gzip
import importlib.util
import json
import os
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest
import zstandard

import posthog.capture_v1
import posthog.consumer
import posthog.request
from posthog import CaptureCompression


@pytest.fixture
def adapter_factory(monkeypatch):
    # Importing the adapter installs passive transport observers. Restore these
    # module globals afterwards so other SDK tests retain their normal transport.
    for module, name in (
        (posthog.request, "batch_post"),
        (posthog.consumer, "batch_post"),
        (posthog.capture_v1, "_post_v1"),
    ):
        monkeypatch.setattr(module, name, getattr(module, name))
    adapters = []

    def load(mode="v1", codec="gzip"):
        monkeypatch.setenv("CAPTURE_MODE", mode)
        monkeypatch.setenv("CAPTURE_COMPRESSION", codec)
        spec = importlib.util.spec_from_file_location(
            "compliance_adapter", Path(__file__).with_name("adapter.py")
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        adapters.append(module)
        return module

    yield load
    for adapter in adapters:
        adapter.state.reset()


@pytest.fixture
def receiver():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            encoding = self.headers.get("Content-Encoding")
            decoders = {
                "gzip": gzip.decompress,
                "deflate": zlib.decompress,
                "zstd": zstandard.ZstdDecompressor().decompress,
            }
            body = json.loads(decoders[encoding](raw) if encoding else raw)
            requests.append((self.path, encoding, body))
            response = json.dumps(
                {
                    "results": {
                        event["uuid"]: {"result": "ok"} for event in body["batch"]
                    }
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    # An explicit override lets concurrent local lanes reserve separate ports.
    server = HTTPServer(
        ("127.0.0.1", int(os.environ.get("ADAPTER_TEST_MOCK_PORT", "0"))), Handler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("codec", ["gzip", "deflate", "zstd"])
@pytest.mark.parametrize("enabled", [True, False, None])
def test_v1_compression_init_overrides_sdk_environment(
    adapter_factory, monkeypatch, receiver, codec, enabled
):
    monkeypatch.setenv("POSTHOG_CAPTURE_COMPRESSION", "deflate")
    adapter = adapter_factory(codec=codec)
    http = adapter.app.test_client()
    host, requests = receiver
    config = {"api_key": "test-key", "host": host, "flush_at": 1, "max_retries": 0}
    if enabled is not None:
        config["enable_compression"] = enabled
    assert http.post("/init", json=config).status_code == 200
    expected = CaptureCompression(codec) if enabled else CaptureCompression.NONE
    assert adapter.state.client.capture_compression == expected
    assert http.get("/health").json["capabilities"] == [
        "capture_v1",
        "capture_ai_v0",
        f"encoding_{codec}",
    ]
    captured = http.post(
        "/capture",
        json={
            "distinct_id": "test-user",
            "event": "test-event",
            "timestamp": "2024-01-02T03:04:05+05:30",
        },
    )
    assert captured.status_code == 200
    assert http.post("/flush").status_code == 200
    assert len(requests) == 1
    path, encoding, body = requests[0]
    assert path == "/i/v1/analytics/events"
    assert encoding == (codec if enabled else None)
    event = body["batch"][0]
    assert event["uuid"] == captured.json["uuid"]
    assert event["timestamp"] == "2024-01-01T21:34:05+00:00"


@pytest.mark.parametrize("mode", ["v0", "v1"])
def test_ai_uses_legacy_gzip_independent_of_analytics_codec(
    adapter_factory, receiver, mode
):
    adapter = adapter_factory(mode=mode, codec="zstd")
    http = adapter.app.test_client()
    host, requests = receiver
    assert (
        http.post(
            "/init",
            json={
                "api_key": "test-key",
                "host": host,
                "enable_compression": True,
                "flush_at": 1,
            },
        ).status_code
        == 200
    )
    captured = http.post(
        "/capture_ai", json={"distinct_id": "test-user", "event": "$ai_generation"}
    )
    assert captured.status_code == 200
    assert http.post("/flush").status_code == 200
    assert len(requests) == 1
    path, encoding, body = requests[0]
    assert path == "/i/v0/ai/batch/"
    assert encoding == "gzip"
    assert body["batch"][0]["uuid"] == captured.json["uuid"]
    if mode == "v0":
        assert http.get("/health").json["capabilities"] == [
            "capture_v0",
            "capture_ai_v0",
            "encoding_gzip",
        ]


@pytest.mark.parametrize("disable_geoip", [True, False, None])
def test_init_preserves_native_geoip_default(adapter_factory, receiver, disable_geoip):
    adapter = adapter_factory()
    host, _ = receiver
    config = {"api_key": "test-key", "host": host}
    if disable_geoip is not None:
        config["disable_geoip"] = disable_geoip
    assert adapter.app.test_client().post("/init", json=config).status_code == 200
    assert adapter.state.client.disable_geoip is (
        True if disable_geoip is None else disable_geoip
    )


@pytest.mark.parametrize("route", ["/flush", "/get_feature_flag"])
def test_flush_uses_public_unbounded_drain(adapter_factory, route):
    adapter = adapter_factory()
    client = Mock()
    client.get_feature_flag.return_value = "variant"
    adapter.state.client = client
    response = adapter.app.test_client().post(
        route, json={"key": "test-flag", "distinct_id": "test-user"}
    )
    assert response.status_code == 200
    client.flush.assert_called_once_with(timeout_seconds=None)
