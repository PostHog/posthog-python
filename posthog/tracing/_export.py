"""The span export queue: batches ended spans and ships them to ``/i/v1/traces``.

Separate from the events queue, with its own timer. Only one flush runs at a
time. Failures back off exponentially, floored by any ``Retry-After``, and a
batch refused across ``MAX_RETRIES_PER_BATCH`` backoff windows is dropped so it
cannot hold the queue.
"""

import logging
import random
import threading
import time
from typing import Any, Callable, List, Optional, Tuple

from ..capture_v1 import _MAX_BACKOFF_SECONDS
from ._config import ResolvedTracesConfig
from ._drops import DropLog
from ._otlp import (
    SpanRecord,
    build_otlp_span,
    build_resource_attributes,
    build_traces_payload,
    to_resource_key_value_list,
)
from ._transport import SendOutcome, send_traces_batch

log = logging.getLogger("posthog")

MAX_RETRIES_PER_BATCH = 8

MAX_FLUSH_BACKOFF_EXPONENT = 6
# The events lane's ceiling, so both backoffs and the Retry-After clamp share one.
MAX_FLUSH_BACKOFF_SECONDS = float(_MAX_BACKOFF_SECONDS)

# Nothing upstream bounds the header, and an unbounded value would strand the
# queue.
MAX_RETRY_AFTER_SECONDS = MAX_FLUSH_BACKOFF_SECONDS

# Spread each backoff by up to a quarter, so clients refused together do not
# return together.
FLUSH_BACKOFF_JITTER = 0.25

SendFn = Callable[[Any, dict], SendOutcome]


class _RetryAfterWindow:
    """The wait the endpoint asked for, as a monotonic deadline.

    A later deadline extends the window, capped at ``MAX_RETRY_AFTER_SECONDS``
    from when it was first installed; a shorter one never pulls it in.
    """

    def __init__(self) -> None:
        self._until = 0.0
        self._installed_at = 0.0

    def record(self, outcome: SendOutcome) -> None:
        if outcome.kind == "too-large":
            return
        if outcome.kind != "retry-later":
            self.reset()
            return
        if not outcome.retry_after or outcome.retry_after <= 0:
            return
        # Read first: a spent window resets `_installed_at`.
        is_open = self.is_open()
        now = time.monotonic()
        asked = min(outcome.retry_after, MAX_RETRY_AFTER_SECONDS)
        if not is_open:
            self._installed_at = now
            self._until = now + asked
            return
        self._until = max(
            self._until, min(now + asked, self._installed_at + MAX_RETRY_AFTER_SECONDS)
        )

    def remaining(self) -> float:
        remaining = min(
            MAX_RETRY_AFTER_SECONDS, max(0.0, self._until - time.monotonic())
        )
        if remaining == 0:
            self.reset()
        return remaining

    def is_open(self) -> bool:
        return self.remaining() > 0

    def reset(self) -> None:
        self._until = 0.0
        self._installed_at = 0.0


class SpanExporter:
    def __init__(
        self,
        client: Any,
        config: ResolvedTracesConfig,
        drops: DropLog,
        send: SendFn = send_traces_batch,
    ) -> None:
        self._client = client
        self._config = config
        self._drops = drops
        self._send = send
        # Encoded once: the resource is the same for every batch.
        self._resource = to_resource_key_value_list(
            build_resource_attributes(
                config.service_name,
                config.service_version,
                config.environment,
                config.resource_attributes,
            )
        )

        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        # Set by close(), so a flush waiting out a backoff returns at once.
        self._closing = threading.Event()
        self._closed = False
        self._queue: List[SpanRecord] = []
        self._flush_timer: Optional[threading.Timer] = None
        self._flush_timer_fires_at = 0.0
        self._max_export_batch_size = config.max_export_batch_size
        # The size before a server 413 halved it, restored once the oversized
        # span is isolated and dropped.
        self._batch_size_before_halving: Optional[int] = None
        self._consecutive_failures = 0
        # Drawn once per failure, so the timer and the retry-budget charge see
        # the same delay.
        self._jitter = 1.0
        self._retry_after = _RetryAfterWindow()
        self._head_batch_failures = 0
        self._head_batch_size = 0
        self._head_batch_chargeable_at = 0.0

    def enqueue(self, record: SpanRecord) -> None:
        with self._lock:
            if self._closed:
                self._drops.record(1, "tracing was shut down")
            elif len(self._queue) >= self._config.max_queue_size:
                # The incoming span goes, not queued ones: those are parents
                # whose children may already have shipped.
                self._drops.record(
                    1,
                    "the queue is full ({}); raise the flush frequency or reduce "
                    "span volume".format(self._config.max_queue_size),
                )
            else:
                self._queue.append(record)
                if self._depth_trigger_due_locked():
                    self._arm_timer_locked(
                        0.0, replace=self._flush_timer_fires_at > time.monotonic()
                    )
                else:
                    self._arm_timer_if_idle_locked()
        self._drops.warn_if_due()

    def flush(
        self, timeout: Optional[float] = None, _timer: Optional[threading.Timer] = None
    ) -> None:
        """Drain the queue: one pass over what was queued, then one follow-up pass.

        With a ``timeout``, the budget starts once no other flush is in flight;
        one still in flight after that long a wait is left to finish and
        nothing is sent here. No request starts once the budget is spent,
        except the first, and a retriable failure is retried after its backoff
        while budget remains, once more at the deadline. Without a timeout a
        retriable failure is left to the timer. A request already in flight is
        bounded by the client's timeout.
        """
        # -1 is Lock.acquire's unbounded form.
        if not self._flush_lock.acquire(
            timeout=-1 if timeout is None else max(0.0, timeout)
        ):
            log.debug(
                "Skipping a span flush: another flush was still in flight after %ss",
                timeout,
            )
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            with self._lock:
                # A timer that waited behind another flush may have been
                # superseded by the backoff that flush armed.
                if _timer is not None and _timer is not self._flush_timer:
                    return
                self._clear_timer_locked()
            try:
                self._drain_within_budget(deadline, retry=_timer is None)
            finally:
                with self._lock:
                    self._rearm_after_pass_locked()
        finally:
            self._flush_lock.release()
            self._drops.warn_if_due(force=True)

    def _drain_within_budget(self, deadline: Optional[float], retry: bool) -> None:
        while True:
            removed, stop = self._drain(deadline)
            if not stop:
                if (
                    removed
                    and self._queue
                    and (deadline is None or time.monotonic() < deadline)
                ):
                    self._drain(deadline)
                return
            if deadline is None or not retry:
                return
            wait = self._retry_wait_locked()
            remaining = deadline - time.monotonic()
            if wait is None or remaining <= 0:
                return
            if self._wait_for_retry(min(wait, remaining)):
                return

    def _retry_wait_locked(self) -> Optional[float]:
        """The backoff to wait out before retrying, or ``None`` when there is nothing to retry."""
        with self._lock:
            if self._closed or not self._queue or not self._consecutive_failures:
                return None
            return self._next_flush_delay_locked()

    def _wait_for_retry(self, seconds: float) -> bool:
        """Wait out a backoff; ``True`` when close() cut the wait short."""
        return self._closing.wait(seconds)

    def close(self) -> None:
        """Stop exporting and discard what is still queued. Called at shutdown."""
        with self._lock:
            self._closed = True
            self._closing.set()
            self._clear_timer_locked()
            discarded = len(self._queue)
            self._queue = []
        if discarded:
            log.warning(
                "Discarding %s span(s) that were still queued when tracing was shut "
                "down. Call flush() earlier if they matter.",
                discarded,
            )

    def warn_if_queued(self) -> None:
        with self._lock:
            queued = len(self._queue)
        if queued:
            log.warning(
                "%s span(s) were still queued at exit and may not be sent. Call "
                "flush() or shutdown() before exit.",
                queued,
            )

    def reinit_after_fork(self) -> None:
        # The inherited locks may be held by parent threads that do not exist
        # in the child, so they are replaced rather than acquired.
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._closing = threading.Event()
        self._flush_timer = None
        self._flush_timer_fires_at = 0.0
        self._queue = []
        self._max_export_batch_size = self._config.max_export_batch_size
        self._batch_size_before_halving = None
        self._retry_after.reset()
        self._end_failure_sequence_locked()

    def _drain(self, deadline: Optional[float]) -> Tuple[int, bool]:
        """One pass over the queue as it stood at the start.

        Returns ``(removed, stop)``; ``stop`` means no follow-up pass now.
        """
        with self._lock:
            if self._is_closed() or self._discard_if_disabled_locked():
                return 0, True
            remaining = len(self._queue)
        removed = 0
        sent_any = False
        # A batch the SDK measured as too large says nothing about the batches
        # after the oversized span is gone, so only this drain is split.
        local_cap: Optional[int] = None
        while remaining > 0:
            with self._lock:
                if self._is_closed() or self._discard_if_disabled_locked():
                    return removed, True
                if not self._queue:
                    break
                # A retried batch cannot grow to take in spans behind it.
                cap = (
                    min(self._max_export_batch_size, self._head_batch_size)
                    if self._head_batch_failures
                    else self._max_export_batch_size
                )
                if local_cap is not None:
                    cap = min(cap, local_cap)
                size = max(1, min(cap, remaining, len(self._queue)))
                batch = self._queue[:size]
                # Read before the send. A send inside an open Retry-After
                # window is caller-driven and exempt from the wait, and so from
                # the charge.
                chargeable = (
                    time.monotonic() >= self._head_batch_chargeable_at
                    and not self._retry_after.is_open()
                )

            spans, failed = self._encode(batch)
            if failed:
                # Out of the queue before the send is settled, so a failed
                # send does not count them a second time.
                with self._lock:
                    if self._is_closed():
                        return removed, True
                    for index in reversed(failed):
                        del self._queue[index]
                    if not spans:
                        self._reset_head_batch_budget_locked()
                size -= len(failed)
                remaining -= len(failed)
                removed += len(failed)
                if not spans:
                    continue

            # The first request is exempt, so a flush called with no budget left
            # (a serverless handler, say) still ships a batch.
            if sent_any and deadline is not None and time.monotonic() >= deadline:
                return removed, True
            sent_any = True
            try:
                outcome = self._send(
                    self._client, build_traces_payload(spans, self._resource)
                )
            except Exception:
                log.debug("Span batch send failed", exc_info=True)
                outcome = SendOutcome("retry-later")

            with self._lock:
                if self._is_closed():
                    return removed, True
                taken, stop, note = self._apply_outcome_locked(
                    outcome, size, chargeable
                )
            if note:
                log.debug(note)
            if outcome.measured_locally and not taken:
                local_cap = max(1, size // 2)
            remaining -= taken
            removed += taken
            if stop:
                return removed, True
        return removed, False

    def _apply_outcome_locked(
        self, outcome: SendOutcome, size: int, chargeable: bool
    ) -> Tuple[int, bool, Optional[str]]:
        """Settle one response. Returns ``(spans removed, stop the pass, debug note)``."""
        self._retry_after.record(outcome)

        if outcome.kind == "ok":
            del self._queue[:size]
            self._end_failure_sequence_locked()
            self._batch_size_before_halving = None
            if self._max_export_batch_size < self._config.max_export_batch_size:
                self._max_export_batch_size = min(
                    self._config.max_export_batch_size, self._max_export_batch_size * 2
                )
            return size, False, None

        if outcome.kind == "too-large":
            if size == 1:
                del self._queue[:1]
                self._end_failure_sequence_locked()
                self._drops.record(1, "it is too large for the ingestion endpoint")
                # The oversized span is gone; the batches after it are not suspect.
                if self._batch_size_before_halving is not None:
                    self._max_export_batch_size = self._batch_size_before_halving
                    self._batch_size_before_halving = None
                return 1, False, None
            # Halve the refused batch, not the configured size: a shallow queue
            # would otherwise resend the same body.
            halved = max(1, size // 2)
            if not outcome.measured_locally:
                if self._batch_size_before_halving is None:
                    self._batch_size_before_halving = self._max_export_batch_size
                self._max_export_batch_size = halved
            self._reset_head_batch_budget_locked()
            return (
                0,
                False,
                "Span batch too large; retrying the same spans in batches of {}".format(
                    halved
                ),
            )

        if outcome.kind == "retry-later":
            self._consecutive_failures += 1
            self._jitter = _draw_jitter()
            self._head_batch_size = size
            # One charge per backoff window: a refusal before it elapses is the
            # same refusal seen again.
            if chargeable:
                self._head_batch_failures += 1
                self._head_batch_chargeable_at = (
                    time.monotonic() + self._next_flush_delay_locked()
                )
            if self._head_batch_failures < MAX_RETRIES_PER_BATCH:
                return 0, True, "Span export failed; retrying on the next flush"
            del self._queue[:size]
            # The endpoint is still failing: the next batch keeps backing off.
            self._reset_head_batch_budget_locked()
            self._drops.record(
                size,
                "the ingestion endpoint failed {} times in a row".format(
                    MAX_RETRIES_PER_BATCH
                ),
            )
            # Dropping the batch does not end the endpoint's wait.
            return size, self._retry_after.is_open(), None

        del self._queue[:size]
        self._end_failure_sequence_locked()
        self._drops.record(size, "the ingestion endpoint rejected the batch")
        return size, False, None

    def _is_closed(self) -> bool:
        # A method, so the check after each send is not narrowed away.
        return self._closed

    def _encode(self, batch: List[SpanRecord]) -> Tuple[List[dict], List[int]]:
        """The batch as OTLP spans, and the indexes of records that could not be encoded."""
        encoded: List[dict] = []
        failed: List[int] = []
        for index, record in enumerate(batch):
            try:
                encoded.append(build_otlp_span(record))
            except Exception:
                log.debug("Failed to encode a span; dropping it", exc_info=True)
                self._drops.record(1, "its attributes could not be encoded")
                failed.append(index)
        return encoded, failed

    def _discard_if_disabled_locked(self) -> bool:
        if not getattr(self._client, "disabled", False):
            return False
        # Spans carry person and session ids: once the client is disabled,
        # nothing queued may export.
        if self._queue:
            self._drops.record(len(self._queue), "the client is disabled")
            self._queue = []
        self._end_failure_sequence_locked()
        return True

    def _end_failure_sequence_locked(self) -> None:
        self._consecutive_failures = 0
        self._jitter = 1.0
        self._reset_head_batch_budget_locked()

    def _reset_head_batch_budget_locked(self) -> None:
        self._head_batch_failures = 0
        self._head_batch_chargeable_at = 0.0

    def _next_flush_delay_locked(self) -> float:
        """The flush interval, doubled per failure up to 30 s and jittered,
        floored by Retry-After."""
        exponent = min(
            max(0, self._consecutive_failures - 1), MAX_FLUSH_BACKOFF_EXPONENT
        )
        delay = self._config.flush_interval * 2**exponent
        capped = min(delay, max(MAX_FLUSH_BACKOFF_SECONDS, self._config.flush_interval))
        return max(capped * self._jitter, self._retry_after.remaining())

    def _depth_trigger_due_locked(self) -> bool:
        # Not while backing off: the queue stays at depth through an outage, and
        # every span end would re-send.
        return (
            len(self._queue) >= self._max_export_batch_size
            and not self._consecutive_failures
            and not self._retry_after.is_open()
        )

    def _arm_timer_if_idle_locked(self) -> None:
        if self._flush_timer is None and self._queue:
            self._arm_timer_locked(self._next_flush_delay_locked())

    def _rearm_after_pass_locked(self) -> None:
        if not self._queue or self._closed:
            return
        # The depth trigger, which a span end may have fired mid-pass: the
        # replacement below would otherwise push it out a whole interval.
        if self._depth_trigger_due_locked():
            self._arm_timer_locked(0.0, replace=True)
            return
        delay = self._next_flush_delay_locked()
        if (
            self._flush_timer is not None
            and time.monotonic() + delay <= self._flush_timer_fires_at
        ):
            return
        self._arm_timer_locked(delay, replace=True)

    def _arm_timer_locked(self, delay: float, replace: bool = False) -> None:
        if self._flush_timer is not None and not replace:
            return
        timer = threading.Timer(delay, lambda: self._timer_flush(timer))
        timer.daemon = True
        # Started before the old timer is cancelled, so a failed start keeps
        # whatever flush was already scheduled.
        timer.start()
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_timer = timer
        self._flush_timer_fires_at = time.monotonic() + delay

    def _timer_flush(self, fired: threading.Timer) -> None:
        with self._lock:
            if fired is not self._flush_timer:
                return
        try:
            self.flush(_timer=fired)
        except Exception:
            log.debug("Background span flush failed", exc_info=True)

    def _clear_timer_locked(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None


def _draw_jitter() -> float:
    return 1 - FLUSH_BACKOFF_JITTER + random.random() * FLUSH_BACKOFF_JITTER * 2
