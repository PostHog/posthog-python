import time
import uuid
from typing import Any, Dict, Optional

try:
    import openai
except ImportError:
    raise ModuleNotFoundError(
        "Please install the OpenAI SDK to use this feature: 'pip install openai'"
    )

from posthog.ai.utils import call_llm_and_track_usage_async, get_model_params
from posthog.client import Client as PostHogClient


class AsyncOpenAI(openai.AsyncOpenAI):
    """
    An async wrapper around the OpenAI SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    def __init__(self, posthog_client: PostHogClient, **kwargs):
        """
        Args:
            api_key: OpenAI API key.
            posthog_client: If provided, events will be captured via this client instance.
            **openai_config: Additional keyword args (e.g. organization="xxx").
        """
        super().__init__(**kwargs)
        super().chat
        self._ph_client = posthog_client

    @property
    def chat(self) -> "AsyncChatNamespace":
        """OpenAI `chat` wrapped with PostHog usage tracking."""
        return AsyncChatNamespace(self)


class AsyncChatNamespace:
    _openai_client: AsyncOpenAI

    def __init__(self, openai_client: AsyncOpenAI):
        self._openai_client = openai_client

    @property
    def completions(self):
        return AsyncChatCompletions(self._openai_client)


class AsyncChatCompletions:
    _openai_client: AsyncOpenAI

    def __init__(self, openai_client: AsyncOpenAI):
        self._openai_client = openai_client

    async def create(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        distinct_id = posthog_distinct_id or uuid.uuid4()

        # If streaming, handle streaming specifically
        if kwargs.get("stream", False):
            return await self._create_streaming(
                distinct_id,
                posthog_trace_id,
                posthog_properties,
                **kwargs,
            )

        # Non-streaming: let track_usage_async handle request and analytics
        async def call_async_method(**call_kwargs):
            return await self._openai_client.chat.completions.create(**call_kwargs)

        response = await call_llm_and_track_usage_async(
            distinct_id,
            self._openai_client._ph_client,
            posthog_trace_id,
            posthog_properties,
            call_async_method,
            self._openai_client.base_url,
            **kwargs,
        )
        return response

    async def _create_streaming(
        self,
        distinct_id: str,
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        **kwargs: Any,
    ):
        start_time = time.time()
        usage_stats: Dict[str, int] = {}
        accumulated_content = []
        stream_options = {"include_usage": True}
        response = await self._openai_client.chat.completions.create(
            **kwargs, stream_options=stream_options
        )

        async def async_generator():
            nonlocal usage_stats, accumulated_content
            try:
                async for chunk in response:
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage_stats = {
                            k: getattr(chunk.usage, k, 0)
                            for k in [
                                "prompt_tokens",
                                "completion_tokens",
                                "total_tokens",
                            ]
                        }
                    if chunk.choices[0].delta.content:
                        accumulated_content.append(chunk.choices[0].delta.content)
                    yield chunk
            finally:
                end_time = time.time()
                latency = end_time - start_time
                output = "".join(accumulated_content)
                self._capture_streaming_event(
                    distinct_id,
                    posthog_trace_id,
                    posthog_properties,
                    kwargs,
                    usage_stats,
                    latency,
                    output,
                )

        return async_generator()

    def _capture_streaming_event(
        self,
        distinct_id: str,
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
        usage_stats: Dict[str, int],
        latency: float,
        output: str,
    ):
        if posthog_trace_id is None:
            posthog_trace_id = uuid.uuid4()

        event_properties = {
            "$ai_provider": "openai",
            "$ai_model": kwargs.get("model"),
            "$ai_model_parameters": get_model_params(kwargs),
            "$ai_input": kwargs.get("messages"),
            "$ai_output": {
                "choices": [
                    {
                        "content": output,
                        "role": "assistant",
                    }
                ]
            },
            "$ai_http_status": 200,
            "$ai_input_tokens": usage_stats.get("prompt_tokens", 0),
            "$ai_output_tokens": usage_stats.get("completion_tokens", 0),
            "$ai_latency": latency,
            "$ai_trace_id": posthog_trace_id,
            "$ai_posthog_properties": posthog_properties,
            "$ai_request_url": str(self._openai_client.base_url.join("chat/completions")),
        }

        if hasattr(self._openai_client._ph_client, "capture"):
            self._openai_client._ph_client.capture(
                distinct_id=distinct_id,
                event="$ai_generation",
                properties=event_properties,
            )
