from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import requests

from posthog.ai.evaluations._errors import EvaluationAPIError
from posthog.ai.evaluations._transport import AsyncTransport, SyncTransport

PATH = "/api/projects/123/ai_observability/offline_experiments/"
BODY = b'{"id":"stable-id","name":"evaluation data"}'


def response(status=200, payload=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload if payload is not None else {}).encode()
    result.headers.update(headers or {})
    return result


@pytest.fixture
def transport():
    client = SyncTransport(123, "phx_test_secret")
    yield client
    client.close()


@pytest.mark.parametrize(
    "host,expected",
    [
        (None, "https://us.posthog.com"),
        ("https://app.posthog.com/", "https://us.posthog.com"),
        ("https://us.i.posthog.com", "https://us.posthog.com"),
        ("https://eu.i.posthog.com/", "https://eu.posthog.com"),
        ("https://eu.posthog.com", "https://eu.posthog.com"),
        ("http://localhost:8000/posthog/", "http://localhost:8000/posthog"),
        ("https://proxy.example.com/prefix", "https://proxy.example.com/prefix"),
    ],
)
def test_management_hosts_and_authenticated_request(host, expected):
    transport = SyncTransport(123, "phx_test_secret", host=host, timeout=7)
    with patch.object(transport._session, "send", return_value=response()) as send:
        assert transport.request("POST", PATH, body=BODY) == {}
    sent = send.call_args.args[0]
    assert sent.url == expected + PATH
    assert sent.headers["Authorization"] == "Bearer phx_test_secret"
    assert sent.headers["Content-Type"] == "application/json"
    assert sent.headers["User-Agent"].startswith("posthog-python/")
    assert sent.body == BODY
    assert send.call_args.kwargs["allow_redirects"] is False
    assert send.call_args.kwargs["timeout"] == 7
    assert transport._session.adapters["https://"].max_retries.total == 0
    transport.close()


@pytest.mark.parametrize(
    "options",
    [
        {"project_id": 0},
        {"project_id": True},
        {"secret_key": ""},
        {"secret_key": "with\nnewline"},
        {"host": "example.com"},
        {"host": "https://user:password@example.com"},
        {"host": "https://example.com?key=secret"},
        {"timeout": 0},
        {"timeout": float("inf")},
        {"max_retries": -1},
        {"max_retries": True},
    ],
)
@pytest.mark.parametrize("transport_class", [SyncTransport, AsyncTransport])
def test_invalid_configuration_fails_before_io(options, transport_class):
    with pytest.raises(ValueError):
        transport_class(**{"project_id": 123, "secret_key": "secret", **options})


def test_lost_response_retries_identical_body_and_identity(transport):
    with (
        patch.object(
            transport._session,
            "request",
            side_effect=[
                requests.ReadTimeout("secret message"),
                response(201, {"id": "stable-id"}),
            ],
        ) as request,
        patch("posthog.ai.evaluations._transport.time.sleep") as sleep,
    ):
        assert transport.request("POST", PATH, body=BODY) == {"id": "stable-id"}
    assert request.call_count == 2
    assert request.call_args_list[0] == request.call_args_list[1]
    sleep.assert_called_once()


@pytest.mark.parametrize("failure", [requests.ReadTimeout(), response(503)])
def test_non_idempotent_mutation_never_replays(transport, failure):
    with (
        patch.object(transport._session, "request", side_effect=[failure]) as request,
        patch("posthog.ai.evaluations._transport.time.sleep") as sleep,
        pytest.raises(EvaluationAPIError) as caught,
    ):
        transport.request("POST", PATH, body=BODY, retry_safe=False)
    assert request.call_count == 1
    assert caught.value.persistence == "unknown"
    sleep.assert_not_called()


def test_exhausted_transport_retry_budget_is_unknown_and_payload_free(transport):
    with (
        patch.object(
            transport._session,
            "request",
            side_effect=requests.ConnectionError("phx_test_secret evaluation data"),
        ) as request,
        patch("posthog.ai.evaluations._transport.time.sleep") as sleep,
        pytest.raises(EvaluationAPIError) as caught,
    ):
        transport.request("POST", PATH, body=BODY)
    assert request.call_count == 4
    assert sleep.call_count == 3
    assert caught.value.status is None
    assert caught.value.code == "transport_error"
    assert caught.value.persistence == "unknown"
    assert "phx_test_secret" not in repr(caught.value)
    assert "evaluation data" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_errors_retain_server_fields_and_conflict_counts_without_logging_them(
    transport,
):
    payload = {
        "code": "incomplete",
        "detail": "evaluation data and secret",
        "attr": "expected_result_count",
        "expected_result_count": 3,
        "result_count": 2,
        "errors": [{"attr": "items.0.payload", "detail": "private input"}],
    }
    with (
        patch.object(
            transport._session, "request", return_value=response(409, payload)
        ) as request,
        pytest.raises(EvaluationAPIError) as caught,
    ):
        transport.request("POST", PATH, body=BODY)
    error = caught.value
    assert request.call_count == 1
    assert error.status == 409
    assert error.code == "incomplete"
    assert error.detail == payload["detail"]
    assert error.attr == "expected_result_count"
    assert error.errors == payload["errors"]
    assert error.response == payload
    assert error.persistence == "rejected"
    assert "evaluation data" not in str(error)
    assert "private input" not in repr(error)


def test_rejection_after_lost_response_does_not_claim_definite_rejection(transport):
    with (
        patch.object(
            transport._session,
            "request",
            side_effect=[requests.ReadTimeout(), response(409, {"code": "conflict"})],
        ),
        patch("posthog.ai.evaluations._transport.time.sleep"),
        pytest.raises(EvaluationAPIError) as caught,
    ):
        transport.request("POST", PATH, body=BODY)
    assert caught.value.status == 409
    assert caught.value.persistence == "unknown"


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize("content", [b"not JSON", b"[]", b"null"])
def test_malformed_success_is_unknown_and_not_retried(transport, status, content):
    malformed = response(status)
    malformed._content = content
    with (
        patch.object(transport._session, "request", return_value=malformed) as request,
        pytest.raises(EvaluationAPIError) as caught,
    ):
        transport.request("POST", PATH, body=BODY)
    assert request.call_count == 1
    assert caught.value.code == "invalid_response"
    assert caught.value.persistence == "unknown"


@pytest.mark.parametrize("retry_after_format", ["seconds", "date"])
def test_retry_after_is_minimum_delay(transport, retry_after_format):
    retry_after = (
        "2"
        if retry_after_format == "seconds"
        else format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20))
    )
    with (
        patch.object(
            transport._session,
            "request",
            side_effect=[
                response(429, headers={"Retry-After": retry_after}),
                response(),
            ],
        ),
        patch("posthog.ai.evaluations._transport.time.sleep") as sleep,
    ):
        transport.request("POST", PATH, body=BODY)
    sleep.assert_called_once()
    assert 2 <= sleep.call_args.args[0] <= 30


def test_retry_after_longer_than_budget_surfaces_without_retrying_early(transport):
    with (
        patch.object(
            transport._session,
            "request",
            return_value=response(429, headers={"Retry-After": "3600"}),
        ) as request,
        patch("posthog.ai.evaluations._transport.time.sleep") as sleep,
        pytest.raises(EvaluationAPIError) as caught,
    ):
        transport.request("POST", PATH, body=BODY)
    assert request.call_count == 1
    assert caught.value.retry_after == 3600
    assert caught.value.persistence == "rejected"
    sleep.assert_not_called()


def test_redirect_is_never_followed(transport):
    with (
        patch.object(
            transport._session,
            "request",
            return_value=response(
                307, headers={"Location": "https://elsewhere.example"}
            ),
        ) as request,
        pytest.raises(EvaluationAPIError) as caught,
    ):
        transport.request("POST", PATH, body=BODY)
    assert request.call_count == 1
    assert request.call_args.kwargs["allow_redirects"] is False
    assert caught.value.status == 307


def test_closed_client_does_not_send(transport):
    transport.close()
    with (
        patch.object(transport._session, "request") as request,
        pytest.raises(EvaluationAPIError) as caught,
    ):
        transport.request("GET", PATH)
    assert caught.value.persistence == "not_sent"
    request.assert_not_called()


@pytest.mark.asyncio
async def test_async_native_request_uses_auth_host_and_preserves_bytes():
    seen = []

    async def handle(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout("phx_test_secret")
        return httpx.Response(201, json={"id": "stable-id"})

    real_client = httpx.AsyncClient
    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kwargs: real_client(
            transport=httpx.MockTransport(handle), **kwargs
        ),
    ) as factory:
        transport = AsyncTransport(
            123, "phx_test_secret", host="https://eu.i.posthog.com", timeout=9
        )
    with patch(
        "posthog.ai.evaluations._transport.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        assert await transport.request("POST", PATH, body=BODY) == {"id": "stable-id"}
    assert len(seen) == 2
    assert seen[0].content == seen[1].content == BODY
    assert str(seen[0].url) == "https://eu.posthog.com" + PATH
    assert seen[0].headers["Authorization"] == "Bearer phx_test_secret"
    assert factory.call_args.kwargs["follow_redirects"] is False
    assert factory.call_args.kwargs["timeout"] == 9
    sleep.assert_awaited_once()
    await transport.aclose()
    assert transport._client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("secret"), httpx.Response(503), asyncio.CancelledError()],
)
async def test_async_non_idempotent_failure_and_cancellation_never_replay(failure):
    transport = AsyncTransport(123, "phx_test_secret")
    with (
        patch.object(transport._client, "request", side_effect=[failure]) as request,
        patch(
            "posthog.ai.evaluations._transport.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        pytest.raises(
            asyncio.CancelledError
            if isinstance(failure, asyncio.CancelledError)
            else EvaluationAPIError
        ),
    ):
        await transport.request("POST", PATH, body=BODY, retry_safe=False)
    request.assert_awaited_once()
    sleep.assert_not_awaited()
    await transport.aclose()


@pytest.mark.asyncio
async def test_async_rate_limit_larger_than_wait_budget_surfaces():
    transport = AsyncTransport(123, "phx_test_secret")
    with (
        patch.object(
            transport._client,
            "request",
            return_value=httpx.Response(429, headers={"Retry-After": "60"}),
        ) as request,
        patch(
            "posthog.ai.evaluations._transport.asyncio.sleep", new_callable=AsyncMock
        ) as sleep,
        pytest.raises(EvaluationAPIError) as caught,
    ):
        await transport.request("POST", PATH, body=BODY)
    request.assert_awaited_once()
    sleep.assert_not_awaited()
    assert caught.value.status == 429
    assert caught.value.retry_after == 60
    await transport.aclose()


@pytest.mark.asyncio
async def test_async_closed_client_does_not_send():
    transport = AsyncTransport(123, "phx_test_secret")
    await transport.aclose()
    with (
        patch.object(transport._client, "request") as request,
        pytest.raises(EvaluationAPIError) as caught,
    ):
        await transport.request("GET", PATH)
    assert caught.value.persistence == "not_sent"
    request.assert_not_awaited()


def test_optional_httpx_is_required_only_for_async_construction():
    script = """
import sys
sys.modules['httpx'] = None
from posthog.ai.evaluations._transport import SyncTransport, AsyncTransport
client = SyncTransport(123, 'secret')
client.close()
try:
    AsyncTransport(123, 'secret')
except RuntimeError as error:
    assert 'posthog[async]' in str(error)
else:
    raise AssertionError('Expected optional dependency guidance')
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=15)


def test_error_snapshots_response_and_submission():
    response_data = {"errors": [{"attr": "input", "detail": "private input"}]}
    submission = {"id": "stable-id", "payload": {"input": "private input"}}
    error = EvaluationAPIError(response=response_data, submission=submission)
    response_data["errors"].clear()
    submission["payload"]["input"] = "changed"
    assert error.response["errors"] == [{"attr": "input", "detail": "private input"}]
    assert error.submission["payload"]["input"] == "private input"
    assert "private input" not in str(error)
