import time
import uuid
from typing import Any, Dict, Optional

import openai.resources

try:
    import openai
except ImportError:
    raise ModuleNotFoundError("Please install the OpenAI SDK to use this feature: 'pip install openai'")

from posthog.ai.utils import call_llm_and_track_usage, get_model_params
from posthog.client import Client as PostHogClient


class OpenAI(openai.OpenAI):
    """
    A wrapper around the OpenAI SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    def __init__(self, posthog_client: PostHogClient, **kwargs):
        """
        Args:
            api_key: OpenAI API key.
            posthog_client: If provided, events will be captured via this client instead
                            of the global posthog.
            **openai_config: Any additional keyword args to set on openai (e.g. organization="xxx").
        """
        super().__init__(**kwargs)
        self._ph_client = posthog_client
        self.chat = WrappedChat(self)


class WrappedChat(openai.resources.chat.Chat):
    _client: OpenAI

    @property
    def completions(self):
        return WrappedCompletions(self._client)


class WrappedCompletions(openai.resources.chat.completions.Completions):
    _client: OpenAI

    def create(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        distinct_id = posthog_distinct_id or uuid.uuid4()

        if kwargs.get("stream", False):
            return self._create_streaming(
                distinct_id,
                posthog_trace_id,
                posthog_properties,
                **kwargs,
            )

        return call_llm_and_track_usage(
            distinct_id,
            self._client._ph_client,
            posthog_trace_id,
            posthog_properties,
            self._client.base_url,
            super().create,
            **kwargs,
        )

    def _create_streaming(
        self,
        distinct_id: str,
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        **kwargs: Any,
    ):
        start_time = time.time()
        usage_stats: Dict[str, int] = {}
        accumulated_content = []
        if "stream_options" not in kwargs:
            kwargs["stream_options"] = {}
        kwargs["stream_options"]["include_usage"] = True
        response = super().create(**kwargs)

        def generator():
            nonlocal usage_stats
            nonlocal accumulated_content
            try:
                for chunk in response:
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage_stats = {
                            k: getattr(chunk.usage, k, 0)
                            for k in [
                                "prompt_tokens",
                                "completion_tokens",
                                "total_tokens",
                            ]
                        }

                    if hasattr(chunk, "choices") and chunk.choices and len(chunk.choices) > 0:
                        content = chunk.choices[0].delta.content
                        if content:
                            accumulated_content.append(content)

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

        return generator()

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
            "$ai_request_url": str(self._client.base_url.join("chat/completions")),
            "$ai_http_status": 200,
            "$ai_input_tokens": usage_stats.get("prompt_tokens", 0),
            "$ai_output_tokens": usage_stats.get("completion_tokens", 0),
            "$ai_latency": latency,
            "$ai_trace_id": posthog_trace_id,
            "$ai_posthog_properties": posthog_properties,
        }

        if hasattr(self._client._ph_client, "capture"):
            self._client._ph_client.capture(
                distinct_id=distinct_id,
                event="$ai_generation",
                properties=event_properties,
            )
