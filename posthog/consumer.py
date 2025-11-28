import json
import logging
import time
from threading import Thread

import backoff

from posthog.request import APIError, DatetimeSerializer, batch_post

try:
    from queue import Empty
except ImportError:
    from Queue import Empty


MAX_MSG_SIZE = 900 * 1024  # 900KiB per event

# The maximum request body size is currently 20MiB, let's be conservative
# in case we want to lower it in the future.
BATCH_SIZE_LIMIT = 5 * 1024 * 1024


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
        flush_interval=0.5,
        gzip=False,
        retries=10,
        timeout=15,
        historical_migration=False,
        use_ai_ingestion_pipeline=False,
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
        # It's important to set running in the constructor: if we are asked to
        # pause immediately after construction, we might set running to True in
        # run() *after* we set it to False in pause... and keep running
        # forever.
        self.running = True
        self.retries = retries
        self.timeout = timeout
        self.historical_migration = historical_migration
        self.use_ai_ingestion_pipeline = use_ai_ingestion_pipeline

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
                self.on_error(e, batch)
        finally:
            # mark items as acknowledged from queue
            for item in batch:
                self.queue.task_done()
            return success

    def next(self):
        """Return the next batch of items to upload."""
        queue = self.queue
        items = []

        start_time = time.monotonic()
        total_size = 0

        while len(items) < self.flush_at:
            elapsed = time.monotonic() - start_time
            if elapsed >= self.flush_interval:
                break
            try:
                item = queue.get(block=True, timeout=self.flush_interval - elapsed)
                item_size = len(json.dumps(item, cls=DatetimeSerializer).encode())
                if item_size > MAX_MSG_SIZE:
                    self.log.error(
                        "Item exceeds 900kib limit, dropping. (%s)", str(item)
                    )
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
        """Attempt to upload the batch and retry before raising an error"""

        def fatal_exception(exc):
            if isinstance(exc, APIError):
                # retry on server errors and client errors
                # with 429 status code (rate limited),
                # don't retry on other client errors
                if exc.status == "N/A":
                    return False
                return (400 <= exc.status < 500) and exc.status != 429
            else:
                # retry on all other errors (eg. network)
                return False

        if self.use_ai_ingestion_pipeline:
            ai_events = []
            non_ai_events = []

            for item in batch:
                event_name = item.get("event", "")
                if event_name.startswith("$ai_"):
                    ai_events.append(item)
                else:
                    non_ai_events.append(item)

            for ai_event in ai_events:
                self._send_ai_event(ai_event, fatal_exception)

            if non_ai_events:

                @backoff.on_exception(
                    backoff.expo,
                    Exception,
                    max_tries=self.retries + 1,
                    giveup=fatal_exception,
                )
                def send_batch_request():
                    batch_post(
                        self.api_key,
                        self.host,
                        gzip=self.gzip,
                        timeout=self.timeout,
                        batch=non_ai_events,
                        historical_migration=self.historical_migration,
                    )

                send_batch_request()
        else:
            @backoff.on_exception(
                backoff.expo,
                Exception,
                max_tries=self.retries + 1,
                giveup=fatal_exception,
            )
            def send_request():
                batch_post(
                    self.api_key,
                    self.host,
                    gzip=self.gzip,
                    timeout=self.timeout,
                    batch=batch,
                    historical_migration=self.historical_migration,
                )

            send_request()

    def _send_ai_event(self, event, fatal_exception):
        """Send a single AI event to the /i/v0/ai endpoint"""
        from posthog.request import ai_post
        from posthog.utils import extract_ai_blob_properties

        # Extract blob properties from the event
        properties = event.get("properties", {})
        cleaned_properties, blobs = extract_ai_blob_properties(properties)

        @backoff.on_exception(
            backoff.expo, Exception, max_tries=self.retries + 1, giveup=fatal_exception
        )
        def send_ai_request():
            ai_post(
                self.api_key,
                self.host,
                gzip=self.gzip,
                timeout=self.timeout,
                event_name=event.get("event"),
                distinct_id=event.get("distinct_id"),
                properties=cleaned_properties,
                blobs=blobs,
                timestamp=event.get("timestamp"),
                uuid=event.get("uuid"),
            )

        send_ai_request()
