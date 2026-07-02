import asyncio
import inspect
import json
import logging
from typing import Any, Optional

from .consumer import AI_MAX_MSG_SIZE, BATCH_SIZE_LIMIT, MAX_MSG_SIZE
from .request import (
    AI_EVENTS_ENDPOINT,
    EVENTS_ENDPOINT,
    APIError,
    DatetimeSerializer,
    is_ai_event,
)
from ._async_request import async_batch_post


class _AsyncConsumer:
    """Consumes messages from an asyncio.Queue and uploads them in batches."""

    log = logging.getLogger("posthog")

    def __init__(
        self,
        queue: asyncio.Queue,
        api_key: str,
        flush_at: int = 100,
        host: Optional[str] = None,
        on_error=None,
        flush_interval: float = 5.0,
        gzip: bool = False,
        retries: int = 10,
        timeout: int = 15,
        historical_migration: bool = False,
        dedicated_ai_endpoint: bool = False,
    ) -> None:
        self.flush_at = flush_at
        self.flush_interval = flush_interval
        self.api_key = api_key
        self.host = host
        self.on_error = on_error
        self.queue = queue
        self.gzip = gzip
        self.dedicated_ai_endpoint = dedicated_ai_endpoint
        self.running = True
        self.retries = retries
        self.timeout = timeout
        self.historical_migration = historical_migration

    async def run(self) -> None:
        self.log.debug("async consumer is running...")
        try:
            while self.running:
                await self.upload()
        except asyncio.CancelledError:
            raise
        finally:
            self.log.debug("async consumer exited.")

    def pause(self) -> None:
        self.running = False

    async def upload(self) -> bool:
        success = False
        batch = await self.next()
        if len(batch) == 0:
            return False

        try:
            await self.request(batch)
            success = True
        except Exception as e:
            self.log.error("error uploading: %s", e)
            success = False
            if self.on_error:
                try:
                    result = self.on_error(e, batch)
                    if inspect.isawaitable(result):
                        await result
                except Exception as error:
                    self.log.error("on_error handler failed: %s", error)
        finally:
            for _ in batch:
                self.queue.task_done()

        return success

    async def next(self) -> list[Any]:
        items: list[Any] = []
        start_time = asyncio.get_running_loop().time()
        total_size = 0

        while len(items) < self.flush_at:
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed >= self.flush_interval:
                break
            try:
                item = await asyncio.wait_for(
                    self.queue.get(), timeout=self.flush_interval - elapsed
                )
                item_size = len(json.dumps(item, cls=DatetimeSerializer).encode())
                max_msg_size = self._max_msg_size(item)
                if item_size > max_msg_size:
                    self.log.error(
                        "Item exceeds %dKiB limit, dropping. (%s)",
                        max_msg_size // 1024,
                        str(item),
                    )
                    self.queue.task_done()
                    continue
                items.append(item)
                total_size += item_size
                if total_size >= BATCH_SIZE_LIMIT:
                    self.log.debug("hit batch size limit (size: %d)", total_size)
                    break
            except asyncio.TimeoutError:
                break

        return items

    async def request(self, batch: list[Any]) -> None:
        if not self.dedicated_ai_endpoint:
            await self._send(batch, EVENTS_ENDPOINT)
            return

        ai_events: list[Any] = []
        analytics_events: list[Any] = []
        for item in batch:
            target = ai_events if is_ai_event(item.get("event")) else analytics_events
            target.append(item)

        first_exc = None
        for events, path in (
            (analytics_events, EVENTS_ENDPOINT),
            (ai_events, AI_EVENTS_ENDPOINT),
        ):
            if not events:
                continue
            try:
                await self._send(events, path)
            except Exception as e:
                if first_exc is None:
                    first_exc = e
                else:
                    self.log.error("error uploading to %s: %s", path, e)

        if first_exc is not None:
            raise first_exc

    async def _send(self, batch: list[Any], path: str) -> None:
        def is_retryable(exc):
            if isinstance(exc, APIError):
                if isinstance(exc.status, int):
                    return not (
                        (400 <= exc.status < 500) and exc.status not in (408, 429)
                    )
                return False
            return True

        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                await async_batch_post(
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
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after and retry_after > 0:
                        await asyncio.sleep(retry_after)
                    else:
                        await asyncio.sleep(min(2**attempt, 30))

        if last_exc:
            raise last_exc

    def _max_msg_size(self, item):
        if self.dedicated_ai_endpoint and is_ai_event(item.get("event")):
            return AI_MAX_MSG_SIZE
        return MAX_MSG_SIZE
