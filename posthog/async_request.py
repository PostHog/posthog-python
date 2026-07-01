import asyncio
import json
import logging
import zlib
from datetime import datetime, timezone
from gzip import GzipFile
from io import BytesIO
from typing import Any, Optional, cast

from .request import (
    EVENTS_ENDPOINT,
    RETRY_STATUS_FORCELIST,
    USER_AGENT,
    APIError,
    DatetimeSerializer,
    GetResponse,
    QuotaLimitError,
    _mask_tokens_in_url,
    normalize_host,
)
from .utils import remove_trailing_slash

try:  # pragma: no cover - exercised when the optional dependency is absent
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


_async_client: Optional["httpx.AsyncClient"] = None
_async_flags_client: Optional["httpx.AsyncClient"] = None
_pooling_enabled = True


def _require_httpx():
    if httpx is None:  # pragma: no cover
        raise RuntimeError(
            "Async PostHog support requires httpx. Install it with `posthog[async]`."
        )
    return httpx


def _build_client():
    httpx_module = _require_httpx()
    return httpx_module.AsyncClient()


def _build_flags_client():
    httpx_module = _require_httpx()
    return httpx_module.AsyncClient()


async def _get_client():
    global _async_client
    if not _pooling_enabled:
        return _build_client()
    if _async_client is None:
        _async_client = _build_client()
    return _async_client


async def _get_flags_client():
    global _async_flags_client
    if not _pooling_enabled:
        return _build_flags_client()
    if _async_flags_client is None:
        _async_flags_client = _build_flags_client()
    return _async_flags_client


async def close_async_clients() -> None:
    global _async_client, _async_flags_client
    clients = [_async_client, _async_flags_client]
    _async_client = None
    _async_flags_client = None
    for client in clients:
        if client is not None:
            await client.aclose()


async def async_post(
    api_key: str,
    host: Optional[str] = None,
    path: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    client: Optional[Any] = None,
    **kwargs,
) -> Any:
    """Post the kwargs to the API using an async HTTP client."""
    log = logging.getLogger("posthog")
    body = kwargs
    body["sent_at"] = datetime.now(tz=timezone.utc).isoformat()
    trimmed_host = remove_trailing_slash(normalize_host(host))
    url = trimmed_host + cast(str, path)
    body["api_key"] = api_key
    data: str | bytes = json.dumps(body, cls=DatetimeSerializer)
    log.debug("making async request to url: %s", url)
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if gzip:
        try:
            buf = BytesIO()
            with GzipFile(fileobj=buf, mode="w") as gz:
                gz.write(cast(str, data).encode("utf-8"))
            data = buf.getvalue()
            headers["Content-Encoding"] = "gzip"
        except (OSError, zlib.error) as exc:
            log.warning("failed to gzip request body, sending uncompressed: %s", exc)

    http_client = client or await _get_client()
    res = await http_client.post(url, data=data, headers=headers, timeout=timeout)

    if res.status_code == 200:
        log.debug("data uploaded successfully")

    return res


def _response_json(res: Any) -> Any:
    return res.json()


def _response_text(res: Any) -> str:
    return res.text


def _response_headers(res: Any) -> Any:
    return res.headers


def _process_async_response(
    res: Any, success_message: str, *, return_json: bool = True
) -> Any:
    log = logging.getLogger("posthog")
    if res.status_code == 200:
        log.debug(success_message)
        response = _response_json(res) if return_json else res
        if (
            isinstance(response, dict)
            and "quotaLimited" in response
            and isinstance(response["quotaLimited"], list)
            and "feature_flags" in response["quotaLimited"]
        ):
            log.warning(
                "[FEATURE FLAGS] PostHog feature flags quota limited, resetting feature flag data.  Learn more about billing limits at https://posthog.com/docs/billing/limits-alerts"
            )
            raise QuotaLimitError(res.status_code, "Feature flags quota limited")
        return response

    retry_after = None
    retry_after_header = _response_headers(res).get("Retry-After")
    if retry_after_header:
        try:
            retry_after = float(retry_after_header)
        except (ValueError, TypeError):
            try:
                from email.utils import parsedate_to_datetime

                retry_after = max(
                    0.0,
                    (
                        parsedate_to_datetime(retry_after_header)
                        - datetime.now(timezone.utc)
                    ).total_seconds(),
                )
            except (ValueError, TypeError):
                pass

    try:
        payload = _response_json(res)
        log.debug("received response with status: %s", res.status_code)
        raise APIError(res.status_code, payload["detail"], retry_after=retry_after)
    except (KeyError, ValueError):
        raise APIError(res.status_code, _response_text(res), retry_after=retry_after)


async def async_flags(
    api_key: str,
    host: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    **kwargs,
) -> Any:
    """Post to the flags API endpoint with async retries for transient failures."""
    httpx_module = _require_httpx()
    last_exc: Optional[BaseException] = None
    for attempt in range(3):
        try:
            res = await async_post(
                api_key,
                host,
                "/flags/?v=2",
                gzip,
                timeout,
                client=await _get_flags_client(),
                **kwargs,
            )
            if res.status_code in RETRY_STATUS_FORCELIST and attempt < 2:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            return _process_async_response(
                res, success_message="Feature flags evaluated successfully"
            )
        except (httpx_module.TimeoutException, httpx_module.NetworkError) as exc:
            last_exc = exc
            if attempt >= 2:
                raise
            await asyncio.sleep(0.5 * (2**attempt))
    if last_exc is not None:
        raise last_exc


async def async_batch_post(
    api_key: str,
    host: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    path: str = EVENTS_ENDPOINT,
    **kwargs,
) -> Any:
    """Post a batch of events using async HTTP."""
    res = await async_post(api_key, host, path, gzip, timeout, **kwargs)
    return _process_async_response(
        res, success_message="data uploaded successfully", return_json=False
    )


async def async_get(
    api_key: str,
    url: str,
    host: Optional[str] = None,
    timeout: Optional[int] = None,
    etag: Optional[str] = None,
) -> GetResponse:
    """Make an async GET request with optional ETag support."""
    log = logging.getLogger("posthog")
    trimmed_host = remove_trailing_slash(normalize_host(host))
    full_url = trimmed_host + url
    headers = {"Authorization": "Bearer %s" % api_key, "User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag

    res = await (await _get_client()).get(full_url, headers=headers, timeout=timeout)
    masked_url = _mask_tokens_in_url(full_url)

    if res.status_code == 304:
        log.debug(f"GET {masked_url} returned 304 Not Modified")
        response_etag = res.headers.get("ETag")
        return GetResponse(data=None, etag=response_etag or etag, not_modified=True)

    data = _process_async_response(
        res, success_message=f"GET {masked_url} completed successfully"
    )
    response_etag = res.headers.get("ETag")
    return GetResponse(data=data, etag=response_etag, not_modified=False)


async def async_remote_config(
    personal_api_key: str,
    project_api_key: str,
    host: Optional[str] = None,
    key: str = "",
    timeout: int = 15,
) -> Any:
    response = await async_get(
        personal_api_key,
        f"/api/projects/@current/feature_flags/{key}/remote_config?token={project_api_key}",
        host,
        timeout,
    )
    return response.data
