from typing import Any
import json
import logging
import time
from threading import Thread

from posthog._logging import _configure_posthog_logging
from posthog.capture_compression import CaptureCompression
from posthog.capture_mode import CaptureMode
from posthog.capture_v1 import _send_v1_batch
from posthog.request import (
    AI_EVENTS_ENDPOINT,
    EVENTS_ENDPOINT,
    APIError,
    DatetimeSerializer,
    batch_post,
    is_ai_event,
)

from queue import Empty


MAX_MSG_SIZE = 900 * 1024  # 900KiB per event

# `$ai_*` events carry LLM inputs/outputs and, when routed to the dedicated AI
# endpoint, hit a pipeline that accepts larger messages than analytics ingestion,
# so they get a higher per-event ceiling when that routing is enabled.
AI_MAX_MSG_SIZE = 8 * 1024 * 1024  # 8MiB per `$ai_*` event

# The maximum request body size is currently 20MiB, let's be conservative
# in case we want to lower it in the future.
BATCH_SIZE_LIMIT = 5 * 1024 * 1024


_configure_posthog_logging()


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
        dedicated_ai_endpoint=False,
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
        self.dedicated_ai_endpoint = dedicated_ai_endpoint
        self.capture_mode = capture_mode
        self.capture_compression = capture_compression
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

    def next(self):
        """Return the next batch of items to upload."""
        queue = self.queue
        items: list[Any] = []

        start_time = time.monotonic()
        total_size = 0

        while len(items) < self.flush_at:
            elapsed = time.monotonic() - start_time
            if elapsed >= self.flush_interval:
                break
            try:
                item = queue.get(block=True, timeout=self.flush_interval - elapsed)
                item_size = len(json.dumps(item, cls=DatetimeSerializer).encode())
                max_msg_size = self._max_msg_size(item)
                if item_size > max_msg_size:
                    self.log.error(
                        "Item exceeds %dKiB limit, dropping. (%s)",
                        max_msg_size // 1024,
                        str(item),
                    )
                    queue.task_done()
                    continue
                items.append(item)
                total_size += item_size
                if total_size >= BATCH_SIZE_LIMIT:
                    self.log.debug("hit batch size limit (size: %d)", total_size)
                    break
            except Empty:
                break

        return items

    def request(self, batch):
        """Upload the batch, routing `$ai_*` events to their own endpoint when enabled.

        Each destination is attempted independently so a failure on one does not
        skip the other. The first failure is raised (so `upload()` logs it and
        invokes `on_error`); a second is logged here so it isn't silently lost.
        The batch was already dequeued in `upload()`, so unsent events are dropped
        after retries, same as the single-endpoint path.

        The analytics destination follows `capture_mode` (v1 -> partial-retry
        submitter); the dedicated AI endpoint has no v1 form and always uses the
        legacy submitter.
        """
        if not self.dedicated_ai_endpoint:
            self._send_analytics(batch)
            return

        ai_events: list[Any] = []
        analytics_events: list[Any] = []
        for item in batch:
            target = ai_events if is_ai_event(item.get("event")) else analytics_events
            target.append(item)

        first_exc = None
        for events, label, sender in (
            (analytics_events, "analytics", self._send_analytics),
            (ai_events, "ai", self._send_ai),
        ):
            if not events:
                continue
            try:
                sender(events)
            except Exception as e:
                if first_exc is None:
                    first_exc = e
                else:
                    self.log.error("error uploading to %s: %s", label, e)

        if first_exc is not None:
            raise first_exc

    def _send_analytics(self, batch):
        """Submit analytics events via the wire protocol selected by `capture_mode`."""
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
        self._send(batch, EVENTS_ENDPOINT)

    def _send_ai(self, batch):
        """Submit `$ai_*` events to the dedicated legacy AI endpoint (no v1 form)."""
        self._send(batch, AI_EVENTS_ENDPOINT)

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

    def _max_msg_size(self, item):
        if self.dedicated_ai_endpoint and is_ai_event(item.get("event")):
            return AI_MAX_MSG_SIZE
        return MAX_MSG_SIZE
