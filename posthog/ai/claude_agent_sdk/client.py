"""PostHog-instrumented ClaudeSDKClient for stateful multi-turn conversations.

Wraps claude_agent_sdk.ClaudeSDKClient to automatically emit $ai_generation,
$ai_span, and $ai_trace events across multiple conversation turns.
"""

import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Union

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeSDKClient,
        ResultMessage,
        ToolUseBlock,
        UserMessage,
    )
    from claude_agent_sdk.types import ClaudeAgentOptions, StreamEvent
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Claude Agent SDK to use this feature: 'pip install claude-agent-sdk'"
    )

from posthog.ai.claude_agent_sdk.processor import (
    PostHogClaudeAgentProcessor,
    _GenerationTracker,
)
from posthog.client import Client

log = logging.getLogger("posthog")


class PostHogClaudeSDKClient:
    """Wraps ClaudeSDKClient for stateful multi-turn conversations with PostHog instrumentation.

    Usage:
        async with PostHogClaudeSDKClient(options, posthog_client=ph, posthog_distinct_id="user") as client:
            await client.query("Hello")
            async for msg in client.receive_response():
                ...  # turn 1, emits $ai_generation events
            await client.query("Follow up")
            async for msg in client.receive_response():
                ...  # turn 2, same trace, has conversation history
    """

    def __init__(
        self,
        options: Optional["ClaudeAgentOptions"] = None,
        transport: Any = None,
        *,
        posthog_client: Optional[Client] = None,
        posthog_distinct_id: Optional[
            Union[str, Callable[["ResultMessage"], Optional[str]]]
        ] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
    ):
        from dataclasses import replace as dc_replace

        # Ensure partial messages for per-generation tracking
        if options is None:
            options = ClaudeAgentOptions(include_partial_messages=True)
        elif not options.include_partial_messages:
            options = dc_replace(options, include_partial_messages=True)

        self._client = ClaudeSDKClient(options, transport)
        self._processor = PostHogClaudeAgentProcessor(
            client=posthog_client,
            distinct_id=posthog_distinct_id,
            privacy_mode=posthog_privacy_mode,
            groups=posthog_groups,
            properties=posthog_properties or {},
        )
        self._trace_id = posthog_trace_id or str(uuid.uuid4())
        self._distinct_id = posthog_distinct_id
        self._extra_props = posthog_properties or {}
        self._privacy = posthog_privacy_mode
        self._groups = posthog_groups or {}

        # Shared state across turns
        self._tracker = _GenerationTracker()
        self._generation_index = 0
        self._current_generation_span_id: Optional[str] = None
        self._current_input: Optional[List[Dict[str, Any]]] = None
        self._next_input: Optional[List[Dict[str, Any]]] = None
        self._pending_output: List[Dict[str, Any]] = []
        self._query_start = time.time()

    async def connect(self, prompt: Any = None) -> None:
        await self._client.connect(prompt)

    async def query(self, prompt: str, session_id: str = "default") -> None:
        # Track the prompt as input for the next generation
        self._current_input = [{"role": "user", "content": prompt}]
        await self._client.query(prompt, session_id)

    async def receive_response(self):
        """Instrumented receive_response -- yields all messages, emits PostHog events."""
        async for message in self._client.receive_response():
            try:
                if isinstance(message, StreamEvent):
                    self._tracker.process_stream_event(message)

                    if self._tracker.has_completed_generation():
                        gen = self._tracker.pop_generation()
                        self._generation_index += 1
                        self._current_generation_span_id = gen.span_id
                        self._processor._emit_generation(
                            gen,
                            self._trace_id,
                            self._generation_index,
                            self._current_input,
                            self._pending_output or None,
                            self._distinct_id,
                            self._extra_props,
                            self._privacy,
                            self._groups,
                        )
                        self._current_input = self._next_input
                        self._next_input = None
                        self._pending_output = []

                elif isinstance(message, AssistantMessage):
                    self._tracker.set_model(message.model)
                    parent_id = (
                        self._tracker.current_span_id
                        or self._current_generation_span_id
                    )
                    output_content: List[Dict[str, Any]] = []
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            self._processor._emit_tool_span(
                                block,
                                self._trace_id,
                                parent_id,
                                self._distinct_id,
                                self._extra_props,
                                self._privacy,
                                self._groups,
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
                        self._pending_output = [
                            {"role": "assistant", "content": output_content}
                        ]

                elif isinstance(message, UserMessage):
                    content = message.content
                    if isinstance(content, str):
                        self._next_input = [{"role": "user", "content": content}]
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
                            self._next_input = [{"role": "user", "content": formatted}]

                elif isinstance(message, ResultMessage):
                    if not self._tracker.had_any_stream_events:
                        self._processor._emit_generation_from_result(
                            message,
                            self._trace_id,
                            self._tracker.last_model,
                            self._query_start,
                            self._current_input,
                            self._pending_output,
                            self._distinct_id,
                            self._extra_props,
                            self._privacy,
                            self._groups,
                        )
                    # Don't emit trace here -- wait for disconnect/close
                    # so multi-turn sessions get one trace at the end

            except Exception as e:
                log.debug(f"PostHog instrumentation error (non-fatal): {e}")

            yield message

    async def disconnect(self) -> None:
        # Emit the trace event covering the entire session
        try:
            latency = time.time() - self._query_start
            resolved_id = self._processor._resolve_distinct_id(self._distinct_id)

            properties: Dict[str, Any] = {
                "$ai_trace_id": self._trace_id,
                "$ai_trace_name": "claude_agent_sdk_session",
                "$ai_provider": "anthropic",
                "$ai_framework": "claude-agent-sdk",
                "$ai_latency": latency,
                **self._extra_props,
            }

            if resolved_id is None:
                properties["$process_person_profile"] = False

            self._processor._capture_event(
                "$ai_trace",
                properties,
                resolved_id or self._trace_id,
                self._groups,
            )

            try:
                ph = self._processor._client
                if hasattr(ph, "flush") and callable(ph.flush):
                    ph.flush()
            except Exception as e:
                log.debug(f"Error flushing PostHog client: {e}")

        except Exception as e:
            log.debug(f"PostHog trace emission error (non-fatal): {e}")

        await self._client.disconnect()

    # Delegate other methods
    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def set_permission_mode(self, mode: str) -> None:
        await self._client.set_permission_mode(mode)

    async def set_model(self, model: Optional[str] = None) -> None:
        await self._client.set_model(model)

    async def __aenter__(self) -> "PostHogClaudeSDKClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        await self.disconnect()
        return False
