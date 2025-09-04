try:
    import anthropic
    from anthropic.resources import AsyncMessages
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Anthropic SDK to use this feature: 'pip install anthropic'"
    )

import time
import uuid
from typing import Any, Dict, List, Optional

from posthog import setup
from posthog.ai.types import StreamingContentBlock, TokenUsage, ToolInProgress
from posthog.ai.utils import (
    call_llm_and_track_usage_async,
    extract_available_tool_calls,
    get_model_params,
    merge_system_prompt,
    merge_usage_stats,
    with_privacy_mode,
)
from posthog.ai.anthropic.anthropic_converter import (
    format_anthropic_streaming_content,
    extract_anthropic_usage_from_event,
    handle_anthropic_content_block_start,
    handle_anthropic_text_delta,
    handle_anthropic_tool_delta,
    finalize_anthropic_tool_input,
)
from posthog.ai.sanitization import sanitize_anthropic
from posthog.client import Client as PostHogClient


class AsyncAnthropic(anthropic.AsyncAnthropic):
    """
    An async wrapper around the Anthropic SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    def __init__(self, posthog_client: Optional[PostHogClient] = None, **kwargs):
        """
        Args:
            posthog_client: PostHog client for tracking usage
            **kwargs: Additional arguments passed to the Anthropic client
        """
        super().__init__(**kwargs)
        self._ph_client = posthog_client or setup()
        self.messages = AsyncWrappedMessages(self)


class AsyncWrappedMessages(AsyncMessages):
    _client: AsyncAnthropic

    async def create(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """
        Create a message using Anthropic's API while tracking usage in PostHog.

        Args:
            posthog_distinct_id: Optional ID to associate with the usage event
            posthog_trace_id: Optional trace UUID for linking events
            posthog_properties: Optional dictionary of extra properties to include in the event
            posthog_privacy_mode: Whether to redact sensitive information in tracking
            posthog_groups: Optional group analytics properties
            **kwargs: Arguments passed to Anthropic's messages.create
        """

        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        if kwargs.get("stream", False):
            return await self._create_streaming(
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
                **kwargs,
            )

        return await call_llm_and_track_usage_async(
            posthog_distinct_id,
            self._client._ph_client,
            "anthropic",
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            self._client.base_url,
            super().create,
            **kwargs,
        )

    async def stream(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        return await self._create_streaming(
            posthog_distinct_id,
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            **kwargs,
        )

    async def _create_streaming(
        self,
        posthog_distinct_id: Optional[str],
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool,
        posthog_groups: Optional[Dict[str, Any]],
        **kwargs: Any,
    ):
        start_time = time.time()
        usage_stats: TokenUsage = TokenUsage(input_tokens=0, output_tokens=0)
        accumulated_content = ""
        content_blocks: List[StreamingContentBlock] = []
        tools_in_progress: Dict[str, ToolInProgress] = {}
        current_text_block: Optional[StreamingContentBlock] = None
        response = await super().create(**kwargs)

        async def generator():
            nonlocal usage_stats
            nonlocal accumulated_content
            nonlocal content_blocks
            nonlocal tools_in_progress
            nonlocal current_text_block

            try:
                async for event in response:
                    # Extract usage stats from event
                    event_usage = extract_anthropic_usage_from_event(event)
                    merge_usage_stats(usage_stats, event_usage)

                    # Handle content block start events
                    if hasattr(event, "type") and event.type == "content_block_start":
                        block, tool = handle_anthropic_content_block_start(event)

                        if block:
                            content_blocks.append(block)

                            if block.get("type") == "text":
                                current_text_block = block
                            else:
                                current_text_block = None

                        if tool:
                            tool_id = tool["block"].get("id")
                            if tool_id:
                                tools_in_progress[tool_id] = tool

                    # Handle text delta events
                    delta_text = handle_anthropic_text_delta(event, current_text_block)

                    if delta_text:
                        accumulated_content += delta_text

                    # Handle tool input delta events
                    handle_anthropic_tool_delta(
                        event, content_blocks, tools_in_progress
                    )

                    # Handle content block stop events
                    if hasattr(event, "type") and event.type == "content_block_stop":
                        current_text_block = None
                        finalize_anthropic_tool_input(
                            event, content_blocks, tools_in_progress
                        )

                    yield event

            finally:
                end_time = time.time()
                latency = end_time - start_time

                await self._capture_streaming_event(
                    posthog_distinct_id,
                    posthog_trace_id,
                    posthog_properties,
                    posthog_privacy_mode,
                    posthog_groups,
                    kwargs,
                    usage_stats,
                    latency,
                    content_blocks,
                    accumulated_content,
                )

        return generator()

    async def _capture_streaming_event(
        self,
        posthog_distinct_id: Optional[str],
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool,
        posthog_groups: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
        usage_stats: TokenUsage,
        latency: float,
        content_blocks: List[StreamingContentBlock],
        accumulated_content: str,
    ):
        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        # Format output using converter
        formatted_content = format_anthropic_streaming_content(content_blocks)
        formatted_output = []

        if formatted_content:
            formatted_output = [{"role": "assistant", "content": formatted_content}]
        else:
            # Fallback to accumulated content if no blocks
            formatted_output = [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": accumulated_content}],
                }
            ]

        event_properties = {
            "$ai_provider": "anthropic",
            "$ai_model": kwargs.get("model"),
            "$ai_model_parameters": get_model_params(kwargs),
            "$ai_input": with_privacy_mode(
                self._client._ph_client,
                posthog_privacy_mode,
                sanitize_anthropic(merge_system_prompt(kwargs, "anthropic")),
            ),
            "$ai_output_choices": with_privacy_mode(
                self._client._ph_client,
                posthog_privacy_mode,
                formatted_output,
            ),
            "$ai_http_status": 200,
            "$ai_input_tokens": usage_stats.get("input_tokens", 0),
            "$ai_output_tokens": usage_stats.get("output_tokens", 0),
            "$ai_cache_read_input_tokens": usage_stats.get(
                "cache_read_input_tokens", 0
            ),
            "$ai_cache_creation_input_tokens": usage_stats.get(
                "cache_creation_input_tokens", 0
            ),
            "$ai_latency": latency,
            "$ai_trace_id": posthog_trace_id,
            "$ai_base_url": str(self._client.base_url),
            **(posthog_properties or {}),
        }

        # Add tools if available
        available_tools = extract_available_tool_calls("anthropic", kwargs)

        if available_tools:
            event_properties["$ai_tools"] = available_tools

        if posthog_distinct_id is None:
            event_properties["$process_person_profile"] = False

        if hasattr(self._client._ph_client, "capture"):
            self._client._ph_client.capture(
                distinct_id=posthog_distinct_id or posthog_trace_id,
                event="$ai_generation",
                properties=event_properties,
                groups=posthog_groups,
            )
