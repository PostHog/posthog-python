import time
from typing import Any, Dict, Optional, Union, AsyncGenerator

try:
    import openai
except ImportError:
    raise ModuleNotFoundError("Please install OpenAI to use this feature: 'pip install openai'")

from posthog.client import Client as PostHogClient
from posthog.ai.utils import get_model_params, format_response, process_async_streaming_response, track_usage_async


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

    @property
    def chat(self) -> "AsyncChatNamespace":
        return AsyncChatNamespace(self._posthog_client, self._openai_client)


class AsyncChatNamespace:
    def __init__(self, posthog_client: Union[PostHogClient, Any], openai_client: Any):
        self._ph_client = posthog_client
        self._openai_client = openai_client

    @property
    def completions(self):
        return AsyncChatCompletions(self._ph_client, self._openai_client)


class AsyncChatCompletions:
    def __init__(self, posthog_client: Union[PostHogClient, Any], openai_client: Any):
        self._ph_client = posthog_client
        self._openai_client = openai_client

    async def create(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """
        Wraps openai chat completions (async) and captures a $ai_generation event in PostHog.

        To use streaming in async mode:
            async for chunk in async_openai.chat.completions.create(stream=True, ...):
                ...
        """
        distinct_id = posthog_distinct_id or "anonymous_ai_user"

        # If streaming, handle streaming specifically
        if kwargs.get("stream", False):
            response = await self._openai_client.chat.completions.create(**kwargs)
            return process_async_streaming_response(
                response=response,
                ph_client=self._ph_client,
                event_properties={},
                distinct_id=distinct_id,
            )

        # Non-streaming: let track_usage_async handle request and analytics
        async def call_async_method(**call_kwargs):
            return await self._openai_client.chat.completions.create(**call_kwargs)

        response = await track_usage_async(
            distinct_id, self._ph_client, posthog_trace_id, posthog_properties, call_async_method, **kwargs
        )
        return response
