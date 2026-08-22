try:
    import anthropic
    from anthropic.resources import Messages
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Anthropic SDK to use this feature: 'pip install anthropic'"
    )

import time
import uuid
from typing import Any, Dict, List, Optional

from ..stream import _StreamWrapper
from ..types import (
    StreamingContentBlock as StreamingContentBlock,
    TokenUsage as TokenUsage,
    ToolInProgress as ToolInProgress,
)
from ..utils import (
    call_llm_and_track_usage as call_llm_and_track_usage,
    merge_usage_stats as merge_usage_stats,
)
from ._anthropic_stream import _AnthropicStreamAccumulator
from .anthropic_converter import (
    extract_anthropic_usage_from_event as extract_anthropic_usage_from_event,
    finalize_anthropic_tool_input as finalize_anthropic_tool_input,
    handle_anthropic_content_block_start as handle_anthropic_content_block_start,
    handle_anthropic_text_delta as handle_anthropic_text_delta,
    handle_anthropic_tool_delta as handle_anthropic_tool_delta,
)
from posthog.client import Client as PostHogClient
from posthog import setup


class Anthropic(anthropic.Anthropic):
    """
    A wrapper around the Anthropic SDK that automatically sends LLM usage events to PostHog.
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
        self.messages = WrappedMessages(self)


class WrappedMessages(Messages):
    _client: Anthropic

    def create(
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
            return self._create_streaming(
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
                **kwargs,
            )

        return call_llm_and_track_usage(
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

    def stream(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """
        Stream an Anthropic message while tracking usage in PostHog.

        Args:
            posthog_distinct_id: Optional distinct ID to associate with the usage event.
            posthog_trace_id: Optional trace ID. Generated automatically when omitted.
            posthog_properties: Additional properties to include with the usage event.
            posthog_privacy_mode: Whether to redact captured input and output.
            posthog_groups: Optional PostHog groups to associate with the event.
            **kwargs: Arguments passed to Anthropic's ``messages.create`` API.

        Returns:
            Anthropic's native streaming context manager.
        """
        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        # Construct the provider resource directly so older Anthropic versions,
        # whose stream manager delegates through ``self.create(stream=True)``,
        # cannot re-enter our tracked ``create`` override.
        manager = Messages(self._client).stream(**kwargs)
        request_attribute = "_MessageStreamManager__api_request"
        request = getattr(manager, request_attribute, None)
        if request is None:
            return manager

        def tracked_request():
            start_time = time.time()
            response = request()
            return self._track_streaming_response(
                response,
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
                kwargs,
                start_time,
            )

        setattr(manager, request_attribute, tracked_request)
        return manager

    def _create_streaming(
        self,
        posthog_distinct_id: Optional[str],
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool,
        posthog_groups: Optional[Dict[str, Any]],
        **kwargs: Any,
    ):
        start_time = time.time()
        response = super().create(**kwargs)
        return self._track_streaming_response(
            response,
            posthog_distinct_id,
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            kwargs,
            start_time,
        )

    def _track_streaming_response(
        self,
        response: Any,
        posthog_distinct_id: Optional[str],
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool,
        posthog_groups: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
        start_time: float,
    ):
        accumulator = _AnthropicStreamAccumulator()

        def generator():
            try:
                for event in response:
                    accumulator.consume(event)
                    yield event
            finally:
                end_time = time.time()
                latency = end_time - start_time

                self._capture_streaming_event(
                    posthog_distinct_id,
                    posthog_trace_id,
                    posthog_properties,
                    posthog_privacy_mode,
                    posthog_groups,
                    kwargs,
                    accumulator.usage_stats,
                    latency,
                    accumulator.content_blocks,
                    accumulator.accumulated_content,
                    stop_reason=accumulator.stop_reason,
                )

        return _StreamWrapper(generator(), stream=response)

    def _capture_streaming_event(
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
        stop_reason: Optional[str] = None,
    ):
        from posthog.ai.types import StreamingEventData
        from posthog.ai.anthropic.anthropic_converter import (
            format_anthropic_streaming_input,
            format_anthropic_streaming_output_complete,
        )
        from posthog.ai.utils import capture_streaming_event

        formatted_input = format_anthropic_streaming_input(kwargs)

        event_data = StreamingEventData(
            provider="anthropic",
            model=kwargs.get("model", "unknown"),
            base_url=str(self._client.base_url),
            kwargs=kwargs,
            formatted_input=formatted_input,
            formatted_output=format_anthropic_streaming_output_complete(
                content_blocks, accumulated_content
            ),
            usage_stats=usage_stats,
            latency=latency,
            distinct_id=posthog_distinct_id,
            trace_id=posthog_trace_id,
            properties=posthog_properties,
            privacy_mode=posthog_privacy_mode,
            groups=posthog_groups,
            stop_reason=stop_reason,
        )

        # Use the common capture function
        capture_streaming_event(self._client._ph_client, event_data)
