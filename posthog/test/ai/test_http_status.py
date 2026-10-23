"""Failed provider calls are recorded with the HTTP status of the SDK error."""

from unittest.mock import patch

import pytest

from posthog.ai.utils import (
    call_llm_and_track_usage,
    call_llm_and_track_usage_async,
    _get_http_status,
)


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        yield mock_client


class _WithStatusCode(Exception):
    status_code = 503


class _WithCode(Exception):
    def __init__(self, code):
        super().__init__("boom")
        self.code = code


def test_get_http_status():
    assert _get_http_status(_WithStatusCode()) == 503
    assert _get_http_status(_WithCode(429)) == 429
    # A `code` that is not an HTTP status is not one
    assert _get_http_status(_WithCode(2)) == 0
    assert _get_http_status(_WithCode("RESOURCE_EXHAUSTED")) == 0
    assert _get_http_status(RuntimeError("no status")) == 0


def _genai_errors():
    errors = pytest.importorskip("google.genai.errors")
    return errors


def _gemini_error(errors, cls, code):
    return cls(code, {"error": {"code": code, "message": "nope", "status": "ERROR"}})


@pytest.mark.parametrize(
    ("cls_name", "code"), [("ClientError", 429), ("ServerError", 503)]
)
def test_gemini_api_error_status_is_recorded(mock_client, cls_name, code):
    errors = _genai_errors()
    err = _gemini_error(errors, getattr(errors, cls_name), code)

    def boom(**kwargs):
        raise err

    with pytest.raises(getattr(errors, cls_name)):
        call_llm_and_track_usage(
            "user",
            mock_client,
            "gemini",
            None,
            None,
            False,
            None,
            "https://generativelanguage.googleapis.com",
            boom,
            model="gemini-3.8-flash",
            contents="hi",
        )

    props = mock_client.capture.call_args[1]["properties"]
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == code


@pytest.mark.asyncio
async def test_gemini_api_error_status_is_recorded_async(mock_client):
    errors = _genai_errors()
    err = _gemini_error(errors, errors.ClientError, 429)

    async def boom(**kwargs):
        raise err

    with pytest.raises(errors.ClientError):
        await call_llm_and_track_usage_async(
            "user",
            mock_client,
            "gemini",
            None,
            None,
            False,
            None,
            "https://generativelanguage.googleapis.com",
            boom,
            model="gemini-3.8-flash",
            contents="hi",
        )

    props = mock_client.capture.call_args[1]["properties"]
    assert props["$ai_http_status"] == 429


def test_status_code_errors_keep_their_status(mock_client):
    def boom(**kwargs):
        raise _WithStatusCode("overloaded")

    with pytest.raises(_WithStatusCode):
        call_llm_and_track_usage(
            "user",
            mock_client,
            "openai",
            None,
            None,
            False,
            None,
            "https://x",
            boom,
            model="m",
        )

    assert mock_client.capture.call_args[1]["properties"]["$ai_http_status"] == 503
