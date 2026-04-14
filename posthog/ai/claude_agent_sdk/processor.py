"""PostHog LLM Analytics processor for the Claude Agent SDK.

Wraps claude_agent_sdk.query() to automatically emit $ai_generation,
$ai_span, and $ai_trace events to PostHog.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        ToolUseBlock,
        UserMessage,
    )
    from claude_agent_sdk import query as original_query
    from claude_agent_sdk.types import ClaudeAgentOptions, StreamEvent
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Claude Agent SDK to use this feature: 'pip install claude-agent-sdk'"
    )

from posthog import setup
from posthog.client import Client

log = logging.getLogger("posthog")


@dataclass
class _GenerationData:
    """Data accumulated for a single LLM generation (one API call)."""

    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    stop_reason: Optional[str] = None


class _GenerationTracker:
    """Tracks StreamEvent boundaries to reconstruct per-generation metrics.

    Each message_start -> message_stop cycle in the Anthropic streaming protocol
    represents one API call (one generation).
    """

    def __init__(self) -> None:
        self._current: Optional[_GenerationData] = None
        self._completed: List[_GenerationData] = []
        self._last_model: Optional[str] = None
        self._received_stream_events: bool = False

    def process_stream_event(self, event: "StreamEvent") -> None:
        self._received_stream_events = True
        raw = event.event
        event_type = raw.get("type")

        if event_type == "message_start":
            self._current = _GenerationData(start_time=time.time())
            message = raw.get("message", {})
            self._current.model = message.get("model")
            usage = message.get("usage", {})
            self._current.input_tokens = usage.get("input_tokens", 0)
            self._current.output_tokens = usage.get("output_tokens", 0)
            self._current.cache_read_input_tokens = usage.get(
                "cache_read_input_tokens", 0
            )
            self._current.cache_creation_input_tokens = usage.get(
                "cache_creation_input_tokens", 0
            )

        elif event_type == "message_delta" and self._current is not None:
            usage = raw.get("usage", {})
            # message_delta usage reports cumulative output tokens
            if usage.get("output_tokens"):
                self._current.output_tokens = usage["output_tokens"]
            # Extract stop reason from message_delta
            delta_stop_reason = raw.get("delta", {}).get("stop_reason")
            if delta_stop_reason is not None:
                self._current.stop_reason = delta_stop_reason

        elif event_type == "message_stop" and self._current is not None:
            self._current.end_time = time.time()
            self._completed.append(self._current)
            self._last_model = self._current.model
            self._current = None

    def set_model(self, model: str) -> None:
        self._last_model = model

    @property
    def last_model(self) -> Optional[str]:
        return self._last_model

    def has_completed_generation(self) -> bool:
        return len(self._completed) > 0

    def pop_generation(self) -> _GenerationData:
        return self._completed.pop(0)

    def has_pending(self) -> bool:
        return self._current is not None

    @property
    def generation_count(self) -> int:
        return len(self._completed)

    @property
    def current_span_id(self) -> Optional[str]:
        """Span ID of the generation currently in progress (before message_stop)."""
        return self._current.span_id if self._current else None

    @property
    def had_any_stream_events(self) -> bool:
        """Whether we received any StreamEvents at all."""
        return self._received_stream_events


class PostHogClaudeAgentProcessor:
    """Wraps claude_agent_sdk.query() to emit PostHog LLM analytics events.

    Emits:
    - $ai_generation: one per Anthropic API call (reconstructed from StreamEvents)
    - $ai_span: one per tool use (ToolUseBlock in AssistantMessage)
    - $ai_trace: one per query() call (on ResultMessage)
    """

    def __init__(
        self,
        client: Optional[Client] = None,
        distinct_id: Optional[
            Union[str, Callable[["ResultMessage"], Optional[str]]]
        ] = None,
        privacy_mode: bool = False,
        groups: Optional[Dict[str, Any]] = None,
        properties: Optional[Dict[str, Any]] = None,
    ):
        self._client = client or setup()
        self._distinct_id = distinct_id
        self._privacy_mode = privacy_mode
        self._groups = groups or {}
        self._properties = properties or {}

    def _get_distinct_id(
        self, result: Optional["ResultMessage"] = None
    ) -> Optional[str]:
        if callable(self._distinct_id):
            if result:
                val = self._distinct_id(result)
                if val:
                    return str(val)
            return None
        elif self._distinct_id:
            return str(self._distinct_id)
        return None

    def _with_privacy_mode(self, value: Any) -> Any:
        if self._privacy_mode or (
            hasattr(self._client, "privacy_mode") and self._client.privacy_mode
        ):
            return None
        return value

    def _capture_event(
        self,
        event: str,
        properties: Dict[str, Any],
        distinct_id: Optional[str] = None,
        groups: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            if not hasattr(self._client, "capture") or not callable(
                self._client.capture
            ):
                return

            final_properties = {
                **properties,
                **self._properties,
            }

            self._client.capture(
                distinct_id=distinct_id or "unknown",
                event=event,
                properties=final_properties,
                groups=groups if groups is not None else self._groups,
            )
        except Exception as e:
            log.debug(f"Failed to capture PostHog event: {e}")

    async def query(
        self,
        *,
        prompt: Any,
        options: Optional[ClaudeAgentOptions] = None,
        transport: Any = None,
        posthog_distinct_id: Optional[
            Union[str, Callable[["ResultMessage"], Optional[str]]]
        ] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: Optional[bool] = None,
        posthog_groups: Optional[Dict[str, Any]] = None,
    ):
        """Drop-in replacement for claude_agent_sdk.query() with PostHog instrumentation.

        All original messages are yielded unchanged. PostHog events are emitted
        automatically in the background.

        Args:
            prompt: The prompt (same as claude_agent_sdk.query)
            options: ClaudeAgentOptions (same as claude_agent_sdk.query)
            transport: Optional transport (same as claude_agent_sdk.query)
            posthog_distinct_id: Override distinct_id for this query
            posthog_trace_id: Override trace_id for this query
            posthog_properties: Extra properties merged into all events for this query
            posthog_privacy_mode: Override privacy mode for this query
            posthog_groups: Override groups for this query
        """
        from dataclasses import replace

        # Per-call overrides
        distinct_id_override = posthog_distinct_id or self._distinct_id
        trace_id = posthog_trace_id or str(uuid.uuid4())
        extra_props = posthog_properties or {}
        privacy = (
            posthog_privacy_mode
            if posthog_privacy_mode is not None
            else self._privacy_mode
        )
        groups = posthog_groups or self._groups

        # Ensure partial messages are enabled for per-generation tracking
        if options is None:
            options = ClaudeAgentOptions(include_partial_messages=True)
        elif not options.include_partial_messages:
            options = replace(options, include_partial_messages=True)

        tracker = _GenerationTracker()
        query_start = time.time()
        generation_index = 0
        current_generation_span_id: Optional[str] = None

        # Track input/output for generation events
        initial_input: List[Dict[str, Any]] = []
        if isinstance(prompt, str):
            initial_input = [{"role": "user", "content": prompt}]
        if options and options.system_prompt and isinstance(options.system_prompt, str):
            initial_input = [
                {"role": "system", "content": options.system_prompt}
            ] + initial_input

        # Two-slot input tracking:
        # - current_input: input for the generation currently in progress
        # - next_input: tool results that arrive mid-turn, queued for the next generation
        #
        # Message ordering from the SDK is:
        #   message_start → content_blocks → AssistantMessage → UserMessage(tool result) → message_stop
        # So UserMessage arrives *before* message_stop. When message_stop fires we emit
        # with current_input, then promote next_input → current_input for the next turn.
        current_input: Optional[List[Dict[str, Any]]] = initial_input or None
        next_input: Optional[List[Dict[str, Any]]] = None

        # Accumulate assistant output per generation
        pending_output: List[Dict[str, Any]] = []

        async for message in original_query(
            prompt=prompt, options=options, transport=transport
        ):
            # All instrumentation is wrapped in try/except so PostHog errors
            # never interrupt the underlying Claude Agent SDK query.
            try:
                if isinstance(message, StreamEvent):
                    tracker.process_stream_event(message)

                    # Emit $ai_generation when a turn completes
                    if tracker.has_completed_generation():
                        gen = tracker.pop_generation()
                        generation_index += 1
                        current_generation_span_id = gen.span_id
                        self._emit_generation(
                            gen,
                            trace_id,
                            generation_index,
                            current_input,
                            pending_output or None,
                            distinct_id_override,
                            extra_props,
                            privacy,
                            groups,
                        )
                        # Promote: tool results from this turn become input for next turn
                        current_input = next_input
                        next_input = None
                        pending_output = []

                elif isinstance(message, AssistantMessage):
                    tracker.set_model(message.model)
                    # Use the in-progress generation's span_id as parent for tool spans.
                    # AssistantMessage arrives before message_stop, so current_generation_span_id
                    # would be stale (from the previous turn). tracker.current_span_id gives us
                    # the correct in-progress generation.
                    parent_id = tracker.current_span_id or current_generation_span_id
                    # Build output content from assistant blocks
                    output_content: List[Dict[str, Any]] = []
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            self._emit_tool_span(
                                block,
                                trace_id,
                                parent_id,
                                distinct_id_override,
                                extra_props,
                                privacy,
                                groups,
                            )
                            output_content.append(
                                {
                                    "type": "function",
                                    "function": {
                                        "name": block.name,
                                        "arguments": block.input,
                                    },
                                }
                            )
                        elif hasattr(block, "text"):
                            output_content.append({"type": "text", "text": block.text})
                    if output_content:
                        pending_output = [
                            {"role": "assistant", "content": output_content}
                        ]

                elif isinstance(message, UserMessage):
                    # UserMessages carry tool results. They arrive *before* message_stop
                    # for the current turn, so queue them as input for the *next* generation.
                    content = message.content
                    if isinstance(content, str):
                        next_input = [{"role": "user", "content": content}]
                    elif isinstance(content, list):
                        formatted: List[Dict[str, Any]] = []
                        for block in content:
                            if hasattr(block, "tool_use_id"):
                                formatted.append(
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": block.tool_use_id,
                                        "content": str(block.content)[:500]
                                        if block.content
                                        else None,
                                    }
                                )
                            elif hasattr(block, "text"):
                                formatted.append({"type": "text", "text": block.text})
                        if formatted:
                            next_input = [{"role": "user", "content": formatted}]

                elif isinstance(message, ResultMessage):
                    # Fallback: if no StreamEvents were received, emit a single
                    # generation from ResultMessage aggregate data
                    if not tracker.had_any_stream_events:
                        self._emit_generation_from_result(
                            message,
                            trace_id,
                            tracker.last_model,
                            query_start,
                            initial_input,
                            pending_output,
                            distinct_id_override,
                            extra_props,
                            privacy,
                            groups,
                        )

                    self._emit_trace(
                        message,
                        trace_id,
                        query_start,
                        distinct_id_override,
                        extra_props,
                        privacy,
                        groups,
                    )

            except Exception as e:
                log.debug(f"PostHog instrumentation error (non-fatal): {e}")

            yield message

    def _emit_generation(
        self,
        gen: _GenerationData,
        trace_id: str,
        generation_index: int,
        input_messages: Optional[List[Dict[str, Any]]],
        output_choices: Optional[List[Dict[str, Any]]],
        distinct_id: Any,
        extra_props: Dict[str, Any],
        privacy: bool,
        groups: Dict[str, Any],
    ) -> None:
        resolved_id = self._resolve_distinct_id(distinct_id)
        latency = (
            (gen.end_time - gen.start_time) if gen.start_time and gen.end_time else 0
        )

        properties: Dict[str, Any] = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": gen.span_id,
            "$ai_span_name": f"generation_{generation_index}",
            "$ai_provider": "anthropic",
            "$ai_framework": "claude-agent-sdk",
            "$ai_model": gen.model,
            "$ai_input_tokens": gen.input_tokens,
            "$ai_output_tokens": gen.output_tokens,
            "$ai_latency": latency,
            **extra_props,
        }

        if input_messages is not None:
            properties["$ai_input"] = (
                None if privacy else self._with_privacy_mode(input_messages)
            )
        if output_choices is not None:
            properties["$ai_output_choices"] = (
                None if privacy else self._with_privacy_mode(output_choices)
            )

        if gen.cache_read_input_tokens:
            properties["$ai_cache_read_input_tokens"] = gen.cache_read_input_tokens
        if gen.cache_creation_input_tokens:
            properties["$ai_cache_creation_input_tokens"] = (
                gen.cache_creation_input_tokens
            )

        if gen.stop_reason is not None:
            properties["$ai_stop_reason"] = gen.stop_reason

        if resolved_id is None:
            properties["$process_person_profile"] = False

        self._capture_event(
            "$ai_generation", properties, resolved_id or trace_id, groups
        )

    def _emit_generation_from_result(
        self,
        result: "ResultMessage",
        trace_id: str,
        model: Optional[str],
        query_start: float,
        input_messages: Optional[List[Dict[str, Any]]],
        output_choices: Optional[List[Dict[str, Any]]],
        distinct_id: Any,
        extra_props: Dict[str, Any],
        privacy: bool,
        groups: Dict[str, Any],
    ) -> None:
        """Fallback: emit a single generation from ResultMessage aggregate data."""
        resolved_id = self._resolve_distinct_id(distinct_id)
        usage = result.usage or {}

        properties: Dict[str, Any] = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": str(uuid.uuid4()),
            "$ai_span_name": "generation_1",
            "$ai_provider": "anthropic",
            "$ai_framework": "claude-agent-sdk",
            "$ai_model": model,
            "$ai_input_tokens": usage.get("input_tokens", 0),
            "$ai_output_tokens": usage.get("output_tokens", 0),
            "$ai_latency": result.duration_api_ms / 1000.0
            if result.duration_api_ms
            else 0,
            "$ai_is_error": result.is_error,
            **extra_props,
        }

        if input_messages is not None:
            properties["$ai_input"] = (
                None if privacy else self._with_privacy_mode(input_messages)
            )
        if output_choices is not None:
            properties["$ai_output_choices"] = (
                None if privacy else self._with_privacy_mode(output_choices)
            )

        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        if cache_read:
            properties["$ai_cache_read_input_tokens"] = cache_read
        if cache_creation:
            properties["$ai_cache_creation_input_tokens"] = cache_creation

        if result.total_cost_usd is not None:
            properties["$ai_total_cost_usd"] = result.total_cost_usd

        if resolved_id is None:
            properties["$process_person_profile"] = False

        self._capture_event(
            "$ai_generation", properties, resolved_id or trace_id, groups
        )

    def _emit_tool_span(
        self,
        block: "ToolUseBlock",
        trace_id: str,
        parent_span_id: Optional[str],
        distinct_id: Any,
        extra_props: Dict[str, Any],
        privacy: bool,
        groups: Dict[str, Any],
    ) -> None:
        resolved_id = self._resolve_distinct_id(distinct_id)

        properties: Dict[str, Any] = {
            "$ai_trace_id": trace_id,
            "$ai_span_id": str(uuid.uuid4()),
            "$ai_parent_id": parent_span_id,
            "$ai_span_name": block.name,
            "$ai_span_type": "tool",
            "$ai_provider": "anthropic",
            "$ai_framework": "claude-agent-sdk",
            **extra_props,
        }

        if not privacy and not (
            hasattr(self._client, "privacy_mode") and self._client.privacy_mode
        ):
            properties["$ai_input_state"] = _ensure_serializable(block.input)

        if resolved_id is None:
            properties["$process_person_profile"] = False

        self._capture_event("$ai_span", properties, resolved_id or trace_id, groups)

    def _emit_trace(
        self,
        result: "ResultMessage",
        trace_id: str,
        query_start: float,
        distinct_id: Any,
        extra_props: Dict[str, Any],
        privacy: bool,
        groups: Dict[str, Any],
    ) -> None:
        resolved_id = self._resolve_distinct_id(distinct_id, result)
        latency = (
            result.duration_ms / 1000.0
            if result.duration_ms
            else (time.time() - query_start)
        )

        properties: Dict[str, Any] = {
            "$ai_trace_id": trace_id,
            "$ai_trace_name": "claude_agent_sdk_query",
            "$ai_provider": "anthropic",
            "$ai_framework": "claude-agent-sdk",
            "$ai_latency": latency,
            "$ai_is_error": result.is_error,
            **extra_props,
        }

        if result.total_cost_usd is not None:
            properties["$ai_total_cost_usd"] = result.total_cost_usd

        if resolved_id is None:
            properties["$process_person_profile"] = False

        self._capture_event("$ai_trace", properties, resolved_id or trace_id, groups)

        # Flush to ensure events are sent before process exits
        try:
            if hasattr(self._client, "flush") and callable(self._client.flush):
                self._client.flush()
        except Exception as e:
            log.debug(f"Error flushing PostHog client: {e}")

    def _resolve_distinct_id(
        self,
        override: Any,
        result: Optional["ResultMessage"] = None,
    ) -> Optional[str]:
        """Resolve distinct_id from override or instance default."""
        if callable(override):
            if result:
                val = override(result)
                if val:
                    return str(val)
            return None
        elif override:
            return str(override)
        # Fall back to instance default
        return self._get_distinct_id(result)


def _ensure_serializable(obj: Any) -> Any:
    """Ensure an object is JSON-serializable."""
    if obj is None:
        return None
    try:
        import json

        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)
