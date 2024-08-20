import json
import logging
from datetime import date, datetime
from gzip import GzipFile
from io import BytesIO
from typing import Any, Optional, Union

import requests
from dateutil.tz import tzutc

from posthog.utils import remove_trailing_slash
from posthog.version import VERSION
import aiohttp
import asyncio

_session = requests.sessions.Session()

US_INGESTION_ENDPOINT = "https://us.i.posthog.com"
EU_INGESTION_ENDPOINT = "https://eu.i.posthog.com"
DEFAULT_HOST = US_INGESTION_ENDPOINT
USER_AGENT = "posthog-python/" + VERSION


def determine_server_host(host: Optional[str]) -> str:
    """Determines the server host to use."""
    host_or_default = host or DEFAULT_HOST
    trimmed_host = remove_trailing_slash(host_or_default)
    if trimmed_host in ("https://app.posthog.com", "https://us.posthog.com"):
        return US_INGESTION_ENDPOINT
    elif trimmed_host == "https://eu.posthog.com":
        return EU_INGESTION_ENDPOINT
    else:
        return host_or_default


async def post(
    api_key: str,
    host: Optional[str] = None,
    path=None,
    gzip: bool = False,
    timeout: int = 15,
    **kwargs,
) -> aiohttp.ClientResponse:
    """Post the `kwargs` to the API asynchronously"""
    log = logging.getLogger("posthog")
    body = kwargs
    body["sentAt"] = datetime.utcnow().replace(tzinfo=tzutc()).isoformat()
    url = remove_trailing_slash(host or DEFAULT_HOST) + path
    body["api_key"] = api_key
    data = json.dumps(body, cls=DatetimeSerializer)
    log.debug("making request: %s", data)
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if gzip:
        headers["Content-Encoding"] = "gzip"
        buf = BytesIO()
        with GzipFile(fileobj=buf, mode="w") as gz:
            gz.write(data.encode("utf-8"))
        data = buf.getvalue()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, data=data, headers=headers, timeout=timeout
        ) as res:
            if res.status == 200:
                log.debug("data uploaded successfully")
            return res


async def _process_response(
    res: aiohttp.ClientResponse, success_message: str, *, return_json: bool = True
) -> Union[aiohttp.ClientResponse, Any]:
    log = logging.getLogger("posthog")
    if res.status == 200:
        log.debug(success_message)
        return await res.json() if return_json else res
    try:
        payload = await res.json()
        log.debug("received response: %s", payload)
        raise APIError(res.status, payload["detail"])
    except (KeyError, ValueError):
        raise APIError(res.status, await res.text())


async def decide(
    api_key: str,
    host: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    **kwargs,
) -> Any:
    """Post the `kwargs` to the decide API endpoint asynchronously"""
    res = await post(api_key, host, "/decide/?v=3", gzip, timeout, **kwargs)
    return await _process_response(
        res, success_message="Feature flags decided successfully"
    )


async def batch_post(
    api_key: str,
    host: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    **kwargs,
) -> aiohttp.ClientResponse:
    """Post the `kwargs` to the batch API endpoint for events asynchronously"""
    res = await post(api_key, host, "/batch/", gzip, timeout, **kwargs)
    return await _process_response(
        res, success_message="data uploaded successfully", return_json=False
    )


async def get(
    api_key: str, url: str, host: Optional[str] = None, timeout: Optional[int] = None
) -> Any:
    url = remove_trailing_slash(host or DEFAULT_HOST) + url
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
            timeout=timeout,
        ) as res:
            return await _process_response(
                res, success_message=f"GET {url} completed successfully"
            )


class APIError(Exception):
    def __init__(self, status: Union[int, str], message: str):
        self.message = message
        self.status = status

    def __str__(self):
        msg = "[PostHog] {0} ({1})"
        return msg.format(self.message, self.status)


class DatetimeSerializer(json.JSONEncoder):
    def default(self, obj: Any):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()

        return json.JSONEncoder.default(self, obj)
