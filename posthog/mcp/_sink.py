# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""The capture pipeline: sanitize -> truncate -> fan out into ``$mcp_*`` /
``$exception`` payloads -> ``before_send`` -> ``Client.capture()``.

``process_mcp_event`` is the single source of truth for the transform, so tests
assert on exactly the payloads that reach ``capture()``. ``McpEventSink`` wraps a
user-supplied posthog ``Client`` and does the actual capture. The SDK never owns
the client lifecycle — the host constructs it and calls ``shutdown()``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import os
import threading
import weakref
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from posthog.client import Client

from ._ids import _uuid7, new_prefixed_id
from .logger import log
from ._posthog_events import PostHogCaptureEvent, build_posthog_capture_events
from ._sanitization import sanitize_event
from ._truncation import truncate_event
from .types import BeforeSendFn, Event, McpEvent


# The client flush running on each client's flush thread, so overlapping
# analytics.flush() calls share one blocked thread instead of starting their own.
_client_flushes: "weakref.WeakKeyDictionary[Any, concurrent.futures.Future[None]]" = (
    weakref.WeakKeyDictionary()
)
_client_flushes_lock = threading.Lock()


def _forget_client_flushes_after_fork() -> None:
    global _client_flushes_lock
    _client_flushes_lock = threading.Lock()
    _client_flushes.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_client_flushes_after_fork)


def _start_client_flush(client: Any) -> "concurrent.futures.Future[None]":
    done: concurrent.futures.Future[None] = concurrent.futures.Future()
    # A running future ignores cancel(), so cancelling an await on it can't make
    # the worker's set_result() raise.
    done.set_running_or_notify_cancel()

    def run() -> None:
        try:
            client.flush()
            done.set_result(None)
        except BaseException as err:
            done.set_exception(err)
        finally:
            with _client_flushes_lock:
                if _client_flushes.get(client) is done:
                    del _client_flushes[client]

    threading.Thread(target=run, name="posthog-mcp-flush", daemon=True).start()
    return done


def _client_flush_started_after(
    client: Any, earlier: "Optional[concurrent.futures.Future[None]]"
) -> "concurrent.futures.Future[None]":
    with _client_flushes_lock:
        current = _client_flushes.get(client)
        if current is not None and current is not earlier and not current.done():
            return current
        current = _start_client_flush(client)
        _client_flushes[client] = current
        return current


@dataclass
class McpCaptureOptions:
    """Per-event toggles consulted by the sink when fanning out an event."""

    enable_exception_autocapture: bool = True
    before_send: Optional[BeforeSendFn] = None


async def process_mcp_event(
    event: McpEvent, options: McpCaptureOptions
) -> Optional[Tuple[Event, List[PostHogCaptureEvent]]]:
    """Run an MCP event through the full transform. Returns ``None`` (and logs)
    if a transform stage raises, so the event is dropped rather than partially
    sent. Payloads dropped by ``before_send`` are filtered out."""
    processed: McpEvent = event

    try:
        processed = sanitize_event(processed)
    except Exception as err:
        log(f"Failed to sanitize event: {err}")
        return None

    try:
        processed = truncate_event(processed)
    except Exception as err:
        log(f"Failed to truncate event: {err}")
        return None

    if not processed.get("id"):
        processed["id"] = new_prefixed_id("evt")

    built = build_posthog_capture_events(
        processed, options.enable_exception_autocapture
    )
    captures = await _apply_before_send(built, options.before_send)
    return processed, captures


async def _apply_before_send(
    captures: List[PostHogCaptureEvent], before_send: Optional[BeforeSendFn]
) -> List[PostHogCaptureEvent]:
    if before_send is None:
        return captures

    kept: List[PostHogCaptureEvent] = []
    for capture in captures:
        try:
            result = before_send(capture)
            if inspect.isawaitable(result):
                result = await result
            if result:
                kept.append(result)
        except Exception as err:
            log(
                f"before_send threw for event {capture.get('event')}; dropping it: {err}"
            )
    return kept


class McpEventSink:
    """Wraps a user-supplied posthog ``Client`` and pushes events through the
    pipeline. Errors at any stage are logged and the event dropped, never
    re-raised into tool code."""

    def __init__(self, posthog: Client) -> None:
        self._posthog = posthog

    async def capture(self, event: McpEvent, options: McpCaptureOptions) -> None:
        result = await process_mcp_event(event, options)
        if result is None:
            return

        full_event, captures = result
        try:
            for capture_event in captures:
                self._posthog.capture(
                    capture_event["event"],
                    distinct_id=capture_event["distinct_id"],
                    properties=capture_event["properties"],
                    options=capture_event.get("options"),
                    timestamp=capture_event.get("timestamp"),
                    uuid=_uuid7(),
                )
            log(
                f"Captured PostHog event {full_event.get('id')} | {full_event.get('event_type')} | "
                f"{full_event.get('duration')} ms | {full_event.get('identify_actor_given_id') or 'anonymous'}"
            )
        except Exception as err:
            log(f"Failed to capture PostHog event {full_event.get('id')}: {err}")

    async def flush(self) -> None:
        # Client.flush() blocks for up to its timeout when ingest stalls, so it runs
        # on its own thread, never a shared pool. A flush already running when we
        # were called may have missed our events: wait for it, then join or start
        # one that began after.
        try:
            with _client_flushes_lock:
                running = _client_flushes.get(self._posthog)
            if running is not None:
                # Its failure is logged by the caller that started it.
                with contextlib.suppress(Exception):
                    await asyncio.wrap_future(running)
            await asyncio.wrap_future(
                _client_flush_started_after(self._posthog, running)
            )
        except Exception as err:
            log(f"Failed to flush PostHog events: {err}")
