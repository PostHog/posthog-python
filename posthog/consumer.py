from typing import Any, Optional
import json
import logging
import threading
import time
from threading import Thread

from posthog._logging import _configure_posthog_logging
from posthog.capture_compression import CaptureCompression
from posthog.capture_mode import CaptureMode
from posthog.capture_v1 import _send_v1_batch
from posthog.request import (
    EVENTS_ENDPOINT,
    APIError,
    DatetimeSerializer,
    batch_post,
)

from queue import Empty


MAX_MSG_SIZE = 900 * 1024  # 900KiB per event

# AI events carry LLM inputs/outputs and post to a dedicated endpoint whose
# pipeline accepts larger messages than analytics ingestion, so the AI lane
# grants a higher per-event ceiling. `next()` appends an item before checking
# BATCH_SIZE_LIMIT, so worst-case request body is BATCH_SIZE_LIMIT +
# AI_MAX_MSG_SIZE (~13MiB) — keep that sum under the 20MiB server body cap.
AI_MAX_MSG_SIZE = 8 * 1024 * 1024  # 8MiB per event

# The maximum request body size is currently 20MiB, let's be conservative
# in case we want to lower it in the future.
BATCH_SIZE_LIMIT = 5 * 1024 * 1024

# How long a consumer that is already accumulating a batch may block on the
# queue before re-checking its drain signal. An idle consumer (nothing
# accumulated) still parks for the whole `flush_interval`, because anything a
# caller enqueued before calling `flush()` is already in the queue and wakes the
# blocking `get` on its own.
DRAIN_POLL_INTERVAL = 0.05


_configure_posthog_logging()


class DrainSignal:
    """Cross-thread "stop batching and send what is pending" signal.

    Explicit flushes must not wait for `flush_at` or `flush_interval`, but a
    consumer accumulating a partial batch is parked on its queue and cannot see
    a plain flag flip. `flush()` bumps a generation counter here; each consumer
    remembers the generation it last saw its queue empty at, so a request stays
    pending until that consumer has actually handed off everything it holds.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0

    def request(self) -> None:
        """Ask every consumer sharing this signal to deliver what it has now."""
        with self._lock:
            self._generation += 1

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation


class Consumer(Thread):
    """Consumes the messages from the client's queue."""

    log = logging.getLogger("posthog")

    def __init__(
        self,
        queue,
        api_key,
        flush_at=100,
        host=None,
        on_error=None,
        flush_interval=5.0,
        gzip=False,
        retries=10,
        timeout=15,
        historical_migration=False,
        endpoint=EVENTS_ENDPOINT,
        max_msg_size=MAX_MSG_SIZE,
        capture_mode=CaptureMode.V0,
        capture_compression=CaptureCompression.NONE,
        drain_signal: Optional[DrainSignal] = None,
    ):
        """Create a consumer thread."""
        Thread.__init__(self)
        # Make consumer a daemon thread so that it doesn't block program exit
        self.daemon = True
        self.flush_at = flush_at
        self.flush_interval = flush_interval
        self.api_key = api_key
        self.host = host
        self.on_error = on_error
        self.queue = queue
        self.gzip = gzip
        self.endpoint = endpoint
        self.max_msg_size = max_msg_size
        self.capture_mode = capture_mode
        self.capture_compression = capture_compression
        self.drain_signal = drain_signal
        # Start level with the signal: a consumer built after a flush must not
        # inherit that flush's pending request.
        self._drain_seen = drain_signal.generation if drain_signal else 0
        # It's important to set running in the constructor: if we are asked to
        # pause immediately after construction, we might set running to True in
        # run() *after* we set it to False in pause... and keep running
        # forever.
        self.running = True
        self.retries = retries
        self.timeout = timeout
        self.historical_migration = historical_migration

    def run(self):
        """Runs the consumer."""
        self.log.debug("consumer is running...")
        while self.running:
            self.upload()

        self.log.debug("consumer exited.")

    def pause(self):
        """Pause the consumer."""
        self.running = False

    def upload(self):
        """Upload the next batch of items, return whether successful."""
        success = False
        batch = self.next()
        if len(batch) == 0:
            return False

        try:
            self.request(batch)
            success = True
        except Exception as e:
            self.log.error("error uploading: %s", e)
            success = False
            if self.on_error:
                try:
                    self.on_error(e, batch)
                except Exception as e:
                    self.log.error("on_error handler failed: %s", e)
        finally:
            # mark items as acknowledged from queue
            for item in batch:
                self.queue.task_done()

        return success

    def _drain_generation(self) -> int:
        """The drain request generation currently visible to this consumer."""
        return self.drain_signal.generation if self.drain_signal is not None else 0

    def next(self):
        """Return the next batch of items to upload."""
        queue = self.queue
        items: list[Any] = []

        start_time = time.monotonic()
        total_size = 0

        while len(items) < self.flush_at:
            # While draining we take only what is already queued, never waiting
            # for `flush_interval` to elapse or for `flush_at` to be reached.
            drain_generation = self._drain_generation()
            draining = drain_generation != self._drain_seen
            remaining = self.flush_interval - (time.monotonic() - start_time)
            if not draining and remaining <= 0:
                break

            # A partial batch is pending, so break the wait into slices to
            # notice a drain request that arrives while we are parked here. An
            # idle consumer still parks for the whole interval: anything a
            # caller enqueued before flush() is already in the queue and wakes
            # the blocking get() by itself.
            sliced = bool(items) and self.drain_signal is not None
            timeout = min(remaining, DRAIN_POLL_INTERVAL) if sliced else remaining

            try:
                if draining:
                    item = queue.get(block=False)
                else:
                    item = queue.get(block=True, timeout=timeout)
                item_size = len(json.dumps(item, cls=DatetimeSerializer).encode())
                if item_size > self.max_msg_size:
                    # Log only name and size: AI events may carry unredacted
                    # multimodal payloads that must not leak into logs.
                    self.log.error(
                        "Event %s (%d bytes) exceeds the %dKiB limit for %s, dropping.",
                        item.get("event") if isinstance(item, dict) else type(item),
                        item_size,
                        self.max_msg_size // 1024,
                        self.endpoint,
                    )
                    queue.task_done()
                    continue
                items.append(item)
                total_size += item_size
                if total_size >= BATCH_SIZE_LIMIT:
                    self.log.debug("hit batch size limit (size: %d)", total_size)
                    break
            except Empty:
                if draining:
                    # Everything that flush was waiting for is in `items` now.
                    # Recording the generation read at the top of this iteration
                    # (rather than the current one) leaves a request that landed
                    # while we were draining pending for the next batch.
                    self._drain_seen = drain_generation
                    break
                if timeout < remaining:
                    # Only a poll slice expired, not the batch window: keep
                    # accumulating so batching is unchanged without a flush.
                    continue
                break

        return items

    def request(self, batch):
        """Upload the batch via the wire protocol selected by `capture_mode`.

        V1 uses the partial-retry submitter (which posts to its own path); V0
        posts the batch to this consumer's `endpoint`.
        """
        if self.capture_mode == CaptureMode.V1:
            _send_v1_batch(
                self.api_key,
                self.host,
                batch,
                compression=self.capture_compression,
                timeout=self.timeout,
                max_retries=self.retries,
                historical_migration=self.historical_migration,
            )
            return
        self._send(batch, self.endpoint)

    def _send(self, batch, path):
        """Attempt to upload a single batch to `path`, retrying before raising an error"""

        def is_retryable(exc):
            if isinstance(exc, APIError):
                # retry on server errors and client errors
                # with 408 (request timeout) or 429 (rate limited),
                # don't retry on other client errors
                if isinstance(exc.status, int):
                    return not (
                        (400 <= exc.status < 500) and exc.status not in (408, 429)
                    )
                return False
            else:
                # retry on all other errors (eg. network)
                return True

        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                batch_post(
                    self.api_key,
                    self.host,
                    gzip=self.gzip,
                    timeout=self.timeout,
                    batch=batch,
                    historical_migration=self.historical_migration,
                    path=path,
                )
                return
            except Exception as e:
                last_exc = e
                if not is_retryable(e):
                    raise
                if attempt < self.retries:
                    # Respect Retry-After header if present, otherwise use exponential backoff
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after and retry_after > 0:
                        time.sleep(retry_after)
                    else:
                        time.sleep(min(2**attempt, 30))

        if last_exc:
            raise last_exc
