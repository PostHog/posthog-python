import json
import logging
import secrets
from datetime import date, datetime
from gzip import GzipFile
from io import BytesIO
from typing import Any, Optional, Union
from uuid import uuid4

import requests
from dateutil.tz import tzutc
from urllib3.util.retry import Retry

from posthog.utils import remove_trailing_slash
from posthog.version import VERSION

# Retry on both connect and read errors
# by default read errors will only retry idempotent HTTP methods (so not POST)
adapter = requests.adapters.HTTPAdapter(
    max_retries=Retry(
        total=2,
        connect=2,
        read=2,
    )
)
_session = requests.sessions.Session()
_session.mount("https://", adapter)

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


def post(
    api_key: str,
    host: Optional[str] = None,
    path=None,
    gzip: bool = False,
    timeout: int = 15,
    **kwargs,
) -> requests.Response:
    """Post the `kwargs` to the API"""
    log = logging.getLogger("posthog")
    body = kwargs
    body["sentAt"] = datetime.now(tz=tzutc()).isoformat()
    url = remove_trailing_slash(host or DEFAULT_HOST) + path
    body["api_key"] = api_key
    data = json.dumps(body, cls=DatetimeSerializer)
    log.debug("making request: %s to url: %s", data, url)
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if gzip:
        headers["Content-Encoding"] = "gzip"
        buf = BytesIO()
        with GzipFile(fileobj=buf, mode="w") as gz:
            # 'data' was produced by json.dumps(),
            # whose default encoding is utf-8.
            gz.write(data.encode("utf-8"))
        data = buf.getvalue()

    res = _session.post(url, data=data, headers=headers, timeout=timeout)

    if res.status_code == 200:
        log.debug("data uploaded successfully")

    return res


def _process_response(
    res: requests.Response, success_message: str, *, return_json: bool = True
) -> Union[requests.Response, Any]:
    log = logging.getLogger("posthog")
    if res.status_code == 200:
        log.debug(success_message)
        response = res.json() if return_json else res
        # Handle quota limited decide responses by raising a specific error
        # NB: other services also put entries into the quotaLimited key, but right now we only care about feature flags
        # since most of the other services handle quota limiting in other places in the application.
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
    try:
        payload = res.json()
        log.debug("received response: %s", payload)
        raise APIError(res.status_code, payload["detail"])
    except (KeyError, ValueError):
        raise APIError(res.status_code, res.text)


def decide(
    api_key: str,
    host: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    **kwargs,
) -> Any:
    """Post the `kwargs to the decide API endpoint"""
    res = post(api_key, host, "/decide/?v=4", gzip, timeout, **kwargs)
    return _process_response(res, success_message="Feature flags decided successfully")


def flags(
    api_key: str,
    host: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    **kwargs,
) -> Any:
    """Post the `kwargs to the flags API endpoint"""
    res = post(api_key, host, "/flags/?v=2", gzip, timeout, **kwargs)
    return _process_response(
        res, success_message="Feature flags evaluated successfully"
    )


def remote_config(
    personal_api_key: str,
    project_api_key: str,
    host: Optional[str] = None,
    key: str = "",
    timeout: int = 15,
) -> Any:
    """Get remote config flag value from remote_config API endpoint"""
    return get(
        personal_api_key,
        f"/api/projects/@current/feature_flags/{key}/remote_config?token={project_api_key}",
        host,
        timeout,
    )


def batch_post(
    api_key: str,
    host: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    **kwargs,
) -> requests.Response:
    """Post the `kwargs` to the batch API endpoint for events"""
    res = post(api_key, host, "/batch/", gzip, timeout, **kwargs)
    return _process_response(
        res, success_message="data uploaded successfully", return_json=False
    )


def get(
    api_key: str, url: str, host: Optional[str] = None, timeout: Optional[int] = None
) -> requests.Response:
    url = remove_trailing_slash(host or DEFAULT_HOST) + url
    res = requests.get(
        url,
        headers={"Authorization": "Bearer %s" % api_key, "User-Agent": USER_AGENT},
        timeout=timeout,
    )
    return _process_response(res, success_message=f"GET {url} completed successfully")


class APIError(Exception):
    def __init__(self, status: Union[int, str], message: str):
        self.message = message
        self.status = status

    def __str__(self):
        msg = "[PostHog] {0} ({1})"
        return msg.format(self.message, self.status)


class QuotaLimitError(APIError):
    pass


class DatetimeSerializer(json.JSONEncoder):
    def default(self, obj: Any):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()

        return json.JSONEncoder.default(self, obj)


def build_ai_multipart_request(
    event_name: str,
    distinct_id: str,
    properties: dict[str, Any],
    blobs: dict[str, Any],
    timestamp: Optional[str] = None,
    event_uuid: Optional[str] = None,
) -> tuple[bytes, str]:
    """
    Build a multipart/form-data request body for AI events.

    Args:
        event_name: The event name (e.g., "$ai_generation")
        distinct_id: The distinct ID for the event
        properties: Event properties (without blob properties)
        blobs: Dictionary of blob properties to include as separate parts
        timestamp: Optional timestamp for the event
        event_uuid: Optional UUID for the event

    Returns:
        Tuple of (body_bytes, boundary) for the multipart request

    Format follows the /i/v0/ai endpoint spec:
        Part 1: "event" - JSON with {uuid, event, distinct_id, timestamp}
        Part 2: "event.properties" - JSON with non-blob properties
        Part 3+: "event.properties.$ai_input" etc. - Blob data as JSON
    """
    # Generate a random boundary that's unlikely to appear in the data
    boundary = "----WebKitFormBoundary" + secrets.token_hex(16)

    # Ensure we have a UUID
    if event_uuid is None:
        event_uuid = str(uuid4())

    # Build the event part
    event_data = {
        "uuid": event_uuid,
        "event": event_name,
        "distinct_id": distinct_id,
    }
    if timestamp is not None:
        event_data["timestamp"] = timestamp

    # Build multipart body
    parts = []

    # Part 1: event
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="event"\r\n')
    parts.append(b"Content-Type: application/json\r\n\r\n")
    parts.append(json.dumps(event_data, cls=DatetimeSerializer).encode("utf-8"))
    parts.append(b"\r\n")

    # Part 2: event.properties
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="event.properties"\r\n')
    parts.append(b"Content-Type: application/json\r\n\r\n")
    parts.append(json.dumps(properties, cls=DatetimeSerializer).encode("utf-8"))
    parts.append(b"\r\n")

    # Part 3+: blob parts
    for blob_name, blob_value in blobs.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="event.properties.{blob_name}"\r\n'.encode()
        )
        parts.append(b"Content-Type: application/json\r\n\r\n")
        parts.append(json.dumps(blob_value, cls=DatetimeSerializer).encode("utf-8"))
        parts.append(b"\r\n")

    # Final boundary
    parts.append(f"--{boundary}--\r\n".encode())

    # Combine all parts
    body = b"".join(parts)

    return body, boundary


def ai_post(
    api_key: str,
    host: Optional[str] = None,
    gzip: bool = False,
    timeout: int = 15,
    **kwargs,
) -> requests.Response:
    """
    Post an AI event to the /i/v0/ai endpoint using multipart/form-data.

    Args:
        api_key: The PostHog API key
        host: The host to post to
        gzip: Whether to gzip compress the request
        timeout: Request timeout in seconds
        **kwargs: Event parameters including event_name, distinct_id, properties, blobs, etc.

    Returns:
        The response from the server

    Raises:
        APIError: If the request fails
    """
    log = logging.getLogger("posthog")

    # Extract event parameters
    event_name = kwargs.get("event_name")
    distinct_id = kwargs.get("distinct_id")
    properties = kwargs.get("properties", {})
    blobs = kwargs.get("blobs", {})
    timestamp = kwargs.get("timestamp")
    event_uuid = kwargs.get("uuid")

    # Build multipart request
    body, boundary = build_ai_multipart_request(
        event_name=event_name,
        distinct_id=distinct_id,
        properties=properties,
        blobs=blobs,
        timestamp=timestamp,
        event_uuid=event_uuid,
    )

    # Optionally gzip compress the body if enabled and body is large enough
    # Spec recommends compression for requests > 10KB
    data = body
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
    }

    if gzip or len(body) > 10 * 1024:  # Compress if gzip enabled or body > 10KB
        headers["Content-Encoding"] = "gzip"
        buf = BytesIO()
        with GzipFile(fileobj=buf, mode="w") as gz:
            gz.write(body)
        data = buf.getvalue()
        log.debug("Compressed AI event from %d bytes to %d bytes", len(body), len(data))

    url = remove_trailing_slash(host or DEFAULT_HOST) + "/i/v0/ai"
    log.debug("Posting AI event to %s", url)
    log.debug(
        "Event: %s, Distinct ID: %s, Blobs: %s",
        event_name,
        distinct_id,
        list(blobs.keys()),
    )

    res = _session.post(url, data=data, headers=headers, timeout=timeout)

    if res.status_code == 200:
        log.debug("AI event uploaded successfully")
        return res

    # Handle errors
    try:
        payload = res.json()
        log.debug("Received error response: %s", payload)
        raise APIError(res.status_code, payload.get("detail", "Unknown error"))
    except (KeyError, ValueError):
        raise APIError(res.status_code, res.text)
