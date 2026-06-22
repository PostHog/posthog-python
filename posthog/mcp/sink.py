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

import inspect
from dataclasses import dataclass
from typing import List, Optional, Tuple

from posthog.client import Client

from .ids import _uuid7, new_prefixed_id
from .logger import log
from .posthog_events import PostHogCaptureEvent, build_posthog_capture_events
from .sanitization import sanitize_event
from .truncation import truncate_event
from .types import BeforeSendFn, Event, McpEvent


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
                    timestamp=capture_event.get("timestamp"),
                    uuid=_uuid7(),
                )
            log(
                f"Captured PostHog event {full_event.get('id')} | {full_event.get('event_type')} | "
                f"{full_event.get('duration')} ms | {full_event.get('identify_actor_given_id') or 'anonymous'}"
            )
        except Exception as err:
            log(f"Failed to capture PostHog event {full_event.get('id')}: {err}")
