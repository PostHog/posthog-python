import time
import uuid
from typing import Any, Dict, Optional, Union

try:
    import openai
except ImportError:
    raise ModuleNotFoundError("Please install OpenAI to use this feature: 'pip install openai'")

from posthog.ai.utils import call_llm_and_track_usage_async, get_model_params
from posthog.client import Client as PostHogClient


class AsyncOpenAI:
    """
    An async wrapper around the OpenAI SDK that automatically sends LLM usage events to PostHog.
    """

    def __init__(
        self,
        posthog_client: PostHogClient,
        **openai_config: Any,
    ):
        """
        Args:
            api_key: OpenAI API key.
            posthog_client: If provided, events will be captured via this client instance.
            **openai_config: Additional keyword args (e.g. organization="xxx").
        """
        self._openai_client = openai.AsyncOpenAI(**openai_config)
        self._posthog_client = posthog_client
        self._base_url = openai_config.get("base_url", "https://api.openai.com/v1")

    def __getattr__(self, name: str) -> Any:
        """
        Expose all attributes of the underlying openai.AsyncOpenAI instance except for the 'chat' property,
        which is replaced with a custom AsyncChatNamespace for usage tracking.
        """
        if name == "chat":
            return self.chat
        return getattr(self._openai_client, name)

    @property
    def chat(self) -> "AsyncChatNamespace":
        return AsyncChatNamespace(self._posthog_client, self._openai_client, self._base_url)


class AsyncChatNamespace:
    def __init__(self, posthog_client: Union[PostHogClient, Any], openai_client: Any, base_url: Optional[str]):
        self._ph_client = posthog_client
        self._openai_client = openai_client
        self._base_url = base_url

    @property
    def completions(self):
        return AsyncChatCompletions(self._ph_client, self._openai_client, self._base_url)


class AsyncChatCompletions:
    def __init__(self, posthog_client: Union[PostHogClient, Any], openai_client: Any, base_url: Optional[str]):
        self._ph_client = posthog_client
        self._openai_client = openai_client
        self._base_url = base_url

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
            self._ph_client,
            posthog_trace_id,
            posthog_properties,
            call_async_method,
            self._base_url,
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
        response = await self._openai_client.chat.completions.create(**kwargs, stream_options=stream_options)

        async def async_generator():
            nonlocal usage_stats, accumulated_content
            try:
                async for chunk in response:
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage_stats = {
                            k: getattr(chunk.usage, k, 0)
                            for k in ["prompt_tokens", "completion_tokens", "total_tokens"]
                        }
                    if chunk.choices[0].delta.content:
                        accumulated_content.append(chunk.choices[0].delta.content)
                    yield chunk
            finally:
                end_time = time.time()
                latency = end_time - start_time
                output = "".join(accumulated_content)
                self._capture_streaming_event(
                    distinct_id, posthog_trace_id, posthog_properties, kwargs, usage_stats, latency, output
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
            "$ai_request_url": f"{self._base_url}/chat/completions",
        }

        if hasattr(self._ph_client, "capture"):
            self._ph_client.capture(
                distinct_id=distinct_id,
                event="$ai_generation",
                properties=event_properties,
            )
