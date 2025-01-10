import time
from typing import Any, Dict, Optional, Union, AsyncGenerator

try:
    import openai
except ImportError:
    raise ModuleNotFoundError(
        "Please install OpenAI to use this feature: 'pip install openai'"
    )

from posthog.client import Client as PostHogClient


def get_model_params(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts model parameters from the kwargs dictionary.
    """
    model_params = {}
    for param in [
        "temperature",
        "max_tokens",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "n",
        "stop",
        "stream",
    ]:
        if param in kwargs:
            model_params[param] = kwargs.get(param)
    return model_params


def format_response(response):
    """
    Format a regular (non-streaming) response.
    """
    output = {"choices": []}
    for choice in response.choices:
        if choice.message.content:
            output["choices"].append(
                {
                    "content": choice.message.content,
                    "role": choice.message.role,
                }
            )
    return output


# ---------------------------
# Streaming Response Handlers
# ---------------------------

def process_sync_streaming_response(
    response,
    ph_client,
    event_properties,
    distinct_id,
):
    """
    Processes chunks from a synchronous streaming response, accumulating them
    so we can capture them in analytics afterward.
    """
    accumulated_content = []

    try:
        for chunk in response:
            if chunk.choices[0].delta.content:
                accumulated_content.append(chunk.choices[0].delta.content)
            yield chunk
    finally:
        # Once we've finished, capture the final content in PostHog
        final_content = "".join(accumulated_content)
        event_properties["$ai_output"] = {
            "choices": [
                {
                    "content": final_content,
                    "role": "assistant",
                }
            ]
        }
        if hasattr(ph_client, "capture"):
            ph_client.capture(
                distinct_id=distinct_id,
                event="$ai_generation",
                properties=event_properties,
            )


async def process_async_streaming_response(
    response,
    ph_client,
    event_properties,
    distinct_id,
) -> AsyncGenerator[Any, None]:
    """
    Processes chunks from an asynchronous streaming response, accumulating them
    so we can capture them in analytics afterward.
    """
    accumulated_content = []
    try:
        async for chunk in response:
            if chunk.choices[0].delta.content:
                accumulated_content.append(chunk.choices[0].delta.content)
            yield chunk
    finally:
        final_content = "".join(accumulated_content)
        event_properties["$ai_output"] = {
            "choices": [
                {
                    "content": final_content,
                    "role": "assistant",
                }
            ]
        }
        if hasattr(ph_client, "capture"):
            ph_client.capture(
                distinct_id=distinct_id,
                event="$ai_generation",
                properties=event_properties,
            )

# -----------------------------------------
# Synchronous OpenAI Wrapper (Blocking)
# -----------------------------------------
class OpenAI:
    """
    A blocking, synchronous wrapper around the OpenAI SDK that automatically
    sends LLM usage events to PostHog.
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
        # The standard OpenAI client for synchronous usage
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
        Wraps openai chat completions (sync) and captures a $ai_generation event in PostHog.

        PostHog-specific parameters:
            - posthog_distinct_id: Ties the resulting event to a user in PostHog.
            - posthog_trace_id: For grouping multiple calls into a single trace.
            - posthog_properties: Additional custom properties for PostHog analytics.
        """
        start_time = time.time()
        response = None
        error = None
        http_status = 200
        usage: Dict[str, Any] = {}

        try:
            # Actual call (sync)
            response = self._openai_client.chat.completions.create(**kwargs)
        except Exception as exc:
            error = exc
            http_status = getattr(exc, "status_code", 500)
        finally:
            end_time = time.time()
            latency = end_time - start_time

            # Extract usage if available
            if response and hasattr(response, "usage"):
                usage = response.usage.model_dump()

            # Prepare analytics data
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            event_properties = {
                "$ai_provider": "openai",
                "$ai_model": kwargs.get("model"),
                "$ai_model_parameters": get_model_params(kwargs),
                "$ai_input": kwargs.get("messages"),
                "$ai_output": None,
                "$ai_http_status": http_status,
                "$ai_input_tokens": input_tokens,
                "$ai_output_tokens": output_tokens,
                "$ai_latency": latency,
                "$ai_trace_id": posthog_trace_id,
                "$ai_posthog_properties": posthog_properties,
            }

            distinct_id = posthog_distinct_id or "anonymous_ai_user"

            # If streaming, yield from a sync generator
            if kwargs.get("stream", False):
                return process_sync_streaming_response(
                    response=response,
                    ph_client=self._ph_client,
                    event_properties=event_properties,
                    distinct_id=distinct_id,
                )

            # Non-streaming
            event_properties["$ai_output"] = format_response(response)
            if hasattr(self._ph_client, "capture") and callable(self._ph_client.capture):
                self._ph_client.capture(
                    distinct_id=distinct_id,
                    event="$ai_generation",
                    properties=event_properties,
                )

        if error:
            raise error

        return response


# -----------------------------------------
# Asynchronous OpenAI Wrapper (Async/Await)
# -----------------------------------------

class AsyncOpenAI:
    """
    An async version of the OpenAI wrapper that uses openai.AsyncOpenAI.
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
        # The async OpenAI client for async usage
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
        start_time = time.time()
        response = None
        error = None
        http_status = 200
        usage: Dict[str, Any] = {}

        try:
            response = await self._openai_client.chat.completions.create(**kwargs)
        except Exception as exc:
            error = exc
            http_status = getattr(exc, "status_code", 500)
        finally:
            end_time = time.time()
            latency = end_time - start_time

            # Extract usage if available
            if response and hasattr(response, "usage"):
                usage = response.usage.model_dump()

            # Prepare analytics data
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            event_properties = {
                "$ai_provider": "openai",
                "$ai_model": kwargs.get("model"),
                "$ai_model_parameters": get_model_params(kwargs),
                "$ai_input": kwargs.get("messages"),
                "$ai_output": None,
                "$ai_http_status": http_status,
                "$ai_input_tokens": input_tokens,
                "$ai_output_tokens": output_tokens,
                "$ai_latency": latency,
                "$ai_trace_id": posthog_trace_id,
                "$ai_posthog_properties": posthog_properties,
            }

            distinct_id = posthog_distinct_id or "anonymous_ai_user"

            # If streaming in async, return an async generator
            if kwargs.get("stream", False):
                return process_async_streaming_response(
                    response=response,
                    ph_client=self._ph_client,
                    event_properties=event_properties,
                    distinct_id=distinct_id,
                )

            # Non-streaming
            event_properties["$ai_output"] = format_response(response)
            if hasattr(self._ph_client, "capture") and callable(self._ph_client.capture):
                self._ph_client.capture(
                    distinct_id=distinct_id,
                    event="$ai_generation",
                    properties=event_properties,
                )

        if error:
            raise error

        return response