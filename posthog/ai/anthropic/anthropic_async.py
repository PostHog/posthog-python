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
from posthog.ai.utils import (
    call_llm_and_track_usage_async,
    extract_available_tool_calls,
    get_model_params,
    merge_system_prompt,
    with_privacy_mode,
)
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
        usage_stats: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        accumulated_content = ""
        content_blocks: List[Dict[str, Any]] = []
        tools_in_progress: Dict[str, Dict[str, Any]] = {}
        current_text_block: Optional[Dict[str, Any]] = None
        response = await super().create(**kwargs)

        async def generator():
            nonlocal usage_stats
            nonlocal accumulated_content
            nonlocal content_blocks
            nonlocal tools_in_progress
            nonlocal current_text_block
            try:
                async for event in response:
                    # Handle usage stats from message_start event
                    if hasattr(event, "type") and event.type == "message_start":
                        if hasattr(event, "message") and hasattr(event.message, "usage"):
                            usage_stats["input_tokens"] = getattr(event.message.usage, "input_tokens", 0)
                            usage_stats["cache_creation_input_tokens"] = getattr(event.message.usage, "cache_creation_input_tokens", 0)
                            usage_stats["cache_read_input_tokens"] = getattr(event.message.usage, "cache_read_input_tokens", 0)

                    # Handle usage stats from message_delta event
                    if hasattr(event, "usage") and event.usage:
                        usage_stats["output_tokens"] = getattr(event.usage, "output_tokens", 0)

                    # Handle content block start events
                    if hasattr(event, "type") and event.type == "content_block_start":
                        if hasattr(event, "content_block"):
                            block = event.content_block
                            if hasattr(block, "type"):
                                if block.type == "text":
                                    current_text_block = {
                                        "type": "text",
                                        "text": ""
                                    }
                                    content_blocks.append(current_text_block)
                                elif block.type == "tool_use":
                                    tool_block = {
                                        "type": "function",
                                        "id": getattr(block, "id", ""),
                                        "function": {
                                            "name": getattr(block, "name", ""),
                                            "arguments": {}
                                        }
                                    }
                                    content_blocks.append(tool_block)
                                    tools_in_progress[block.id] = {
                                        "block": tool_block,
                                        "input_string": ""
                                    }
                                    current_text_block = None

                    # Handle text delta events
                    if hasattr(event, "delta"):
                        if hasattr(event.delta, "text"):
                            delta_text = event.delta.text or ""
                            accumulated_content += delta_text
                            if current_text_block is not None:
                                current_text_block["text"] += delta_text

                    # Handle tool input delta events
                    if hasattr(event, "type") and event.type == "content_block_delta":
                        if hasattr(event, "delta") and hasattr(event.delta, "type") and event.delta.type == "input_json_delta":
                            if hasattr(event, "index") and event.index < len(content_blocks):
                                block = content_blocks[event.index]
                                if block.get("type") == "function" and block.get("id") in tools_in_progress:
                                    tool = tools_in_progress[block["id"]]
                                    partial_json = getattr(event.delta, "partial_json", "")
                                    tool["input_string"] += partial_json

                    # Handle content block stop events
                    if hasattr(event, "type") and event.type == "content_block_stop":
                        current_text_block = None
                        # Parse accumulated tool input
                        if hasattr(event, "index") and event.index < len(content_blocks):
                            block = content_blocks[event.index]
                            if block.get("type") == "function" and block.get("id") in tools_in_progress:
                                tool = tools_in_progress[block["id"]]
                                try:
                                    import json
                                    block["function"]["arguments"] = json.loads(tool["input_string"])
                                except (json.JSONDecodeError, Exception):
                                    # Keep empty dict if parsing fails
                                    pass
                                del tools_in_progress[block["id"]]

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
        usage_stats: Dict[str, int],
        latency: float,
        content_blocks: List[Dict[str, Any]],
        accumulated_content: str,
    ):
        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        # Format output to match non-streaming version
        formatted_output = []
        if content_blocks:
            formatted_output = [{
                "role": "assistant",
                "content": content_blocks
            }]
        else:
            # Fallback to accumulated content if no blocks
            formatted_output = [{
                "role": "assistant",
                "content": [{"type": "text", "text": accumulated_content}]
            }]

        event_properties = {
            "$ai_provider": "anthropic",
            "$ai_model": kwargs.get("model"),
            "$ai_model_parameters": get_model_params(kwargs),
            "$ai_input": with_privacy_mode(
                self._client._ph_client,
                posthog_privacy_mode,
                merge_system_prompt(kwargs, "anthropic"),
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
