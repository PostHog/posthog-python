from __future__ import annotations

import logging

import pytest

from posthog._async_request import async_get, async_post, _process_async_response
from posthog.request import APIError


class FakeAsyncClient:
    async def post(self, *args, **kwargs):
        return FakeResponse(200, {"ok": True})


class FakeAsyncGetClient:
    async def get(self, *args, **kwargs):
        return FakeResponse(304, {})


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_async_post_debug_log_does_not_include_payload_or_api_key(caplog):
    caplog.set_level(logging.DEBUG, logger="posthog")

    await async_post(
        "test-secret-key",
        "https://example.com",
        "/batch/",
        client=FakeAsyncClient(),
        batch=[{"properties": {"password": "super-secret"}}],
    )

    logs = caplog.text
    assert "making async request" in logs
    assert "https://example.com/batch/" not in logs
    assert "test-secret-key" not in logs
    assert "super-secret" not in logs


@pytest.mark.asyncio
async def test_async_get_debug_log_does_not_include_url_tokens(caplog):
    caplog.set_level(logging.DEBUG, logger="posthog")

    response = await async_get(
        "personal-secret-key",
        "/api/projects/@current/feature_flags/flag/remote_config?token=project-secret-key",
        "https://example.com",
        client=FakeAsyncGetClient(),
    )

    assert response.not_modified is True
    logs = caplog.text
    assert "GET returned 304 Not Modified" in logs
    assert "project-secret-key" not in logs
    assert "personal-secret-key" not in logs
    assert "https://example.com" not in logs


def test_process_async_response_debug_log_does_not_include_response_payload(caplog):
    caplog.set_level(logging.DEBUG, logger="posthog")

    with pytest.raises(APIError):
        _process_async_response(FakeResponse(400, {"detail": "password=secret"}), "ok")

    logs = caplog.text
    assert "received response with status: 400" in logs
    assert "password=secret" not in logs
