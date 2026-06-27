"""Serialization and transport for the Capture V1 wire protocol.

This module owns everything specific to ``POST /i/v1/analytics/events``: the
*transform* layer (legacy-shaped queued message -> v1 wire event + batch
envelope) and the *transport* layer (a single HTTP attempt, response parsing,
and the partial-retry send loop).

The v1 contract (see ``rust/capture/src/v1/analytics/types.rs``) differs from
the legacy ``/batch/`` shape in a few load-bearing ways that this module
encodes:

- A typed ``options`` object carries a handful of sentinel properties, renamed
  and strictly typed. Wrong JSON types fail deserialization of the *whole
  batch*, so values are coerced to native types or omitted entirely.
- ``$set``/``$set_once`` have no top-level form in v1; the server reads them
  from ``properties``. The legacy ``set()``/``set_once()`` builders emit them at
  the top level, so they are relocated into ``properties`` here.
- ``$lib``/``$lib_version`` are injected server-side from the required
  ``PostHog-Sdk-Info`` header and are stripped from v1 properties.

The response is per-event: a 200 carries a ``results`` map keyed by event uuid,
each tagged ``ok``/``warning`` (terminal-success), ``drop`` (terminal-failure),
or ``retry``. :func:`send_v1_batch` resends only the ``retry`` events on the next
attempt (a stable ``PostHog-Request-Id`` and ``created_at`` across attempts; an
incrementing ``PostHog-Attempt``), logs drops, and raises a
:class:`CaptureV1Error` on batch-level/terminal failure or retry exhaustion so
the consumer's existing ``on_error(exc, batch)`` path fires unchanged.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Optional, cast
from uuid import uuid4

from posthog.request import (
    DatetimeSerializer,
    USER_AGENT,
    APIError,
    _get_session,
    gzip_compress,
    normalize_host,
)
from posthog.utils import guess_timezone as _guess_timezone, remove_trailing_slash

if TYPE_CHECKING:
    import requests

log = logging.getLogger("posthog")

CAPTURE_V1_PATH = "/i/v1/analytics/events"

# Required request/response headers for the v1 endpoint. Defined here as the
# single source of truth; the transport layer builds requests from them.
HEADER_SDK_INFO = "PostHog-Sdk-Info"
HEADER_ATTEMPT = "PostHog-Attempt"
HEADER_REQUEST_ID = "PostHog-Request-Id"
HEADER_REQUEST_TIMESTAMP = "PostHog-Request-Timestamp"

# Per-event result codes the backend emits (rust EventResult). `ok`/`warning`
# are terminal-success; `drop` terminal-failure; `retry` is safe to resend.
RESULT_OK = "ok"
RESULT_WARNING = "warning"
RESULT_DROP = "drop"
RESULT_RETRY = "retry"

# HTTP status classification. 429 is terminal in v1 (unlike v0, where it is
# retried) — the backend signals overload via retryable 5xx + Retry-After.
RETRYABLE_STATUSES = frozenset({408, 500, 502, 503, 504})
TERMINAL_STATUSES = frozenset({400, 401, 402, 413, 415, 429})

# Sentinel properties lifted to top-level string fields on the event.
_TOPLEVEL_SENTINELS: tuple[tuple[str, str], ...] = (
    ("$session_id", "session_id"),
    ("$window_id", "window_id"),
)

# Top-level legacy keys relocated into properties (v1 has no top-level form).
_RELOCATE_TO_PROPERTIES = ("$set", "$set_once")

# Properties dropped from v1 events (server injects them from PostHog-Sdk-Info).
_STRIP_FROM_PROPERTIES = ("$lib", "$lib_version")


def _coerce_bool(value: Any) -> Optional[bool]:
    """Coerce a sentinel value to ``bool`` using the backend's truthiness rules.

    Native bool passes through; ``"true"``/``"1"`` and ``"false"``/``"0"``
    (case-insensitive, trimmed) map to the obvious bool; any other numeric value
    is nonzero-truthy. Anything else returns ``None`` so the option is omitted
    rather than sent with a type the strict v1 schema would reject.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1"):
            return True
        if normalized in ("false", "0"):
            return False
        return None
    if isinstance(value, (int, float)):
        return value != 0
    return None


def _coerce_str(value: Any) -> Optional[str]:
    """Accept only ``str`` (the backend's ``product_tour_id`` is ``Option<String>``)."""
    return value if isinstance(value, str) else None


# Sentinel properties lifted into the typed `options` object: legacy property
# key, the backend's field name, and the coercer enforcing its strict type
# (wrong JSON types fail deserialization of the whole batch, so a value that
# won't coerce is omitted). The coercer is stored directly to keep the dispatch
# type-checked rather than keyed by a stringly-typed name.
_OPTION_SENTINELS: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
    ("$cookieless_mode", "cookieless_mode", _coerce_bool),
    ("$ignore_sent_at", "disable_skew_correction", _coerce_bool),
    ("$product_tour_id", "product_tour_id", _coerce_str),
    ("$process_person_profile", "process_person_profile", _coerce_bool),
)


def _v1_timestamp(timestamp: Any) -> str:
    """Return a timezone-aware RFC3339 timestamp string.

    Messages off the queue already carry an ISO-8601 string (``_enqueue`` runs
    ``guess_timezone(...).isoformat()``), so that is passed through. A
    ``datetime`` is normalized to timezone-aware and serialized; a missing value
    defaults to now in UTC. The v1 server parses strictly with
    ``DateTime::parse_from_rfc3339`` and rejects naive timestamps.
    """
    if timestamp is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(timestamp, datetime):
        return _guess_timezone(timestamp).isoformat()
    return timestamp


def to_v1_event(msg: dict) -> dict:
    """Transform a legacy-shaped queued message into a v1 wire event.

    Pure: the input ``msg`` is not mutated (a fresh ``properties`` dict is
    built), so it remains safe to keep the original for retries or callbacks.
    """
    properties = dict(msg.get("properties") or {})

    # Relocate top-level $set/$set_once into properties; v1 has no top-level
    # form. On the unusual collision where properties already carries the key,
    # the properties value wins.
    for key in _RELOCATE_TO_PROPERTIES:
        top_val = msg.get(key)
        if top_val is None:
            continue
        existing = properties.get(key)
        if isinstance(top_val, dict) and isinstance(existing, dict):
            properties[key] = {**top_val, **existing}
        elif key not in properties:
            properties[key] = top_val

    for key in _STRIP_FROM_PROPERTIES:
        properties.pop(key, None)

    options: dict[str, Any] = {}
    for prop_key, wire_key, coercer in _OPTION_SENTINELS:
        if prop_key not in properties:
            continue
        # Always removed from properties — these sentinels must never reach v1
        # backend properties — but only emitted as an option when coercible.
        coerced = coercer(properties.pop(prop_key))
        if coerced is not None:
            options[wire_key] = coerced

    top_level: dict[str, str] = {}
    for prop_key, field_name in _TOPLEVEL_SENTINELS:
        if prop_key not in properties:
            continue
        coerced_str = _coerce_str(properties.pop(prop_key))
        if coerced_str is not None:
            top_level[field_name] = coerced_str

    event = {
        "event": msg["event"],
        "uuid": msg["uuid"],
        "distinct_id": msg["distinct_id"],
        "timestamp": _v1_timestamp(msg.get("timestamp")),
        # Always a dict so it serializes as "{}" rather than null when empty.
        "options": options,
        "properties": properties,
    }
    event.update(top_level)
    return event


def build_v1_batch_body(events: list[dict], historical_migration: bool = False) -> dict:
    """Assemble the v1 batch envelope.

    Carries no ``api_key`` (Bearer auth) and no ``sent_at``.
    ``historical_migration`` is omitted when False (the server defaults it).
    """
    body: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "batch": events,
    }
    if historical_migration:
        body["historical_migration"] = True
    return body


@dataclass
class V1EventResult:
    """A single event's directive from a 2xx ``results`` map."""

    result: Optional[str]
    details: Optional[str] = None


@dataclass
class V1ParsedResponse:
    """Classified outcome of one v1 HTTP attempt.

    ``is_success`` is the 2xx classification. On success ``results`` holds the
    per-uuid directives (``None``/``malformed=True`` when the body could not be
    parsed — treated as terminal so a bad success never loops forever). On a
    non-2xx, ``error_message`` is the best-effort human-readable detail.
    """

    status_code: int
    is_success: bool
    retry_after: Optional[float] = None
    results: Optional[dict[str, V1EventResult]] = None
    malformed: bool = False
    error_message: str = ""


class CaptureV1Error(APIError):
    """Batch-level failure of a capture-v1 send.

    Subclasses :class:`APIError` so the consumer's existing ``on_error`` handling
    (which already inspects ``status``/``retry_after``) keeps working; the extra
    fields carry v1 specifics for richer logging/callbacks.
    """

    def __init__(
        self,
        status: int | str,
        message: str,
        *,
        retry_after: Optional[float] = None,
        request_id: Optional[str] = None,
        attempts: Optional[int] = None,
        retry_exhausted: Optional[list[str]] = None,
        drops: Optional[list[tuple[str, Optional[str]]]] = None,
    ):
        super().__init__(status, message, retry_after=retry_after)
        self.request_id = request_id
        self.attempts = attempts
        # uuids the server told us to retry but we never delivered (exhausted).
        self.retry_exhausted = retry_exhausted or []
        # (uuid, details) pairs the server told us to drop on a 2xx response.
        self.drops = drops or []


def _is_success_status(status: int) -> bool:
    return 200 <= status < 300


def _parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) to seconds."""
    if not header_value:
        return None
    try:
        return float(header_value)
    except (ValueError, TypeError):
        pass
    try:
        delta = parsedate_to_datetime(header_value) - datetime.now(timezone.utc)
        return max(0.0, delta.total_seconds())
    except (ValueError, TypeError):
        return None


def post_v1(
    api_key: str,
    host: Optional[str],
    batch_body: dict,
    *,
    attempt: int,
    request_id: str,
    gzip: bool = False,
    timeout: int = 15,
    session: Optional["requests.Session"] = None,
) -> "requests.Response":
    """Perform a single ``POST /i/v1/analytics/events`` attempt.

    Bearer-authed (no ``api_key`` in the body) with the required v1 headers.
    ``attempt`` (1-based) and the stable ``request_id`` are echoed via
    ``PostHog-Attempt``/``PostHog-Request-Id`` so the backend can correlate
    retries. Returns the raw response; classification is left to the caller.
    """
    trimmed_host = remove_trailing_slash(normalize_host(host))
    url = trimmed_host + CAPTURE_V1_PATH
    data: str | bytes = json.dumps(batch_body, cls=DatetimeSerializer)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {api_key}",
        HEADER_SDK_INFO: USER_AGENT,
        HEADER_ATTEMPT: str(attempt),
        HEADER_REQUEST_ID: request_id,
        HEADER_REQUEST_TIMESTAMP: datetime.now(timezone.utc).isoformat(),
    }
    if gzip:
        headers["Content-Encoding"] = "gzip"
        data = gzip_compress(cast(str, data))

    log.debug("capture v1 POST %s attempt=%s request_id=%s", url, attempt, request_id)
    return (session or _get_session()).post(
        url, data=data, headers=headers, timeout=timeout
    )


def parse_v1_response(res: "requests.Response") -> V1ParsedResponse:
    """Read and classify a v1 response without raising."""
    status = res.status_code
    retry_after = _parse_retry_after(res.headers.get("Retry-After"))

    if _is_success_status(status):
        try:
            payload = res.json()
            raw_results = payload["results"]
            results = {
                uid: V1EventResult(
                    result=(r or {}).get("result"),
                    details=(r or {}).get("details"),
                )
                for uid, r in raw_results.items()
            }
            return V1ParsedResponse(status, True, retry_after, results=results)
        except (ValueError, KeyError, AttributeError, TypeError):
            # 2xx with a body we can't read as a results map: terminal, so we
            # don't loop forever re-sending against a broken success.
            return V1ParsedResponse(status, True, retry_after, malformed=True)

    message = ""
    try:
        payload = res.json()
        if isinstance(payload, dict):
            message = (
                payload.get("error_description")
                or payload.get("error")
                or payload.get("detail")
                or ""
            )
    except (ValueError, AttributeError):
        pass
    if not message:
        message = res.text or f"capture v1 request failed with status {status}"
    return V1ParsedResponse(status, False, retry_after, error_message=message)


def _backoff(attempt_index: int, retry_after: Optional[float]) -> None:
    """Sleep before the next attempt: ``Retry-After`` wins, else exp backoff.

    Mirrors the legacy ``Consumer._send`` policy so both wire protocols back off
    identically (capped at 30s).
    """
    if retry_after and retry_after > 0:
        time.sleep(retry_after)
    else:
        time.sleep(min(2**attempt_index, 30))


def _log_result_summary(
    request_id: str, attempt: int, results: dict[str, V1EventResult]
) -> None:
    tally = {RESULT_OK: 0, RESULT_WARNING: 0, RESULT_DROP: 0, RESULT_RETRY: 0}
    other = 0
    for r in results.values():
        if r.result in tally:
            tally[r.result] += 1
        else:
            other += 1
    log.debug(
        "capture v1 response request_id=%s attempt=%s events=%d ok=%d warning=%d drop=%d retry=%d other=%d",
        request_id,
        attempt,
        len(results),
        tally[RESULT_OK],
        tally[RESULT_WARNING],
        tally[RESULT_DROP],
        tally[RESULT_RETRY],
        other,
    )


def send_v1_batch(
    api_key: str,
    host: Optional[str],
    batch: list[dict],
    *,
    gzip: bool = False,
    timeout: int = 15,
    max_retries: int = 3,
    historical_migration: bool = False,
    session: Optional["requests.Session"] = None,
) -> None:
    """Deliver ``batch`` to the v1 endpoint with partial retry.

    The v1 sibling of ``Consumer._send``: it loops up to ``max_retries + 1``
    attempts, but unlike v0 it shrinks the batch to only the events the server
    tagged ``retry`` after each 2xx. ``ok``/``warning``/absent events succeed
    silently; ``drop`` events are logged (a successful request that the server
    chose to drop is not a delivery failure). Raises :class:`CaptureV1Error`
    (or the underlying transport exception) on a batch-level terminal/transport
    failure or once retries are exhausted, so the caller's ``on_error`` fires.
    """
    request_id = str(uuid4())
    pending_events = [to_v1_event(m) for m in batch]
    pending_uuids = [e["uuid"] for e in pending_events]
    last_exc: Optional[Exception] = None

    for attempt_index in range(max_retries + 1):
        attempt = attempt_index + 1
        last_attempt = attempt_index == max_retries
        body = build_v1_batch_body(pending_events, historical_migration)

        try:
            res = post_v1(
                api_key,
                host,
                body,
                attempt=attempt,
                request_id=request_id,
                gzip=gzip,
                timeout=timeout,
                session=session,
            )
        except Exception as e:
            # Transport-level failure (connection/timeout): retry like v0 does.
            last_exc = e
            if last_attempt:
                raise
            _backoff(attempt_index, None)
            continue

        parsed = parse_v1_response(res)

        if parsed.is_success:
            if parsed.malformed:
                raise CaptureV1Error(
                    parsed.status_code,
                    "capture v1 returned a success status with an unparseable body",
                    request_id=request_id,
                    attempts=attempt,
                )
            results = parsed.results or {}
            _log_result_summary(request_id, attempt, results)

            retry_events: list[dict] = []
            retry_uuids: list[str] = []
            drops: list[tuple[str, Optional[str]]] = []
            for event, uid in zip(pending_events, pending_uuids):
                directive = results.get(uid)
                if directive is None:
                    # Absent from the map: treated as accepted (matches posthog-rs).
                    continue
                if directive.result == RESULT_RETRY:
                    retry_events.append(event)
                    retry_uuids.append(uid)
                elif directive.result == RESULT_DROP:
                    drops.append((uid, directive.details))
                # ok / warning / unrecognized -> terminal success.

            for uid, details in drops:
                log.warning(
                    "capture v1 dropped event uuid=%s request_id=%s details=%s",
                    uid,
                    request_id,
                    details or "",
                )

            if not retry_uuids:
                return
            if last_attempt:
                raise CaptureV1Error(
                    parsed.status_code,
                    f"{len(retry_uuids)} event(s) still pending retry after {attempt} attempt(s)",
                    request_id=request_id,
                    attempts=attempt,
                    retry_exhausted=retry_uuids,
                    drops=drops,
                )
            pending_events, pending_uuids = retry_events, retry_uuids
            _backoff(attempt_index, parsed.retry_after)
            continue

        # Non-2xx. Retryable transient statuses back off; everything else
        # (400/401/402/413/415/429/...) is terminal.
        v1_error = CaptureV1Error(
            parsed.status_code,
            parsed.error_message,
            retry_after=parsed.retry_after,
            request_id=request_id,
            attempts=attempt,
        )
        if parsed.status_code in RETRYABLE_STATUSES:
            last_exc = v1_error
            if last_attempt:
                raise v1_error
            _backoff(attempt_index, parsed.retry_after)
            continue
        raise v1_error

    # Unreachable in practice (every branch returns or continues), but keeps the
    # function total if max_retries is somehow negative.
    if last_exc:
        raise last_exc
