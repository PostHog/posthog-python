import logging
from unittest import mock

import httpx
import pytest
import requests
from requests.adapters import BaseAdapter

import posthog
from posthog.client import Client
from posthog.network_metrics import _mark_internal, _template_path
from posthog.request import _get_flags_session, _get_session

FAKE_API_KEY = "phc_test_key"

ORIGINAL_REQUESTS_SEND = requests.Session.send
ORIGINAL_HTTPX_SEND = httpx.Client.send
ORIGINAL_HTTPX_ASYNC_SEND = httpx.AsyncClient.send


def make_client(network=True):
    return Client(
        FAKE_API_KEY,
        host="https://us.example.com",
        sync_mode=True,
        metrics={"network": network},
    )


@pytest.fixture
def client():
    c = make_client()
    yield c
    c.shutdown()


@pytest.fixture
def recorded(client):
    with mock.patch.object(client.metrics, "histogram") as histogram:
        yield histogram


def recorded_attributes(histogram):
    return histogram.call_args.kwargs["attributes"]


class FakeAdapter(BaseAdapter):
    """Answers each send with the next canned status, or raises the given error."""

    def __init__(self, *statuses, error=None):
        super().__init__()
        self.statuses = list(statuses)
        self.error = error

    def send(self, request, **kwargs):
        if self.error is not None:
            raise self.error
        response = requests.Response()
        response.status_code = self.statuses.pop(0)
        response.request = request
        response.url = request.url
        response._content = b""
        response._content_consumed = True
        if response.status_code in (301, 302):
            response.headers["location"] = request.url + "next/"
        return response

    def close(self):
        pass


def session_with(adapter, scheme="https://"):
    session = requests.Session()
    session.mount(scheme, adapter)
    return session


class TestRequestsRecording:
    def test_records_a_duration_histogram_with_default_attributes(self, recorded):
        session_with(FakeAdapter(200)).get("https://api.example.com/users/42/orders")

        recorded.assert_called_once()
        name, duration_ms = recorded.call_args.args
        assert name == "http.client.request.duration"
        assert duration_ms >= 0
        assert recorded.call_args.kwargs["unit"] == "ms"
        assert recorded_attributes(recorded) == {
            "method": "GET",
            "host": "api.example.com",
            "path": "/users/:id/orders",
            "status_class": "2xx",
        }

    @pytest.mark.parametrize(
        "status,status_class",
        [(200, "2xx"), (304, "3xx"), (404, "4xx"), (503, "5xx")],
    )
    def test_status_class_groups_the_status_code(self, recorded, status, status_class):
        session_with(FakeAdapter(status)).get("https://api.example.com/")

        assert recorded_attributes(recorded)["status_class"] == status_class

    def test_a_failed_request_records_missing_and_still_raises(self, recorded):
        session = session_with(FakeAdapter(error=requests.ConnectionError("boom")))

        with pytest.raises(requests.ConnectionError):
            session.post("https://api.example.com/")

        assert recorded_attributes(recorded) == {
            "method": "POST",
            "host": "api.example.com",
            "path": "/",
            "status_class": "missing",
        }

    def test_a_redirected_request_is_recorded_once_with_its_final_status(
        self, recorded
    ):
        session_with(FakeAdapter(302, 200)).get("https://api.example.com/a/")

        recorded.assert_called_once()
        assert recorded_attributes(recorded)["status_class"] == "2xx"

    @pytest.mark.parametrize("scheme", ["file://", "ftp://"])
    def test_non_http_requests_are_not_recorded(self, recorded, scheme):
        session_with(FakeAdapter(200), scheme).get(scheme + "example.com/thing")

        recorded.assert_not_called()

    @pytest.mark.parametrize("get_sdk_session", [_get_session, _get_flags_session])
    def test_the_sdks_own_requests_are_not_recorded(self, recorded, get_sdk_session):
        session = get_sdk_session()
        sdk_adapter = session.get_adapter("https://us.example.com/")
        session.mount("https://", FakeAdapter(200))
        try:
            session.get("https://us.example.com/batch/")
        finally:
            session.mount("https://", sdk_adapter)

        recorded.assert_not_called()

    def test_a_marked_session_is_not_recorded(self, recorded):
        _mark_internal(session_with(FakeAdapter(200))).get("https://api.example.com/")

        recorded.assert_not_called()


class TestHttpxRecording:
    def test_records_sync_httpx_requests(self, recorded):
        transport = httpx.MockTransport(lambda request: httpx.Response(201))
        with httpx.Client(transport=transport) as http:
            http.post("https://api.example.com/items")

        assert recorded_attributes(recorded) == {
            "method": "POST",
            "host": "api.example.com",
            "path": "/items",
            "status_class": "2xx",
        }

    async def test_records_async_httpx_requests(self, recorded):
        transport = httpx.MockTransport(lambda request: httpx.Response(404))
        async with httpx.AsyncClient(transport=transport) as http:
            await http.get("https://api.example.com/items/9f8e7d6c5b4a")

        assert recorded_attributes(recorded) == {
            "method": "GET",
            "host": "api.example.com",
            "path": "/items/:id",
            "status_class": "4xx",
        }

    async def test_a_failed_async_request_records_missing_and_still_raises(
        self, recorded
    ):
        def fail(request):
            raise httpx.ConnectError("boom", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
            with pytest.raises(httpx.ConnectError):
                await http.get("https://api.example.com/")

        assert recorded_attributes(recorded)["status_class"] == "missing"

    async def test_a_marked_async_client_is_not_recorded(self, recorded):
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with _mark_internal(httpx.AsyncClient(transport=transport)) as http:
            await http.get("https://api.example.com/")

        recorded.assert_not_called()


class TestConfig:
    def test_a_string_name_is_used_for_every_request(self):
        client = make_client({"name": "outbound.duration"})
        try:
            with mock.patch.object(client.metrics, "histogram") as histogram:
                session_with(FakeAdapter(200)).get("https://api.example.com/")
        finally:
            client.shutdown()

        assert histogram.call_args.args[0] == "outbound.duration"

    def test_a_name_function_sees_the_request_and_can_skip_it(self):
        seen = []

        def name(request):
            seen.append(request)
            return None if request["method"] == "DELETE" else "kept"

        client = make_client({"name": name})
        try:
            with mock.patch.object(client.metrics, "histogram") as histogram:
                session = session_with(FakeAdapter(200, 200))
                session.delete("https://api.example.com/a")
                session.get("https://api.example.com/b?x=1")
        finally:
            client.shutdown()

        assert seen == [
            {"url": "https://api.example.com/a", "method": "DELETE"},
            {"url": "https://api.example.com/b?x=1", "method": "GET"},
        ]
        histogram.assert_called_once()
        assert histogram.call_args.args[0] == "kept"

    def test_attributes_merge_over_and_replace_the_defaults(self):
        def attributes(request, response):
            assert response["status"] == 200
            assert response["duration_ms"] >= 0
            return {"path": "/users/{id}", "team": "billing"}

        client = make_client({"attributes": attributes})
        try:
            with mock.patch.object(client.metrics, "histogram") as histogram:
                session_with(FakeAdapter(200)).get("https://api.example.com/users/7")
        finally:
            client.shutdown()

        assert recorded_attributes(histogram) == {
            "method": "GET",
            "host": "api.example.com",
            "path": "/users/{id}",
            "status_class": "2xx",
            "team": "billing",
        }

    def test_a_raising_attributes_function_is_logged_and_the_request_succeeds(
        self, caplog
    ):
        def attributes(request, response):
            raise ValueError("bad attributes")

        client = make_client({"attributes": attributes})
        try:
            with caplog.at_level(logging.WARNING, logger="posthog"):
                response = session_with(FakeAdapter(200)).get(
                    "https://api.example.com/"
                )
        finally:
            client.shutdown()

        assert response.status_code == 200
        assert "bad attributes" in caplog.text

    @pytest.mark.parametrize("network", ["yes", 1, {"name": 3}])
    def test_invalid_config_warns_and_uses_the_defaults(self, network, caplog):
        with caplog.at_level(logging.WARNING, logger="posthog"):
            client = make_client(network)
        try:
            with mock.patch.object(client.metrics, "histogram") as histogram:
                session_with(FakeAdapter(200)).get("https://api.example.com/")
        finally:
            client.shutdown()

        assert "network" in caplog.text
        assert histogram.call_args.args[0] == "http.client.request.duration"


class TestLifecycle:
    @pytest.mark.parametrize("network", [None, False, {}])
    def test_nothing_is_wrapped_while_network_metrics_are_off(self, network):
        client = Client(FAKE_API_KEY, sync_mode=True, metrics={"network": network})
        try:
            assert requests.Session.send is ORIGINAL_REQUESTS_SEND
            assert httpx.Client.send is ORIGINAL_HTTPX_SEND
            assert httpx.AsyncClient.send is ORIGINAL_HTTPX_ASYNC_SEND
        finally:
            client.shutdown()

    def test_wrappers_install_when_the_client_is_built_and_leave_on_shutdown(self):
        client = make_client()

        assert requests.Session.send is not ORIGINAL_REQUESTS_SEND
        assert httpx.Client.send is not ORIGINAL_HTTPX_SEND
        assert httpx.AsyncClient.send is not ORIGINAL_HTTPX_ASYNC_SEND

        client.shutdown()

        assert requests.Session.send is ORIGINAL_REQUESTS_SEND
        assert httpx.Client.send is ORIGINAL_HTTPX_SEND
        assert httpx.AsyncClient.send is ORIGINAL_HTTPX_ASYNC_SEND

    def test_a_wrapper_that_cannot_be_removed_passes_requests_through(self):
        client = make_client()
        histogram = mock.patch.object(client.metrics, "histogram").start()
        ours = requests.Session.send

        def layered(self, request, **kwargs):
            return ours(self, request, **kwargs)

        requests.Session.send = layered
        try:
            client.shutdown()
            assert requests.Session.send is layered

            response = session_with(FakeAdapter(200)).get("https://api.example.com/")

            assert response.status_code == 200
            histogram.assert_not_called()
        finally:
            mock.patch.stopall()
            requests.Session.send = ORIGINAL_REQUESTS_SEND

    def test_module_level_setup_installs_the_wrappers(self):
        saved = (
            posthog.default_client,
            posthog.api_key,
            posthog.host,
            posthog.sync_mode,
            posthog.metrics,
        )
        posthog.default_client = None
        posthog.api_key = FAKE_API_KEY
        posthog.host = "https://us.example.com"
        posthog.sync_mode = True
        posthog.metrics = {"network": True}
        try:
            posthog.setup()

            assert requests.Session.send is not ORIGINAL_REQUESTS_SEND

            posthog.shutdown()

            assert requests.Session.send is ORIGINAL_REQUESTS_SEND
        finally:
            (
                posthog.default_client,
                posthog.api_key,
                posthog.host,
                posthog.sync_mode,
                posthog.metrics,
            ) = saved


@pytest.mark.parametrize(
    "path,templated",
    [
        ("/", "/"),
        ("/users", "/users"),
        ("/users/123/orders/4", "/users/:id/orders/:id"),
        (
            "/items/3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "/items/:id",
        ),
        ("/items/9f8e7d6c", "/items/:id"),
        ("/items/abcdefgh", "/items/abcdefgh"),
        ("/orders/order-123", "/orders/order-123"),
        ("/files/38217.pdf", "/files/38217.pdf"),
    ],
)
def test_template_path_replaces_id_like_segments(path, templated):
    assert _template_path(path) == templated
