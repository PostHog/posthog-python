from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from ._async_request import async_batch_post, async_send_v1_batch
from .capture_compression import CaptureCompression
from .capture_mode import CaptureMode
from .consumer import BATCH_SIZE_LIMIT, MAX_MSG_SIZE
from .request import APIError, DatetimeSerializer, EVENTS_ENDPOINT

_FLUSH = object()
_STOP = object()


class _AsyncConsumer:
    """Consume an asyncio queue and upload capture batches."""

    log = logging.getLogger("posthog")

    def __init__(
        self,
        queue: asyncio.Queue[Any],
        api_key: str,
        *,
        host: Optional[str],
        on_error: Optional[Callable[[Exception, list[dict[str, Any]]], Any]],
        process_event: Callable[[dict[str, Any]], Awaitable[Optional[dict[str, Any]]]],
        flush_at: int,
        flush_interval: float,
        gzip: bool,
        retries: int,
        timeout: int,
        historical_migration: bool,
        capture_mode: CaptureMode,
        capture_compression: CaptureCompression,
        http_client: Optional[Any],
    ) -> None:
        self.queue = queue
        self.api_key = api_key
        self.host = host
        self.on_error = on_error
        self.process_event = process_event
        self.flush_at = flush_at
        self.flush_interval = flush_interval
        self.gzip = gzip
        self.retries = max(0, retries)
        self.timeout = timeout
        self.historical_migration = historical_migration
        self.capture_mode = capture_mode
        self.capture_compression = capture_compression
        self.http_client = http_client

    async def run(self) -> None:
        self.log.debug("async consumer is running")
        try:
            while True:
                batch, stop = await self.next()
                if batch:
                    await self.upload(batch)
                if stop:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.log.error(
                "async consumer stopped after an unexpected %s", type(error).__name__
            )
        finally:
            self.log.debug("async consumer exited")

    async def upload(self, batch: list[dict[str, Any]]) -> None:
        try:
            await self.request(batch)
        except Exception as error:
            self.log.error(
                "async capture upload failed (%s, status=%s)",
                type(error).__name__,
                getattr(error, "status", None),
            )
            if self.on_error:
                try:
                    result = self.on_error(error, batch)
                    if inspect.isawaitable(result):
                        await result
                except Exception as callback_error:
                    self.log.error(
                        "on_error handler failed (%s)", type(callback_error).__name__
                    )
        finally:
            for _ in batch:
                self.queue.task_done()

    async def next(self) -> tuple[list[dict[str, Any]], bool]:
        items: list[dict[str, Any]] = []
        total_size = 0
        stop = False
        started = asyncio.get_running_loop().time()

        while len(items) < self.flush_at:
            remaining = self.flush_interval - (
                asyncio.get_running_loop().time() - started
            )
            if remaining <= 0:
                break

            try:
                queued = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break

            if queued is _FLUSH:
                self.queue.task_done()
                break
            if queued is _STOP:
                self.queue.task_done()
                stop = True
                break

            try:
                item = await self.process_event(queued)
            except Exception as error:
                self.log.error(
                    "unable to process queued event, dropping (%s)",
                    type(error).__name__,
                )
                self.queue.task_done()
                continue

            if item is None:
                self.queue.task_done()
                continue

            try:
                serialized = await asyncio.to_thread(
                    json.dumps, item, cls=DatetimeSerializer
                )
                item_size = len(serialized.encode())
            except Exception:
                self.log.error("unable to serialize queued event for sizing, dropping")
                self.queue.task_done()
                continue

            if item_size > MAX_MSG_SIZE:
                self.log.error(
                    "Event %s (%d bytes) exceeds the %dKiB limit, dropping.",
                    item.get("event"),
                    item_size,
                    MAX_MSG_SIZE // 1024,
                )
                self.queue.task_done()
                continue

            items.append(item)
            total_size += item_size
            if total_size >= BATCH_SIZE_LIMIT:
                self.log.debug("hit async batch size limit (size: %d)", total_size)
                break

        return items, stop

    async def request(self, batch: list[dict[str, Any]]) -> None:
        if self.capture_mode == CaptureMode.V1:
            await async_send_v1_batch(
                self.api_key,
                self.host,
                batch,
                compression=self.capture_compression,
                timeout=self.timeout,
                max_retries=self.retries,
                historical_migration=self.historical_migration,
            )
            return

        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                await async_batch_post(
                    self.api_key,
                    self.host,
                    batch=batch,
                    path=EVENTS_ENDPOINT,
                    gzip=self.gzip,
                    timeout=self.timeout,
                    historical_migration=self.historical_migration,
                    client=self.http_client,
                )
                return
            except Exception as error:
                last_error = error
                if not self._is_retryable(error) or attempt >= self.retries:
                    raise
                retry_after = getattr(error, "retry_after", None)
                delay = max(
                    min(2**attempt, 30),
                    min(retry_after, 30) if retry_after and retry_after > 0 else 0,
                )
                await asyncio.sleep(delay)

        if last_error is not None:  # pragma: no cover - loop always raises first
            raise last_error

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        if not isinstance(error, APIError):
            return True
        if not isinstance(error.status, int):
            return False
        return not (400 <= error.status < 500 and error.status not in (408, 429))
