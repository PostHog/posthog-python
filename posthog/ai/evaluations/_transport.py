from __future__ import annotations

import asyncio
import importlib
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from ...version import VERSION
from ._errors import EvaluationAPIError

_MAX_RETRY_DELAY = 30.0
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_CLOUD_HOSTS = {
    "app.posthog.com": "us.posthog.com",
    "us.i.posthog.com": "us.posthog.com",
    "eu.i.posthog.com": "eu.posthog.com",
}


def _normalize_host(host: str | None) -> str:
    parsed = urlsplit((host or "https://us.posthog.com").strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("host must be an absolute HTTP(S) URL without credentials")
    # Accessing port also validates malformed port numbers before any I/O.
    netloc = parsed.netloc
    if parsed.port in {None, 443} and parsed.scheme == "https":
        netloc = _CLOUD_HOSTS.get(parsed.hostname, netloc)
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _validate_options(
    project_id: int, secret_key: str, timeout: float, max_retries: int
) -> None:
    if (
        isinstance(project_id, bool)
        or not isinstance(project_id, int)
        or project_id <= 0
    ):
        raise ValueError("project_id must be a positive integer")
    if (
        not isinstance(secret_key, str)
        or not secret_key
        or any(character.isspace() for character in secret_key)
    ):
        raise ValueError("secret_key must be a nonempty credential without whitespace")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite positive number of seconds")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
    ):
        raise ValueError("max_retries must be a nonnegative integer")


def _request_url(host: str, path: str) -> str:
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ValueError(
            "path must be an absolute API path without a query or fragment"
        )
    return host + path


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _retry_delay(
    error: EvaluationAPIError, attempt: int, max_retries: int, retry_safe: bool
) -> float | None:
    if (
        not retry_safe
        or attempt >= max_retries
        or (error.status is not None and error.status not in _RETRY_STATUSES)
        or error.code == "invalid_response"
    ):
        return None
    # A longer Retry-After must surface to the caller. Clamping it and retrying
    # early would break the server's rate limit contract.
    if error.retry_after is not None and error.retry_after > _MAX_RETRY_DELAY:
        return None
    backoff = min(_MAX_RETRY_DELAY, 0.5 * 2 ** min(attempt, 6))
    backoff = min(_MAX_RETRY_DELAY, backoff * random.uniform(0.8, 1.2))
    return max(backoff, error.retry_after or 0.0)


def _parse_response(
    status: int, payload: Any, retry_after: str | None
) -> dict[str, Any]:
    if 200 <= status < 300:
        if isinstance(payload, dict):
            return payload
        raise EvaluationAPIError(
            status=status,
            code="invalid_response",
            detail="Expected a JSON object in the server acknowledgment.",
        )
    response = payload if isinstance(payload, dict) else None
    fields = response or {}
    raise EvaluationAPIError(
        status=status,
        code=fields.get("code"),
        detail=fields.get("detail", payload),
        attr=fields.get("attr"),
        errors=fields.get("errors"),
        response=response,
        retry_after=_retry_after(retry_after),
        persistence="rejected" if 400 <= status < 500 and status != 408 else "unknown",
    )


class _BearerAuth(requests.auth.AuthBase):
    def __init__(self, secret_key: str) -> None:
        self._secret_key = secret_key

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        # Setting auth explicitly prevents requests from substituting .netrc
        # credentials for this client's API credential.
        request.headers["Authorization"] = f"Bearer {self._secret_key}"
        return request


class SyncTransport:
    def __init__(
        self,
        project_id: int,
        secret_key: str,
        host: str | None = None,
        timeout: float = 15,
        max_retries: int = 3,
    ) -> None:
        _validate_options(project_id, secret_key, timeout, max_retries)
        self._host = _normalize_host(host)
        self._timeout = timeout
        self._max_retries = max_retries
        self._closed = False
        self._session = requests.Session()
        self._session.auth = _BearerAuth(secret_key)
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"posthog-python/{VERSION}",
            }
        )
        self._session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
        self._session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        params: dict[str, str | int] | None = None,
        retry_safe: bool = True,
    ) -> dict[str, Any]:
        if self._closed:
            raise EvaluationAPIError(code="client_closed", persistence="not_sent")
        url = _request_url(self._host, path)
        had_unknown_attempt = False
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    data=body,
                    params=params,
                    timeout=self._timeout,
                    allow_redirects=False,
                )
                try:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    return _parse_response(
                        response.status_code,
                        payload,
                        response.headers.get("Retry-After"),
                    )
                finally:
                    response.close()
            except requests.exceptions.RequestException:
                error = EvaluationAPIError(code="transport_error")
            except EvaluationAPIError as exc:
                error = exc
            had_unknown_attempt = had_unknown_attempt or error.persistence == "unknown"
            if had_unknown_attempt:
                error.persistence = "unknown"
            delay = _retry_delay(error, attempt, self._max_retries, retry_safe)
            if delay is None:
                raise error from None
            time.sleep(delay)
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._closed = True
        self._session.close()


class AsyncTransport:
    def __init__(
        self,
        project_id: int,
        secret_key: str,
        host: str | None = None,
        timeout: float = 15,
        max_retries: int = 3,
    ) -> None:
        _validate_options(project_id, secret_key, timeout, max_retries)
        self._host = _normalize_host(host)
        try:
            self._httpx = importlib.import_module("httpx")
        except ImportError:
            raise RuntimeError(
                "Async evaluations require httpx. Install it with `posthog[async]`."
            ) from None
        self._max_retries = max_retries
        self._closed = False
        self._client = self._httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {secret_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"posthog-python/{VERSION}",
            },
            timeout=timeout,
            follow_redirects=False,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        params: dict[str, str | int] | None = None,
        retry_safe: bool = True,
    ) -> dict[str, Any]:
        if self._closed:
            raise EvaluationAPIError(code="client_closed", persistence="not_sent")
        url = _request_url(self._host, path)
        had_unknown_attempt = False
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(
                    method, url, content=body, params=params
                )
                try:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    return _parse_response(
                        response.status_code,
                        payload,
                        response.headers.get("Retry-After"),
                    )
                finally:
                    await response.aclose()
            except self._httpx.RequestError:
                error = EvaluationAPIError(code="transport_error")
            except EvaluationAPIError as exc:
                error = exc
            had_unknown_attempt = had_unknown_attempt or error.persistence == "unknown"
            if had_unknown_attempt:
                error.persistence = "unknown"
            delay = _retry_delay(error, attempt, self._max_retries, retry_safe)
            if delay is None:
                raise error from None
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self._closed = True
        await self._client.aclose()
