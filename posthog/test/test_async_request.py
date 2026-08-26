from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from unittest import mock

import pytest

from posthog._async_request import (
    _build_client,
    _process_response,
    async_batch_post,
    async_flags,
    async_remote_config,
)
from posthog.request import APIError


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.headers = {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, response=None):
        self.responses = (
            list(response)
            if isinstance(response, list)
            else [response or FakeResponse()]
        )
        self.calls = []
        self.closed = False

    async def post(self, *args, **kwargs):
        self.calls.append(("post", args, kwargs))
        return self.responses.pop(0)

    async def get(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return self.responses.pop(0)

    async def aclose(self):
        self.closed = True


def test_import_posthog_does_not_require_async_extra():
    script = """
import builtins
original_import = builtins.__import__
def blocked_import(name, *args, **kwargs):
    if name == 'httpx' or name.startswith('httpx.'):
        raise ImportError('httpx intentionally unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = blocked_import
import posthog
assert posthog.Client
assert posthog.Posthog
assert posthog.AsyncClient
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_build_client_scopes_requests_to_host_and_follows_redirects():
    with mock.patch("posthog._async_request.httpx.AsyncClient") as async_client:
        _build_client("https://example.com/")
    async_client.assert_called_once_with(
        base_url="https://example.com", follow_redirects=True
    )


@pytest.mark.asyncio
async def test_async_batch_post_uses_relative_path_and_sanitized_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="posthog")
    client = FakeAsyncClient()

    await async_batch_post(
        "test-secret-key",
        "https://example.com",
        batch=[{"properties": {"password": "super-secret"}}],
        path="/batch/",
        client=client,
    )

    assert client.calls[0][1] == ("/batch/",)
    assert "super-secret" not in caplog.text
    assert "test-secret-key" not in caplog.text
    assert "https://example.com" not in caplog.text


@pytest.mark.asyncio
async def test_async_batch_post_serializes_off_event_loop():
    client = FakeAsyncClient()
    real_to_thread = asyncio.to_thread

    with mock.patch(
        "posthog._async_request.asyncio.to_thread", wraps=real_to_thread
    ) as to_thread:
        await async_batch_post(
            "test-key",
            "https://example.com",
            batch=[{"event": "event"}],
            path="/batch/",
            gzip=True,
            client=client,
        )

    to_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_batch_post_rejects_absolute_request_path():
    with pytest.raises(ValueError, match="relative"):
        await async_batch_post(
            "test-key",
            "https://example.com",
            batch=[],
            path="https://attacker.example/batch/",
            client=FakeAsyncClient(),
        )


@pytest.mark.asyncio
async def test_async_flags_sends_v2_request_payload():
    client = FakeAsyncClient(FakeResponse(200, {"flags": {}}))

    result = await async_flags(
        "project-key",
        "https://example.com",
        timeout=3,
        max_retries=1,
        client=client,
        distinct_id="user-1",
        groups={"company": "company-1"},
    )

    assert result == {"flags": {}}
    _, args, kwargs = client.calls[0]
    assert args == ("/flags/?v=2",)
    payload = json.loads(kwargs["content"])
    assert payload["api_key"] == "project-key"
    assert payload["distinct_id"] == "user-1"
    assert payload["groups"] == {"company": "company-1"}
    assert "sent_at" in payload
    assert "Authorization" not in kwargs["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 504])
async def test_async_flags_retries_only_contract_statuses(status):
    client = FakeAsyncClient(
        [
            FakeResponse(status, {"detail": "temporary"}),
            FakeResponse(200, {"flags": {}}),
        ]
    )

    with mock.patch("posthog._async_request.asyncio.sleep") as sleep:
        await async_flags(
            "project-key",
            "https://example.com",
            timeout=3,
            max_retries=1,
            client=client,
            distinct_id="user-1",
        )

    assert len(client.calls) == 2
    sleep.assert_awaited_once_with(0.3)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 500, 503])
async def test_async_flags_does_not_retry_other_http_errors(status):
    client = FakeAsyncClient(FakeResponse(status, {"detail": "terminal"}))

    with pytest.raises(APIError):
        await async_flags(
            "project-key",
            "https://example.com",
            timeout=3,
            max_retries=2,
            client=client,
            distinct_id="user-1",
        )

    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_async_remote_config_uses_bearer_auth_and_encodes_flag_key():
    client = FakeAsyncClient(FakeResponse(200, {"color": "blue"}))

    result = await async_remote_config(
        "secret-key",
        "project-key",
        "https://example.com",
        "flag/with spaces?",
        timeout=3,
        client=client,
    )

    assert result == {"color": "blue"}
    method, args, kwargs = client.calls[0]
    assert method == "get"
    assert args == (
        "/api/projects/@current/feature_flags/flag%2Fwith%20spaces%3F/remote_config",
    )
    assert kwargs["params"] == {"token": "project-key"}
    assert kwargs["headers"]["Authorization"] == "Bearer secret-key"


def test_process_response_raises_api_error_without_logging_payload(caplog):
    caplog.set_level(logging.DEBUG, logger="posthog")
    response = FakeResponse(400, {"detail": "password=secret"})

    with pytest.raises(APIError):
        _process_response(response)

    assert "password=secret" not in caplog.text
