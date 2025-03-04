import time
import uuid
from typing import Any, Dict, Optional

try:
    import openai
    import openai.resources
except ImportError:
    raise ModuleNotFoundError("Please install the OpenAI SDK to use this feature: 'pip install openai'")

from posthog.ai.utils import call_llm_and_track_usage_async, get_model_params, with_privacy_mode
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
        self._ph_client = posthog_client
        self.chat = WrappedChat(self)
        self.embeddings = WrappedEmbeddings(self)
        self.beta = WrappedBeta(self)


class WrappedChat(openai.resources.chat.AsyncChat):
    _client: AsyncOpenAI

    @property
    def completions(self):
        return WrappedCompletions(self._client)


class WrappedCompletions(openai.resources.chat.completions.AsyncCompletions):
    _client: AsyncOpenAI

    async def create(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        if posthog_trace_id is None:
            posthog_trace_id = uuid.uuid4()

        # If streaming, handle streaming specifically
        if kwargs.get("stream", False):
            return await self._create_streaming(
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
                **kwargs,
            )

        response = await call_llm_and_track_usage_async(
            posthog_distinct_id,
            self._client._ph_client,
            "openai",
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            self._client.base_url,
            super().create,
            **kwargs,
        )
        return response

    async def _create_streaming(
        self,
        posthog_distinct_id: Optional[str],
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        start_time = time.time()
        usage_stats: Dict[str, int] = {}
        accumulated_content = []
        accumulated_tools = {}
        if "stream_options" not in kwargs:
            kwargs["stream_options"] = {}
        kwargs["stream_options"]["include_usage"] = True
        response = await super().create(**kwargs)

        async def async_generator():
            nonlocal usage_stats, accumulated_content, accumulated_tools
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

                        # Add support for cached tokens
                        if hasattr(chunk.usage, "prompt_tokens_details") and hasattr(
                            chunk.usage.prompt_tokens_details, "cached_tokens"
                        ):
                            usage_stats["cache_read_input_tokens"] = chunk.usage.prompt_tokens_details.cached_tokens

                    if hasattr(chunk, "choices") and chunk.choices and len(chunk.choices) > 0:
                        content = chunk.choices[0].delta.content
                        if content:
                            accumulated_content.append(content)

                        # Process tool calls
                        tool_calls = getattr(chunk.choices[0].delta, "tool_calls", None)
                        if tool_calls:
                            for tool_call in tool_calls:
                                index = tool_call.index
                                if index not in accumulated_tools:
                                    accumulated_tools[index] = tool_call
                                else:
                                    # Append arguments for existing tool calls
                                    if hasattr(tool_call, "function") and hasattr(tool_call.function, "arguments"):
                                        accumulated_tools[index].function.arguments += tool_call.function.arguments

                    yield chunk

            finally:
                end_time = time.time()
                latency = end_time - start_time
                output = "".join(accumulated_content)
                tools = list(accumulated_tools.values()) if accumulated_tools else None
                await self._capture_streaming_event(
                    posthog_distinct_id,
                    posthog_trace_id,
                    posthog_properties,
                    posthog_privacy_mode,
                    posthog_groups,
                    kwargs,
                    usage_stats,
                    latency,
                    output,
                    tools,
                )

        return async_generator()

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
        output: str,
        tool_calls=None,
    ):
        if posthog_trace_id is None:
            posthog_trace_id = uuid.uuid4()

        event_properties = {
            "$ai_provider": "openai",
            "$ai_model": kwargs.get("model"),
            "$ai_model_parameters": get_model_params(kwargs),
            "$ai_input": with_privacy_mode(self._client._ph_client, posthog_privacy_mode, kwargs.get("messages")),
            "$ai_output_choices": with_privacy_mode(
                self._client._ph_client,
                posthog_privacy_mode,
                [{"content": output, "role": "assistant"}],
            ),
            "$ai_http_status": 200,
            "$ai_input_tokens": usage_stats.get("prompt_tokens", 0),
            "$ai_output_tokens": usage_stats.get("completion_tokens", 0),
            "$ai_cache_read_input_tokens": usage_stats.get("cache_read_input_tokens", 0),
            "$ai_latency": latency,
            "$ai_trace_id": posthog_trace_id,
            "$ai_base_url": str(self._client.base_url),
            **(posthog_properties or {}),
        }

        if tool_calls:
            event_properties["$ai_tools"] = with_privacy_mode(
                self._client._ph_client,
                posthog_privacy_mode,
                tool_calls,
            )

        if posthog_distinct_id is None:
            event_properties["$process_person_profile"] = False

        if hasattr(self._client._ph_client, "capture"):
            self._client._ph_client.capture(
                distinct_id=posthog_distinct_id or posthog_trace_id,
                event="$ai_generation",
                properties=event_properties,
                groups=posthog_groups,
            )


class WrappedEmbeddings(openai.resources.embeddings.AsyncEmbeddings):
    _client: AsyncOpenAI

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
        Create an embedding using OpenAI's 'embeddings.create' method, but also track usage in PostHog.

        Args:
            posthog_distinct_id: Optional ID to associate with the usage event.
            posthog_trace_id: Optional trace UUID for linking events.
            posthog_properties: Optional dictionary of extra properties to include in the event.
            posthog_privacy_mode: Whether to store input and output in PostHog.
            posthog_groups: Optional dictionary of groups to include in the event.
            **kwargs: Any additional parameters for the OpenAI Embeddings API.

        Returns:
            The response from OpenAI's embeddings.create call.
        """
        if posthog_trace_id is None:
            posthog_trace_id = uuid.uuid4()

        start_time = time.time()
        response = await super().create(**kwargs)
        end_time = time.time()

        # Extract usage statistics if available
        usage_stats = {}
        if hasattr(response, "usage") and response.usage:
            usage_stats = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            }

        latency = end_time - start_time

        # Build the event properties
        event_properties = {
            "$ai_provider": "openai",
            "$ai_model": kwargs.get("model"),
            "$ai_input": with_privacy_mode(self._client._ph_client, posthog_privacy_mode, kwargs.get("input")),
            "$ai_http_status": 200,
            "$ai_input_tokens": usage_stats.get("prompt_tokens", 0),
            "$ai_latency": latency,
            "$ai_trace_id": posthog_trace_id,
            "$ai_base_url": str(self._client.base_url),
            **(posthog_properties or {}),
        }

        if posthog_distinct_id is None:
            event_properties["$process_person_profile"] = False

        # Send capture event for embeddings
        if hasattr(self._client._ph_client, "capture"):
            self._client._ph_client.capture(
                distinct_id=posthog_distinct_id or posthog_trace_id,
                event="$ai_embedding",
                properties=event_properties,
                groups=posthog_groups,
            )

        return response


class WrappedBeta(openai.resources.beta.AsyncBeta):
    _client: AsyncOpenAI

    @property
    def chat(self):
        return WrappedBetaChat(self._client)


class WrappedBetaChat(openai.resources.beta.chat.AsyncChat):
    _client: AsyncOpenAI

    @property
    def completions(self):
        return WrappedBetaCompletions(self._client)


class WrappedBetaCompletions(openai.resources.beta.chat.completions.AsyncCompletions):
    _client: AsyncOpenAI

    async def parse(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        return await call_llm_and_track_usage_async(
            posthog_distinct_id,
            self._client._ph_client,
            "openai",
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            self._client.base_url,
            super().parse,
            **kwargs,
        )
