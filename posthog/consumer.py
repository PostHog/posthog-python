from typing import Any, Optional
import json
import logging
import time
from threading import Thread

from posthog._logging import _configure_posthog_logging
from posthog.capture_compression import CaptureCompression
from posthog.capture_mode import CaptureMode
from posthog.capture_v1 import _backoff, _send_v1_batch
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

_configure_posthog_logging()


class _DrainSignal:
    """Wake queue consumers while one or more drain requests are active."""

    def __init__(self, queue) -> None:
        self._queue = queue
        self._requests = 0

    def request(self) -> None:
        with self._queue.not_empty:
            self._requests += 1
            self._queue.not_empty.notify_all()

    def complete(self) -> None:
        with self._queue.not_empty:
            self._requests -= 1
            self._queue.not_empty.notify_all()

    def stop(self, consumer, drain: bool) -> None:
        """Publish a consumer stop under the queue's dequeue lock."""
        with self._queue.not_empty:
            consumer.running = False
            consumer._drain_on_stop = drain
            self._queue.not_empty.notify_all()

    def wait_until_inactive_or_work(self, consumer) -> None:
        with self._queue.not_empty:
            while self._requests and not self._queue._qsize() and consumer.running:
                self._queue.not_empty.wait()

    @property
    def requested(self) -> bool:
        with self._queue.mutex:
            return self._requests > 0

    def draining(self, consumer) -> bool:
        with self._queue.mutex:
            return self._requests > 0 and (consumer.running or consumer._drain_on_stop)

    def get(self, timeout: float, consumer=None):
        """Get an item, or wake with ``Empty`` when draining or stopping."""
        with self._queue.not_empty:
            deadline = time.monotonic() + timeout
            while True:
                draining = self._requests > 0 and (
                    consumer is None or consumer.running or consumer._drain_on_stop
                )
                if consumer is not None and not consumer.running and not draining:
                    raise Empty
                if self._queue._qsize():
                    break
                if draining:
                    raise Empty
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Empty
                self._queue.not_empty.wait(remaining)
            item = self._queue._get()
            self._queue.not_full.notify()
            return item


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
        self._drain_signal: Optional[_DrainSignal] = None
        self._drain_on_stop = False
        # It's important to set running in the constructor: if we are asked to
        # pause immediately after construction, we might set running to True in
        # run() *after* we set it to False in pause... and keep running
        # forever.
        self.running = True
        self.retries = max(0, retries)
        self.timeout = timeout
        self.historical_migration = historical_migration

    def run(self):
        """Runs the consumer."""
        self.log.debug("consumer is running...")
        while self.running:
            self.upload()
            if self._drain_signal is not None:
                self._drain_signal.wait_until_inactive_or_work(self)

        self.log.debug("consumer exited.")

    def pause(self):
        """Pause the consumer without admitting additional queued work."""
        self._pause(drain=False)

    def _pause(self, drain: bool) -> None:
        if self._drain_signal is not None:
            self._drain_signal.stop(self, drain)
        else:
            self.running = False
            self._drain_on_stop = drain

    def upload(self):
        """Upload the next batch of items, return whether successful."""
        success = False
        batch = self.next()
        if len(batch) == 0:
            return False

        try:
            if not self._can_upload():
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

    def _set_drain_signal(self, drain_signal: _DrainSignal) -> None:
        self._drain_signal = drain_signal

    def _draining(self) -> bool:
        return (
            self._drain_signal.draining(self)
            if self._drain_signal is not None
            else False
        )

    def _can_upload(self) -> bool:
        if self._drain_signal is None:
            return self.running or self._drain_on_stop
        # This lock-protected check is the request admission point. A request
        # admitted before a stop may finish on the daemon consumer, but a stop
        # prevents any later buffered or queued batch from being admitted.
        with self.queue.mutex:
            return self.running or self._drain_on_stop

    def next(self):
        """Return the next batch of items to upload."""
        queue = self.queue
        items: list[Any] = []

        start_time = time.monotonic()
        total_size = 0
        pending_items = 0

        try:
            while len(items) < self.flush_at:
                # While draining we take only what is already queued, never waiting
                # for `flush_interval` to elapse or for `flush_at` to be reached.
                draining = self._draining()
                if not self.running and not draining:
                    break
                remaining = self.flush_interval - (time.monotonic() - start_time)
                if not draining and remaining <= 0:
                    break

                try:
                    if self._drain_signal is not None:
                        item = self._drain_signal.get(
                            timeout=0 if draining else remaining,
                            consumer=self,
                        )
                    else:
                        item = queue.get(block=True, timeout=remaining)
                    pending_items += 1
                    try:
                        item_size = len(
                            json.dumps(item, cls=DatetimeSerializer).encode()
                        )
                    except Exception:
                        # Callback-modified events can still contain invalid mapping
                        # keys or circular references. Never log the payload here.
                        self.log.error(
                            "Unable to serialize queued event for sizing, dropping."
                        )
                        queue.task_done()
                        pending_items -= 1
                        continue
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
                        pending_items -= 1
                        continue
                    items.append(item)
                    total_size += item_size
                    if total_size >= BATCH_SIZE_LIMIT:
                        self.log.debug("hit batch size limit (size: %d)", total_size)
                        break
                except Empty:
                    break
        except BaseException:
            for _ in range(pending_items):
                queue.task_done()
            raise

        if not self._can_upload():
            for _ in range(pending_items):
                queue.task_done()
            return []

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
                    _backoff(attempt, getattr(e, "retry_after", None))

        if last_exc:
            raise last_exc
