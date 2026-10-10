"""Transport for the capture v1 wire protocol.

This module sends batches to the capture v1 endpoints
(``POST /i/v1/analytics/events`` and ``POST /i/v1/ai/events``, which share one
wire contract): a single HTTP attempt, response parsing, and the partial-retry
send loop. :mod:`posthog.capture_event` builds the events and batch envelope.

The response is per-event: a 200 carries a ``results`` map keyed by event uuid,
each tagged ``ok``/``warning`` (terminal-success), ``drop`` (terminal-failure),
or ``retry``. :func:`_send_v1_batch` resends only the ``retry`` events on the next
attempt, holding the ``PostHog-Request-Id`` and batch ``created_at`` stable
across attempts while incrementing ``PostHog-Attempt``. ``ok``/``warning``/absent
events succeed; ``drop`` and retry-exhaustion are carried on the
:class:`CaptureError` raised on batch-level/terminal failure, so the consumer's
existing ``on_error(exc, batch)`` path surfaces them unchanged (no per-event
logging of its own).

Request bodies are optionally compressed per :class:`~posthog.capture_compression.CaptureCompression`
(``gzip`` or zlib-wrapped ``deflate``), advertised via ``Content-Encoding``.
"""

import json
import logging
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from gzip import GzipFile
from io import BytesIO
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from posthog.capture_compression import CaptureCompression, _zstandard
from posthog.capture_event import _build_v1_batch_body, _to_v1_event
from posthog.request import (
    DatetimeSerializer,
    USER_AGENT,
    APIError,
    _get_session,
    normalize_host,
)
from posthog.utils import _uuid7, remove_trailing_slash

if TYPE_CHECKING:
    import requests

log = logging.getLogger("posthog")

# Only the error types are public API: they reach user code through `on_error`
# callbacks, so callers may want to catch/inspect them. Everything else is
# submitter plumbing.
__all__ = ["CaptureError", "CaptureEventResult"]

_CAPTURE_V1_PATH = "/i/v1/analytics/events"
_CAPTURE_AI_V1_PATH = "/i/v1/ai/events"

# Required request/response headers for the v1 endpoint. Defined here as the
# single source of truth; the transport layer builds requests from them.
_HEADER_SDK_INFO = "PostHog-Sdk-Info"
_HEADER_ATTEMPT = "PostHog-Attempt"
_HEADER_REQUEST_ID = "PostHog-Request-Id"
_HEADER_REQUEST_TIMESTAMP = "PostHog-Request-Timestamp"

# Per-event result codes the backend emits (rust EventResult). `ok`/`warning`
# are terminal-success; `drop` terminal-failure; `retry` is safe to resend.
_RESULT_OK = "ok"
_RESULT_WARNING = "warning"
_RESULT_DROP = "drop"
_RESULT_RETRY = "retry"

# HTTP status classification. 429 is terminal in v1 (unlike v0, where it is
# retried) — the backend signals overload via retryable 5xx + Retry-After.
_RETRYABLE_STATUSES = frozenset({408, 500, 502, 503, 504})
_TERMINAL_STATUSES = frozenset({400, 401, 402, 413, 415, 429})

# Single ceiling (seconds) for the retry backoff: caps the exponential schedule
# and clamps a server ``Retry-After`` to the same value. Keeps the max retry
# wait bounded (a hostile/buggy header can't park the consumer thread) and
# unifies the default with posthog-go/posthog-rs (all 30s).
_MAX_BACKOFF_SECONDS = 30

# First retry delay; it doubles per attempt up to `_MAX_BACKOFF_SECONDS`. Matches
# posthog-go.
_BACKOFF_BASE_SECONDS = 0.1


@dataclass(frozen=True)
class CaptureEventResult:
    """One event's verdict from a 2xx capture response.

    ``result`` is ``ok``, ``warning``, ``drop`` or ``retry``, or a value newer
    than this SDK, which counts as success. ``details`` is the server's reason
    tag, such as ``billing_limit_exceeded``.
    """

    result: Optional[str]
    details: Optional[str] = None


@dataclass
class _V1ParsedResponse:
    """Classified outcome of one v1 HTTP attempt.

    ``is_success`` is the 2xx classification. On success ``results`` holds the
    per-uuid directives (``None``/``malformed=True`` when the body could not be
    parsed — treated as terminal so a bad success never loops forever). On a
    non-2xx, ``error_message`` is the best-effort human-readable detail.
    """

    status_code: int
    is_success: bool
    retry_after: Optional[float] = None
    results: Optional[dict[str, CaptureEventResult]] = None
    malformed: bool = False
    error_message: str = ""


class CaptureError(APIError):
    """A capture batch that was not fully delivered.

    Passed to ``on_error`` with the batch. ``status`` is the HTTP status of the
    last attempt, or ``0`` when no response arrived (``__cause__`` holds the
    transport error). A 2xx ``status`` means the request succeeded but some
    events were dropped or ran out of retries.

    ``event_results`` maps each event uuid to its last verdict from a 2xx
    response. Events missing from it never got a verdict, because every
    attempt that reached them failed as a whole request.
    """

    def __init__(
        self,
        status: int | str,
        message: str,
        *,
        endpoint: str,
        retry_after: Optional[float] = None,
        request_id: Optional[str] = None,
        attempts: Optional[int] = None,
        retry_exhausted: Optional[list[str]] = None,
        drops: Optional[list[tuple[str, Optional[str]]]] = None,
        event_results: Optional[dict[str, CaptureEventResult]] = None,
    ):
        super().__init__(status, message, retry_after=retry_after)
        # Capture path the batch was sent to, so one on_error handler can tell
        # the analytics and AI lanes apart.
        self.endpoint = endpoint
        self.request_id = request_id
        self.attempts = attempts
        # uuids the server told us to retry but we never delivered (exhausted).
        self.retry_exhausted = retry_exhausted or []
        # (uuid, details) pairs the server told us to drop on a 2xx response.
        self.drops = drops or []
        self.event_results = event_results or {}

    def verdict_summary(self) -> str:
        """Count the undelivered events by verdict and reason, for example
        ``drop/billing_limit_exceeded=2, retry/not_persisted=1``.

        Counts the events in ``drops`` and ``retry_exhausted``: events the
        server dropped, and events still pending retry after the last 2xx
        response. A retry verdict followed by a failed request, for example a
        503 on the last attempt, is not counted; ``event_results`` keeps it.
        Empty when no event is counted.
        """
        counts: dict[str, int] = {}
        failed = [uid for uid, _ in self.drops] + self.retry_exhausted
        for uid in failed:
            verdict = self.event_results.get(uid)
            if verdict is None or not verdict.result:
                continue
            tag = verdict.result
            if verdict.details:
                tag = f"{tag}/{verdict.details}"
            counts[tag] = counts.get(tag, 0) + 1
        return ", ".join(f"{tag}={n}" for tag, n in sorted(counts.items()))


def _is_success_status(status: int) -> bool:
    return 200 <= status < 300


def _canonical_uuid(value: str) -> str:
    """Return ``value`` in the lowercase hyphenated form capture keys results by.

    ``before_send`` can set any uuid form capture parses. A value that does not
    parse is returned unchanged; capture rejects its whole batch anyway.
    """
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        return value


def _undelivered_count(error: Exception, batch_size: int) -> int:
    if not isinstance(error, CaptureError):
        return batch_size
    delivered = sum(
        1
        for r in error.event_results.values()
        if r.result not in (_RESULT_DROP, _RESULT_RETRY)
    )
    return max(0, batch_size - delivered)


def _capture_loss_message(error: Exception, batch_size: int, endpoint: str) -> str:
    """One aggregate line for a failed batch, for callers with no ``on_error``.

    It never names individual events, includes payloads, or repeats the server's
    error text: any of them may carry sensitive content, and per-event lines
    scale with event volume.
    """
    count = _undelivered_count(error, batch_size)
    if isinstance(error, CaptureError):
        endpoint = error.endpoint
        if isinstance(error.status, int) and _is_success_status(error.status):
            detail = f"{len(error.drops)} dropped, {len(error.retry_exhausted)} out of retries"
            summary = error.verdict_summary()
            if summary:
                detail = f"{detail} ({summary})"
            return f"{count} event(s) not persisted by {endpoint}: {detail}"
    detail = type(error).__name__
    if error.__cause__ is not None:
        detail = f"{detail} from {type(error.__cause__).__name__}"
    status = getattr(error, "status", None)
    if status is not None:
        detail = f"{detail} (status={status})"
    return f"{count} event(s) not persisted by {endpoint}: {detail}"


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


def _compress_v1(
    compression: CaptureCompression, data: str
) -> tuple[str | bytes, Optional[str]]:
    """Compress a v1 request body, returning ``(body, Content-Encoding token)``.

    ``GZIP`` emits a gzip stream; ``DEFLATE`` emits a *zlib-wrapped* deflate
    stream (RFC 1950, leading ``0x78``) to match posthog-go / posthog-rs and the
    server's zlib decoder for ``Content-Encoding: deflate`` — raw, headerless
    deflate would be misrouted. ``ZSTD`` emits a standard zstd frame via the
    optional zstandard package. ``NONE`` returns the string body and no token.
    """
    if compression == CaptureCompression.GZIP:
        buf = BytesIO()
        with GzipFile(fileobj=buf, mode="w") as gz:
            # `data` is produced by json.dumps(), whose default encoding is utf-8.
            gz.write(data.encode("utf-8"))
        return buf.getvalue(), "gzip"
    if compression == CaptureCompression.DEFLATE:
        return zlib.compress(data.encode("utf-8")), "deflate"
    if compression == CaptureCompression.ZSTD:
        # _resolve_capture_compression only yields ZSTD when zstandard is
        # importable; this guard covers direct Consumer construction.
        if _zstandard is None:
            raise ValueError(
                "capture_compression 'zstd' requires the zstandard package; "
                "install posthog[zstd]"
            )
        return _zstandard.ZstdCompressor().compress(data.encode("utf-8")), "zstd"
    return data, None


def _post_v1(
    api_key: str,
    host: Optional[str],
    batch_body: dict,
    *,
    attempt: int,
    request_id: str,
    compression: CaptureCompression = CaptureCompression.NONE,
    timeout: int = 15,
    sdk_info: str = USER_AGENT,
    session: Optional["requests.Session"] = None,
    path: str = _CAPTURE_V1_PATH,
) -> "requests.Response":
    """Perform a single capture v1 ``POST`` to ``path``.

    Bearer-authed (no ``api_key`` in the body) with the required v1 headers.
    ``attempt`` (1-based) and the stable ``request_id`` are echoed via
    ``PostHog-Attempt``/``PostHog-Request-Id`` so the backend can correlate
    retries. The body is compressed per ``compression`` (advertised via
    ``Content-Encoding``). Returns the raw response; classification is left to
    the caller.
    """
    trimmed_host = remove_trailing_slash(normalize_host(host))
    url = trimmed_host + path
    data = json.dumps(batch_body, cls=DatetimeSerializer)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": sdk_info,
        "Authorization": f"Bearer {api_key}",
        _HEADER_SDK_INFO: sdk_info,
        _HEADER_ATTEMPT: str(attempt),
        _HEADER_REQUEST_ID: request_id,
        _HEADER_REQUEST_TIMESTAMP: datetime.now(timezone.utc).isoformat(),
    }
    body, encoding = _compress_v1(compression, data)
    if encoding is not None:
        headers["Content-Encoding"] = encoding

    log.debug("capture v1 POST %s attempt=%s request_id=%s", url, attempt, request_id)
    return (session or _get_session()).post(
        url, data=body, headers=headers, timeout=timeout
    )


def _parse_v1_response(res: "requests.Response") -> _V1ParsedResponse:
    """Read and classify a v1 response without raising."""
    status = res.status_code
    retry_after = _parse_retry_after(res.headers.get("Retry-After"))

    if _is_success_status(status):
        try:
            payload = res.json()
            raw_results = payload["results"]
            results = {
                uid: CaptureEventResult(
                    result=(r or {}).get("result"),
                    details=(r or {}).get("details"),
                )
                for uid, r in raw_results.items()
            }
            return _V1ParsedResponse(status, True, retry_after, results=results)
        except (ValueError, KeyError, AttributeError, TypeError):
            # 2xx with a body we can't read as a results map: terminal, so we
            # don't loop forever re-sending against a broken success.
            return _V1ParsedResponse(status, True, retry_after, malformed=True)

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
    return _V1ParsedResponse(status, False, retry_after, error_message=message)


def _backoff(attempt_index: int, retry_after: Optional[float]) -> None:
    """Sleep before the next attempt.

    Exponential backoff capped at :data:`_MAX_BACKOFF_SECONDS` is the base. When
    the server sent a ``Retry-After`` it acts as a *minimum*, not a replacement:
    the client waits the longer of the configured backoff and ``Retry-After``, so
    a small ``Retry-After`` never retries earlier than the normal schedule
    (matching posthog-go / posthog-rs). ``Retry-After`` is itself clamped to
    :data:`_MAX_BACKOFF_SECONDS`, so both sides share one ceiling and a
    hostile/buggy header can't park the consumer thread.
    """
    configured = min(_BACKOFF_BASE_SECONDS * 2**attempt_index, _MAX_BACKOFF_SECONDS)
    clamped_retry_after = (
        min(retry_after, _MAX_BACKOFF_SECONDS) if retry_after and retry_after > 0 else 0
    )
    time.sleep(max(configured, clamped_retry_after))


def _log_result_summary(
    request_id: str, attempt: int, results: dict[str, CaptureEventResult]
) -> None:
    tally = {_RESULT_OK: 0, _RESULT_WARNING: 0, _RESULT_DROP: 0, _RESULT_RETRY: 0}
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
        tally[_RESULT_OK],
        tally[_RESULT_WARNING],
        tally[_RESULT_DROP],
        tally[_RESULT_RETRY],
        other,
    )


def _send_v1_batch(
    api_key: str,
    host: Optional[str],
    batch: list[dict],
    *,
    compression: CaptureCompression = CaptureCompression.NONE,
    timeout: int = 15,
    max_retries: int = 3,
    historical_migration: bool = False,
    sdk_info: str = USER_AGENT,
    session: Optional["requests.Session"] = None,
    path: str = _CAPTURE_V1_PATH,
) -> None:
    """Deliver ``batch`` to the v1 endpoint at ``path`` with partial retry.

    Loops up to ``max_retries + 1`` attempts. After each 2xx it resends only
    the events the server tagged ``retry``. ``ok``/``warning`` events, events
    with an unrecognized verdict, and events absent from ``results`` succeed
    (matching posthog-go and posthog-rs). Results are matched by canonical
    uuid, because capture keys them that way whatever form was sent.

    A server-chosen ``drop`` is a terminal per-event rejection. Drops are
    accumulated across attempts and surfaced via :class:`CaptureError` even
    when the request itself was a 2xx (a success status is not full delivery)
    and even when a later attempt clears the outstanding retries — matching
    posthog-go (per-event failure callback) and posthog-rs (``on_error`` on a
    2xx with undelivered verdicts). Raises :class:`CaptureError` on any drop,
    batch-level terminal failure, retry exhaustion, or transport failure
    (``status`` 0, chained to the transport error), carrying the endpoint, the
    accumulated ``drops``, any exhausted uuids, and every verdict seen so far,
    so the caller's ``on_error`` gets the full picture.
    ``request_id`` and the batch ``created_at`` are stable across attempts;
    ``PostHog-Attempt`` increments. Negative ``max_retries`` values are treated
    as zero, so delivery is always attempted at least once.
    """
    max_retries = max(0, max_retries)
    request_id = str(_uuid7())
    # Hoisted once so the batch envelope is byte-identical across retry attempts
    # (only the events list shrinks and the attempt header increments).
    created_at = datetime.now(timezone.utc).isoformat()
    pending_events = [_to_v1_event(m) for m in batch]
    pending_uuids = [_canonical_uuid(e["uuid"]) for e in pending_events]
    # (uuid, details) for every event the server dropped, across all attempts.
    # Accumulated (not per-attempt) so a drop seen early is not lost when a
    # later attempt succeeds or clears the outstanding retries.
    all_drops: list[tuple[str, Optional[str]]] = []
    # Latest 2xx verdict per uuid, across all attempts.
    event_results: dict[str, CaptureEventResult] = {}

    def capture_error(
        status: int,
        message: str,
        *,
        attempts: int,
        retry_after: Optional[float] = None,
        retry_exhausted: Optional[list[str]] = None,
    ) -> CaptureError:
        return CaptureError(
            status,
            message,
            endpoint=path,
            retry_after=retry_after,
            request_id=request_id,
            attempts=attempts,
            retry_exhausted=retry_exhausted,
            drops=all_drops,
            event_results=event_results,
        )

    for attempt_index in range(max_retries + 1):
        attempt = attempt_index + 1
        last_attempt = attempt_index == max_retries
        body = _build_v1_batch_body(
            pending_events, historical_migration, created_at=created_at
        )

        try:
            res = _post_v1(
                api_key,
                host,
                body,
                attempt=attempt,
                request_id=request_id,
                compression=compression,
                timeout=timeout,
                sdk_info=sdk_info,
                session=session,
                path=path,
            )
        except Exception as e:
            # Transport-level failure (connection/timeout): retryable.
            if last_attempt:
                raise capture_error(
                    0, f"{type(e).__name__}: {e}", attempts=attempt
                ) from e
            _backoff(attempt_index, None)
            continue

        parsed = _parse_v1_response(res)

        if parsed.is_success:
            if parsed.malformed:
                raise capture_error(
                    parsed.status_code,
                    "capture v1 returned a success status with an unparseable body",
                    attempts=attempt,
                )
            results = parsed.results or {}
            _log_result_summary(request_id, attempt, results)

            retry_events: list[dict] = []
            retry_uuids: list[str] = []
            for event, uid in zip(pending_events, pending_uuids):
                directive = results.get(uid)
                if directive is None:
                    # Absent from the map: treated as accepted (matches
                    # posthog-go and posthog-rs). Capture answers every event
                    # of a 2xx, so this only happens if it breaks that contract.
                    continue
                event_results[uid] = directive
                if directive.result == _RESULT_RETRY:
                    retry_events.append(event)
                    retry_uuids.append(uid)
                elif directive.result == _RESULT_DROP:
                    # Terminal per-event rejection; keep it so it is surfaced
                    # even when the rest of the batch succeeds (see below).
                    all_drops.append((uid, directive.details))
                # ok / warning / unrecognized -> terminal success.

            if not retry_uuids:
                # Nothing left to resend. If the server dropped any events,
                # surface them via on_error even though the request was a 2xx —
                # a success status does not mean every event was delivered.
                if all_drops:
                    raise capture_error(
                        parsed.status_code,
                        f"{len(all_drops)} event(s) dropped by the server",
                        attempts=attempt,
                    )
                return
            if last_attempt:
                raise capture_error(
                    parsed.status_code,
                    f"{len(retry_uuids)} event(s) still pending retry after {attempt} attempt(s)",
                    attempts=attempt,
                    retry_exhausted=retry_uuids,
                )
            pending_events, pending_uuids = retry_events, retry_uuids
            _backoff(attempt_index, parsed.retry_after)
            continue

        # Non-2xx. Retryable transient statuses back off; everything else
        # (400/401/402/413/415/429/...) is terminal. Any drops collected from a
        # prior 2xx attempt ride along so on_error still sees them.
        v1_error = capture_error(
            parsed.status_code,
            parsed.error_message,
            retry_after=parsed.retry_after,
            attempts=attempt,
        )
        if parsed.status_code in _RETRYABLE_STATUSES and not last_attempt:
            _backoff(attempt_index, parsed.retry_after)
            continue
        raise v1_error
