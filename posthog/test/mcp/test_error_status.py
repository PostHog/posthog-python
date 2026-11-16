"""``$mcp_error_status``: the HTTP status behind a failed call, read off the
raised exception's cause chain (posthog-python#1223, sdk-specs mcp-analytics
"Failure capture")."""

from http import HTTPStatus
from types import SimpleNamespace

import httpx
import pytest

from posthog.mcp import PostHogMCP
from posthog.mcp.constants import PostHogMCPAnalyticsProperty as P
from posthog.test.mcp._helpers import events_named, flush_background


class Failure(Exception):
    def __init__(self, message="failed", **attributes):
        super().__init__(message)
        for name, value in attributes.items():
            setattr(self, name, value)


def _http_status_error(status):
    request = httpx.Request("GET", "https://api.example.com/invoices/1")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"{status} from upstream", request=request, response=response
    )


def _raised_from(outer, inner):
    try:
        try:
            raise inner
        except Exception as error:
            raise outer from error
    except Exception as error:
        return error


def _raised_during(outer, inner):
    try:
        try:
            raise inner
        except Exception:
            raise outer
    except Exception as error:
        return error


def _raised_from_none(outer, inner):
    try:
        try:
            raise inner
        except Exception:
            raise outer from None
    except Exception as error:
        return error


def _cause_without_suppressed_context():
    error = _raised_during(RuntimeError("lookup failed"), Failure(status=500))
    error.__cause__ = Failure(status=404)
    error.__suppress_context__ = False
    return error


def _chain_of(length, status):
    error = Failure("innermost", status=status)
    for _ in range(length - 1):
        error = _raised_from(Failure("wrapper"), error)
    return error


class UnreadableResponse(Exception):
    @property
    def response(self):
        raise RuntimeError("response not loaded")


def _cycle():
    first, second = Failure("first"), Failure("second")
    first.__cause__, second.__cause__ = second, first
    return first


@pytest.mark.parametrize(
    "error, status",
    [
        (_http_status_error(503), 503),
        (_raised_from(RuntimeError("lookup failed"), _http_status_error(503)), 503),
        (_raised_during(RuntimeError("lookup failed"), _http_status_error(429)), 429),
        (
            _raised_from_none(RuntimeError("lookup failed"), _http_status_error(503)),
            None,
        ),
        (_raised_from(Failure(status_code=404), _http_status_error(503)), 404),
        (Failure(status_code=502), 502),
        (Failure(statusCode=502), 502),
        (Failure(status=502), 502),
        (Failure(status_code=None, status=502), 502),
        (Failure(response=SimpleNamespace(status=502)), 502),
        (Failure(status="failed", response=SimpleNamespace(status_code=401)), 401),
        (Failure(status=HTTPStatus.BAD_GATEWAY), 502),
        (Failure(status_code=42), None),
        (Failure(status_code=600), None),
        (Failure(status_code=True), None),
        (Failure(status_code="503"), None),
        (Failure(status_code=503.0), None),
        (Failure("no status here"), None),
        (_cause_without_suppressed_context(), 404),
        (_chain_of(50, 503), 503),
        (_chain_of(51, 503), None),
        (_cycle(), None),
        (UnreadableResponse("failed"), None),
        ("upstream returned 503", None),
    ],
)
async def test_failed_call_carries_the_status_from_the_cause_chain(error, status):
    client = PostHogMCP("phc_test")
    captured = []
    client.capture = lambda event, **kw: captured.append({"event": event, **kw})

    client.capture_tool_call("fetch_invoice", is_error=True, error=error)
    await flush_background()

    [call] = events_named(captured, "$mcp_tool_call")
    assert call["properties"].get(P.ERROR_STATUS) == status


async def test_explicit_status_wins_over_the_derived_one():
    client = PostHogMCP("phc_test")
    captured = []
    client.capture = lambda event, **kw: captured.append({"event": event, **kw})

    client.capture_tool_call(
        "fetch_invoice",
        is_error=True,
        error=_http_status_error(503),
        properties={P.ERROR_STATUS: 418},
    )
    await flush_background()

    [call] = events_named(captured, "$mcp_tool_call")
    assert call["properties"][P.ERROR_STATUS] == 418
