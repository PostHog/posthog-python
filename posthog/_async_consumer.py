from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from ._async_request import async_batch_post, async_send_v1_batch
from .capture_compression import CaptureCompression
from .capture_mode import CaptureMode
from .consumer import BATCH_SIZE_LIMIT, MAX_MSG_SIZE
from .request import APIError, DatetimeSerializer, EVENTS_ENDPOINT

_STOP = object()
_PROCESSING_EVENT = contextvars.ContextVar(
    "posthog_async_processing_event", default=False
)


def _is_processing_event() -> bool:
    return _PROCESSING_EVENT.get()


@dataclass(frozen=True)
class _QueuedEvent:
    event: dict[str, Any]
    context: contextvars.Context


async def _invoke_callback(callback, *args):
    if inspect.iscoroutinefunction(callback):
        return await callback(*args)
    result = await asyncio.to_thread(callback, *args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _serialized_event_size(event: dict[str, Any]) -> int:
    serialized = await asyncio.to_thread(json.dumps, event, cls=DatetimeSerializer)
    return len(serialized.encode())


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
        self._carryover: Optional[tuple[dict[str, Any], int]] = None
        self._flush_event = asyncio.Event()

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

    def request_flush(self) -> None:
        self._flush_event.set()

    async def _get_or_flush(self, timeout: float) -> tuple[Any, bool]:
        get_task = asyncio.create_task(self.queue.get())
        flush_task = asyncio.create_task(self._flush_event.wait())
        done, pending = await asyncio.wait(
            {get_task, flush_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if get_task in done:
            return get_task.result(), False
        if flush_task in done:
            self._flush_event.clear()
            return None, True
        return None, False

    async def _process_queued_event(
        self, event: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        token = _PROCESSING_EVENT.set(True)
        try:
            return await self.process_event(event)
        finally:
            _PROCESSING_EVENT.reset(token)

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
                    await _invoke_callback(self.on_error, error, batch)
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

        if self._carryover is not None:
            carried_item, carried_size = self._carryover
            self._carryover = None
            items.append(carried_item)
            total_size = carried_size

        while len(items) < self.flush_at:
            remaining = self.flush_interval - (
                asyncio.get_running_loop().time() - started
            )
            if remaining <= 0:
                break

            queued, flush_requested = await self._get_or_flush(remaining)
            if flush_requested or queued is None:
                break

            if queued is _STOP:
                self.queue.task_done()
                stop = True
                break

            try:
                process_task = queued.context.run(
                    asyncio.create_task, self._process_queued_event(queued.event)
                )
                item = await process_task
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
                item_size = await _serialized_event_size(item)
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

            if items and total_size + item_size > BATCH_SIZE_LIMIT:
                self._carryover = (item, item_size)
                self.log.debug("hit async batch size limit (size: %d)", total_size)
                break

            items.append(item)
            total_size += item_size

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
