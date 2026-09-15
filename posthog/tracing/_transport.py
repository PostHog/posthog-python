"""HTTP transport for span batches: one POST to ``/i/v1/traces`` per batch."""

import gzip
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import requests

from ..request import USER_AGENT, _get_session
from ..utils import remove_trailing_slash

log = logging.getLogger("posthog")

TRACES_PATH = "/i/v1/traces"

# PostHog's hosted ingestion accepts 10 MiB (measured decompressed); a larger
# body can only come back 413, so it is refused without a request. A proxy or
# self-hosted deployment enforcing less is covered by the 413 path.
OTLP_MAX_BODY_BYTES = 10 * 1024 * 1024

_RETRIABLE_STATUSES = frozenset({408, 429})

_DELTA_SECONDS_RE = re.compile(r"^\d+$")
_NUMERIC_RE = re.compile(r"^[+-]?[\d.]+$")


@dataclass(frozen=True)
class SendOutcome:
    """How one export attempt went: ``ok``, ``retry-later``, ``too-large`` or ``fatal``."""

    kind: str
    retry_after: Optional[float] = None
    # Too large by the SDK's own measure, so no request was spent (too-large only).
    measured_locally: bool = False


OK = SendOutcome("ok")
TOO_LARGE = SendOutcome("too-large")
TOO_LARGE_LOCALLY = SendOutcome("too-large", measured_locally=True)
FATAL = SendOutcome("fatal")


def parse_retry_after(value: Any, now: Optional[datetime] = None) -> Optional[float]:
    """``Retry-After`` as seconds from now; ``None`` when absent, malformed or not in the future.

    Accepts delta-seconds or an HTTP-date. A repeated header arrives joined as
    ``"60, 120"``; the first value is the outermost hop's.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if re.match(r"^\d+\s*,", raw):
        raw = raw.split(",", 1)[0].strip()
    if _DELTA_SECONDS_RE.match(raw):
        seconds = float(raw)
    elif _NUMERIC_RE.match(raw):
        return None
    else:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - (now or datetime.now(timezone.utc))).total_seconds()
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def send_traces_batch(client: Any, payload: dict) -> SendOutcome:
    """POST one OTLP batch with bearer auth and gzip, classifying the response.

    2xx is ok; 413 is too large; 408, 429, 5xx and transport errors are
    retriable; any other status is fatal. A 2xx is not proof of ingestion: an
    unknown but well-formed key is accepted and the spans dropped downstream.
    """
    if getattr(client, "disabled", False):
        return FATAL
    if not getattr(client, "send", True):
        return OK

    serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(serialized) > OTLP_MAX_BODY_BYTES:
        log.warning(
            "Span batch is %s bytes, over the %s byte ingestion limit; not sending it",
            len(serialized),
            OTLP_MAX_BODY_BYTES,
        )
        return TOO_LARGE_LOCALLY

    url = remove_trailing_slash(client.host) + TRACES_PATH
    timeout = getattr(client, "timeout", 15) or 15
    try:
        response = _get_session().post(
            url,
            data=gzip.compress(serialized),
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "Authorization": "Bearer {}".format(client.api_key),
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
            stream=True,
        )
    except requests.exceptions.RequestException as e:
        log.debug("Span batch request failed: %s", e)
        return SendOutcome("retry-later")
    # Status and headers alone classify the response, so the body is never
    # read: the timeout bounds read inactivity, and a body that keeps dripping
    # would otherwise hold the exporter's single flight open indefinitely.
    try:
        return _classify(response)
    finally:
        response.close()


def _classify(response: requests.Response) -> SendOutcome:
    status = response.status_code
    if status < 300:
        return OK
    if status == 413:
        return TOO_LARGE
    if status >= 500 or status in _RETRIABLE_STATUSES:
        try:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
        except Exception:
            retry_after = None
        return SendOutcome("retry-later", retry_after)
    log.error("Failed to send span batch: HTTP %s", status)
    return FATAL
