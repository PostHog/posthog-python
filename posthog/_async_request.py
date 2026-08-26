from __future__ import annotations

import asyncio
import json
import logging
import zlib
from datetime import datetime, timezone
from gzip import GzipFile
from io import BytesIO
from typing import Any, Optional

from .capture_compression import CaptureCompression
from .capture_v1 import _send_v1_batch
from .request import APIError, DatetimeSerializer, USER_AGENT, normalize_host
from .utils import remove_trailing_slash

try:  # pragma: no cover - exercised when the optional dependency is absent
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


def _require_httpx():
    if httpx is None:  # pragma: no cover
        raise RuntimeError(
            "Async PostHog support requires httpx. Install it with `posthog[async]`."
        )
    return httpx


def _build_client(host: Optional[str] = None):
    httpx_module = _require_httpx()
    base_url = remove_trailing_slash(normalize_host(host))
    return httpx_module.AsyncClient(base_url=base_url, follow_redirects=True)


def _serialize_v0_body(
    api_key: str, gzip_enabled: bool, body: dict[str, Any]
) -> tuple[str | bytes, dict[str, str]]:
    payload = {
        **body,
        "sent_at": datetime.now(tz=timezone.utc).isoformat(),
        "api_key": api_key,
    }
    serialized = json.dumps(payload, cls=DatetimeSerializer)
    data: str | bytes = serialized
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}

    if gzip_enabled:
        try:
            buf = BytesIO()
            with GzipFile(fileobj=buf, mode="w") as gz:
                gz.write(serialized.encode("utf-8"))
            data = buf.getvalue()
            headers["Content-Encoding"] = "gzip"
        except (OSError, zlib.error) as exc:
            logging.getLogger("posthog").warning(
                "failed to gzip async request body, sending uncompressed: %s", exc
            )

    return data, headers


def _parse_retry_after(response: Any) -> Optional[float]:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _process_response(response: Any) -> None:
    if response.status_code == 200:
        return

    retry_after = _parse_retry_after(response)
    try:
        payload = response.json()
        detail = payload["detail"]
    except (KeyError, TypeError, ValueError):
        detail = response.text
    raise APIError(response.status_code, detail, retry_after=retry_after)


async def async_batch_post(
    api_key: str,
    host: Optional[str],
    *,
    batch: list[dict[str, Any]],
    path: str,
    gzip: bool = False,
    timeout: int = 15,
    historical_migration: bool = False,
    client: Optional[Any] = None,
) -> None:
    """Post one legacy capture batch without blocking the event loop."""
    if not path.startswith("/") or "://" in path:
        raise ValueError("async capture paths must be relative")

    data, headers = await asyncio.to_thread(
        _serialize_v0_body,
        api_key,
        gzip,
        {
            "batch": batch,
            "historical_migration": historical_migration,
        },
    )

    owns_client = client is None
    http_client = client or _build_client(host)
    try:
        logging.getLogger("posthog").debug("making async capture request")
        response = await http_client.post(
            path, content=data, headers=headers, timeout=timeout
        )
        _process_response(response)
    finally:
        if owns_client:
            await http_client.aclose()


async def async_send_v1_batch(
    api_key: str,
    host: Optional[str],
    batch: list[dict[str, Any]],
    *,
    compression: CaptureCompression,
    timeout: int,
    max_retries: int,
    historical_migration: bool,
) -> None:
    """Run the existing capture-v1 submitter off-loop to preserve wire parity."""
    await asyncio.to_thread(
        _send_v1_batch,
        api_key,
        host,
        batch,
        compression=compression,
        timeout=timeout,
        max_retries=max_retries,
        historical_migration=historical_migration,
    )
