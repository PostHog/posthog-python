import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from unittest.mock import Mock, patch

import pytest
import requests

import posthog
from posthog._remote_config import _RemoteConfigPoller, _fetch_remote_config
from posthog.client import Client


@pytest.mark.parametrize(
    "host,base",
    [
        (None, "https://us-assets.i.posthog.com"),
        ("https://us.i.posthog.com/", "https://us-assets.i.posthog.com"),
        ("https://eu.i.posthog.com", "https://eu-assets.i.posthog.com"),
        ("https://app.posthog.com", "https://us-assets.i.posthog.com"),
        ("https://eu.posthog.com", "https://eu-assets.i.posthog.com"),
        ("https://proxy.example/posthog/", "https://proxy.example/posthog"),
    ],
)
def test_request_contract(host, base):
    config = {"hasFeatureFlags": False, "futureSetting": {"enabled": True}}
    with patch("posthog._remote_config._get_session") as session:
        response = session.return_value.get.return_value.__enter__.return_value
        response.json.return_value = config
        assert _fetch_remote_config("phc_test", host, 3) == config
        session.return_value.get.assert_called_once_with(
            base + "/array/phc_test/config", timeout=3
        )
        response.raise_for_status.assert_called_once()


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"sdkDiagnosticsEnabled": True}, True),
        ({"sdkDiagnosticsEnabled": False}, False),
        ({}, False),
        *[
            ({"sdkDiagnosticsEnabled": value}, False)
            for value in [None, 1, 0, "true", "false", [], {}, [True]]
        ],
    ],
)
@pytest.mark.parametrize("local_enabled", [True, False])
def test_sdk_diagnostics_remote_config_value(config, expected, local_enabled):
    with (
        patch("posthog._remote_config._RemoteConfigPoller.start"),
        patch("posthog._remote_config._RemoteConfigPoller.stop"),
        patch("posthog._remote_config._fetch_remote_config", return_value=config),
    ):
        client = Client(
            "phc_test", sync_mode=True, sdk_diagnostics_enabled=local_enabled
        )
        try:
            assert client._sdk_diagnostics_enabled is False
            client._remote_config_poller._refresh()
            assert client._sdk_diagnostics_enabled is (local_enabled and expected)
            assert client.sdk_diagnostics_enabled is local_enabled
        finally:
            client.shutdown()


def test_module_sdk_diagnostics_local_setting(monkeypatch):
    monkeypatch.setattr(posthog, "default_client", None)
    monkeypatch.setattr(posthog, "project_api_key", "phc_test")
    monkeypatch.setattr(posthog, "sync_mode", True)
    monkeypatch.setattr(posthog, "sdk_diagnostics_enabled", False)
    with (
        patch("posthog._remote_config._RemoteConfigPoller.start"),
        patch("posthog._remote_config._RemoteConfigPoller.stop"),
        patch(
            "posthog._remote_config._fetch_remote_config",
            return_value={
                "sdkDiagnosticsEnabled": True,
            },
        ),
    ):
        client = posthog.setup()
        try:
            client._remote_config_poller._refresh()
            assert client.sdk_diagnostics_enabled is False
            assert client._sdk_diagnostics_enabled is False
            posthog.sdk_diagnostics_enabled = True
            assert posthog.setup() is client
            assert client._sdk_diagnostics_enabled is True
            posthog.sdk_diagnostics_enabled = False
            posthog.setup()
            assert client._sdk_diagnostics_enabled is False
        finally:
            client.shutdown()


def test_sdk_diagnostics_disabled_without_remote_config():
    client = Client(
        "phc_test", sync_mode=True, remote_config_poll_interval_seconds=None
    )
    try:
        assert client.sdk_diagnostics_enabled is True
        assert client._sdk_diagnostics_enabled is False
    finally:
        client.shutdown()


@pytest.mark.parametrize("replacement", [{}, {"sdkDiagnosticsEnabled": "true"}])
def test_sdk_diagnostics_missing_or_invalid_refresh_disables(replacement):
    with (
        patch("posthog._remote_config._RemoteConfigPoller.start"),
        patch("posthog._remote_config._RemoteConfigPoller.stop"),
        patch(
            "posthog._remote_config._fetch_remote_config",
            side_effect=[
                {"sdkDiagnosticsEnabled": True},
                replacement,
            ],
        ),
    ):
        client = Client("phc_test", sync_mode=True)
        try:
            client._remote_config_poller._refresh()
            assert client._sdk_diagnostics_enabled is True
            client._remote_config_poller._refresh()
            assert client._sdk_diagnostics_enabled is False
        finally:
            client.shutdown()


def test_stopped_before_start_does_not_fetch():
    worker = _RemoteConfigPoller("phc_test", "https://proxy.example", 300, 3)
    worker.stopped.set()
    with patch("posthog._remote_config._fetch_remote_config") as fetch:
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        fetch.assert_not_called()


def test_token_is_one_path_segment():
    with patch("posthog._remote_config._get_session") as session:
        response = session.return_value.get.return_value.__enter__.return_value
        response.json.return_value = {}
        _fetch_remote_config("a/b?c", "https://proxy.example", 3)
        assert session.return_value.get.call_args.args[0].endswith(
            "/array/a%2Fb%3Fc/config"
        )


@pytest.mark.parametrize("value", [[], None, True, "config", 42])
def test_reject_non_object(value):
    with patch("posthog._remote_config._get_session") as session:
        response = session.return_value.get.return_value.__enter__.return_value
        response.json.return_value = value
        with pytest.raises(ValueError):
            _fetch_remote_config("phc_test", "https://proxy.example", 3)


@pytest.mark.parametrize(
    "failure", [requests.Timeout(), ValueError(), requests.HTTPError()]
)
def test_refresh_failure_preserves_last_success(failure):
    worker = _RemoteConfigPoller("phc_test", "https://proxy.example", 300, 3)
    worker.stopped = Mock()
    worker.stopped.is_set.return_value = False
    worker.stopped.wait.side_effect = [False, True]
    with patch(
        "posthog._remote_config._fetch_remote_config", side_effect=[{"x": 1}, failure]
    ) as fetch:
        worker.run()
    assert worker._config == {"x": 1}
    assert fetch.call_count == 2
    assert worker.stopped.wait.call_args.args == (300,)


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), True, "300"])
def test_invalid_interval(interval):
    with pytest.raises(ValueError, match="remote_config_poll_interval_seconds"):
        Client("phc_test", remote_config_poll_interval_seconds=interval)


@pytest.mark.parametrize(
    "options",
    [
        {"send": False},
        {"remote_config_poll_interval_seconds": None},
    ],
)
def test_fetching_opt_out_does_not_start(options):
    with patch("posthog.client._RemoteConfigPoller") as worker:
        client = Client("phc_test", **options)
        try:
            worker.assert_not_called()
        finally:
            client.shutdown()


@pytest.mark.parametrize("module_client", [False, True])
def test_disabled_client_fetches_after_reenabling(monkeypatch, module_client):
    fetched = threading.Event()

    def fetch(*args):
        fetched.set()
        return {}

    with patch("posthog._remote_config._fetch_remote_config", side_effect=fetch):
        if module_client:
            monkeypatch.setattr(posthog, "default_client", None)
            monkeypatch.setattr(posthog, "project_api_key", "phc_test")
            monkeypatch.setattr(posthog, "disabled", True)
            monkeypatch.setattr(posthog, "sync_mode", True)
            monkeypatch.setattr(posthog, "send", True)
            monkeypatch.setattr(posthog, "remote_config_poll_interval_seconds", 0.01)
            client = posthog.setup()
        else:
            client = Client(
                "phc_test",
                disabled=True,
                sync_mode=True,
                remote_config_poll_interval_seconds=0.01,
            )
        try:
            assert not fetched.wait(0.05)
            if module_client:
                posthog.disabled = False
                assert posthog.setup() is client
            else:
                client.disabled = False
            assert fetched.wait(2), "Re-enabled client never fetched remote config"
        finally:
            client.shutdown()


def test_sync_client_registers_nonblocking_exit_cleanup():
    entered = threading.Event()
    release = threading.Event()

    def fetch(*args):
        entered.set()
        assert release.wait(5)
        return {}

    with (
        patch("posthog._remote_config._fetch_remote_config", side_effect=fetch),
        patch("posthog.client.atexit.register") as register,
    ):
        client = Client("phc_test", sync_mode=True)
        try:
            assert entered.wait(5)
            for call in register.call_args_list:
                callback, *args = call.args
                callback(*args, **call.kwargs)
            assert client._remote_config_poller.stopped.is_set(), (
                "Registered exit callbacks did not signal the sync poller"
            )
            assert client._remote_config_poller.is_alive()
        finally:
            release.set()
            client.shutdown()


def test_empty_key_does_not_start():
    with patch("posthog.client._RemoteConfigPoller") as worker:
        client = Client(" ")
        try:
            worker.assert_not_called()
        finally:
            client.shutdown()


def test_default_interval_and_module_option(monkeypatch):
    with patch("posthog.client._RemoteConfigPoller") as worker:
        client = Client("phc_test", sync_mode=True)
        try:
            assert client.remote_config_poll_interval_seconds == 300
            assert worker.call_args.args[2] == 300
            worker.return_value.start.assert_called_once()
            is_enabled = worker.call_args.kwargs["is_enabled"]
            assert is_enabled()
            client.disabled = True
            assert not is_enabled()
            client.disabled = False
            client.send = False
            assert not is_enabled()
        finally:
            client.shutdown()
        worker.return_value.stop.assert_called_once()
    monkeypatch.setattr(posthog, "default_client", None)
    monkeypatch.setattr(posthog, "api_key", "phc_test")
    monkeypatch.setattr(posthog, "remote_config_poll_interval_seconds", None)
    posthog.flush()
    try:
        assert posthog.default_client.remote_config_poll_interval_seconds is None
        assert posthog.default_client._remote_config_poller is None
    finally:
        posthog.shutdown()


@pytest.fixture
def server():
    replies = Queue()
    requests_seen = Queue()
    release = threading.Event()
    release.set()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.put((self.command, self.path, dict(self.headers)))
            release.wait(5)
            status, body = replies.get(timeout=5)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{httpd.server_port}/proxy",
            replies,
            requests_seen,
            release,
        )
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def wait_for_config(worker, expected):
    deadline = time.monotonic() + 5
    while worker._config != expected and time.monotonic() < deadline:
        time.sleep(0.005)
    assert worker._config == expected


@pytest.mark.parametrize("sync_mode", [False, True])
def test_startup_refresh_and_shutdown_with_http_server(server, sync_mode):
    host, replies, seen, release = server
    first = {
        "hasFeatureFlags": False,
        "errorTracking": True,
        "futureSetting": 1,
        "sdkDiagnosticsEnabled": True,
    }
    last = {"surveys": False, "sdkDiagnosticsEnabled": False}
    for status, body in [
        (200, json.dumps(first).encode()),
        (503, b"unavailable"),
        (200, b"invalid json"),
        (200, b"[]"),
        (200, json.dumps(last).encode()),
    ]:
        replies.put((status, body))
    release.clear()
    client = Client(
        "phc_test",
        host=host,
        sync_mode=sync_mode,
        remote_config_poll_interval_seconds=0.1,
        timeout=1,
    )
    worker = client._remote_config_poller
    try:
        # The constructor returned while the startup response is still blocked.
        method, path, headers = seen.get(timeout=5)
        assert (method, path) == ("GET", "/proxy/array/phc_test/config")
        assert "Authorization" not in headers
        assert headers.get("Content-Length", "0") == "0"
        assert worker._config is None
        assert client._sdk_diagnostics_enabled is False
        release.set()
        wait_for_config(worker, first)
        assert client._sdk_diagnostics_enabled is True
        for _ in range(3):
            seen.get(timeout=5)
            assert worker._config == first
            assert client._sdk_diagnostics_enabled is True
        seen.get(timeout=5)
        wait_for_config(worker, last)
        assert client._sdk_diagnostics_enabled is False
        assert client.enable_exception_autocapture is False
        assert client._feature_flags is None
    finally:
        release.set()
        client.shutdown()
    assert not worker.is_alive()
    assert seen.empty()


def test_polling_skips_requests_while_disabled():
    worker = _RemoteConfigPoller(
        "phc_test",
        "https://proxy.example",
        300,
        3,
        is_enabled=Mock(side_effect=[True, False, True]),
    )
    worker.stopped = Mock()
    worker.stopped.is_set.return_value = False
    worker.stopped.wait.side_effect = [False, False, True]
    with patch("posthog._remote_config._fetch_remote_config", return_value={}) as fetch:
        worker.run()
    assert fetch.call_count == 2


def test_startup_failure_recovers_on_next_interval():
    worker = _RemoteConfigPoller("phc_test", "https://proxy.example", 300, 3)
    worker.stopped = Mock()
    worker.stopped.is_set.return_value = False
    worker.stopped.wait.side_effect = [False, True]
    with patch(
        "posthog._remote_config._fetch_remote_config",
        side_effect=[requests.ConnectionError(), {"recovered": True}],
    ):
        worker.run()
    assert worker._config == {"recovered": True}


def test_join_waits_for_inflight_fetch_without_publishing_after_stop():
    entered = threading.Event()
    release = threading.Event()

    def fetch(*args):
        entered.set()
        assert release.wait(5)
        return {"late": True}

    with patch("posthog._remote_config._fetch_remote_config", side_effect=fetch):
        client = Client("phc_test", sync_mode=True)
        worker = client._remote_config_poller
        cleanup = threading.Thread(target=client.join)
        try:
            assert entered.wait(5)
            cleanup.start()
            assert worker.stopped.wait(5)
            assert cleanup.is_alive()
            release.set()
            cleanup.join(5)
            assert not cleanup.is_alive()
            assert not worker.is_alive()
            assert worker._config is None
        finally:
            release.set()
            client.shutdown()


def test_atexit_signals_stop_without_waiting_for_request():
    entered = threading.Event()
    release = threading.Event()

    def fetch(*args):
        entered.set()
        assert release.wait(5)
        return {}

    with patch("posthog._remote_config._fetch_remote_config", side_effect=fetch):
        client = Client("phc_test", sync_mode=True)
        try:
            assert entered.wait(5)
            client._atexit()
            assert client._remote_config_poller.stopped.is_set()
            assert client._remote_config_poller.is_alive()
        finally:
            release.set()
            client.shutdown()


def test_fork_recreates_worker_and_terminal_client_stays_stopped():
    with patch("posthog.client._RemoteConfigPoller") as worker:
        client = Client("phc_test", sync_mode=True, enable_local_evaluation=False)
        try:
            client._reinit_after_fork()
            assert worker.call_count == 2
            client.shutdown()
            client._reinit_after_fork()
            assert worker.call_count == 2
            assert client._remote_config_poller is None
        finally:
            client.shutdown()
