import time
from typing import Any, Dict, Optional, Union, AsyncGenerator

try:
    import openai
except ImportError:
    raise ModuleNotFoundError("Please install OpenAI to use this feature: 'pip install openai'")

from posthog.client import Client as PostHogClient
from posthog.ai.utils import get_model_params, format_response, process_sync_streaming_response, track_usage


class OpenAI:
    """
    A wrapper around the OpenAI SDK that automatically sends LLM usage events to PostHog.
    """

    def __init__(
        self,
        posthog_client: PostHogClient,
        **openai_config: Any,
    ):
        """
        Args:
            api_key: OpenAI API key.
            posthog_client: If provided, events will be captured via this client instead
                            of the global posthog.
            **openai_config: Any additional keyword args to set on openai (e.g. organization="xxx").
        """
        self._openai_client = openai.OpenAI(**openai_config)
        self._posthog_client = posthog_client

    @property
    def chat(self) -> "ChatNamespace":
        return ChatNamespace(self._posthog_client, self._openai_client)


class ChatNamespace:
    def __init__(self, posthog_client: Union[PostHogClient, Any], openai_client: Any):
        self._ph_client = posthog_client
        self._openai_client = openai_client

    @property
    def completions(self):
        return ChatCompletions(self._ph_client, self._openai_client)


class ChatCompletions:
    def __init__(self, posthog_client: Union[PostHogClient, Any], openai_client: Any):
        self._ph_client = posthog_client
        self._openai_client = openai_client

    def create(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """
        Wraps openai chat completions and captures a $ai_generation event in PostHog.

        PostHog-specific parameters:
            - posthog_distinct_id: Ties the resulting event to a user in PostHog.
            - posthog_trace_id: For grouping multiple calls into a single trace.
            - posthog_properties: Additional custom properties for PostHog analytics.
        """
        distinct_id = posthog_distinct_id or "anonymous_ai_user"

        # If streaming, handle it separately
        if kwargs.get("stream", False):
            response = self._openai_client.chat.completions.create(**kwargs)
            return process_sync_streaming_response(
                response=response,
                ph_client=self._ph_client,
                event_properties={},
                distinct_id=distinct_id,
            )

        # Non-streaming: let track_usage handle the request and analytics
        def call_method(**call_kwargs):
            return self._openai_client.chat.completions.create(**call_kwargs)

        response = track_usage(
            distinct_id, self._ph_client, posthog_trace_id, posthog_properties, call_method, **kwargs
        )
        return response
