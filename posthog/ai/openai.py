import time
from typing import Any, Dict, Optional, Union

import openai
from posthog.client import Client as PostHogClient


def get_model_params(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts model parameters from the kwargs dictionary.
    """
    model_params = {}
    for param in ["temperature", "max_tokens", "top_p", "frequency_penalty", 
                "presence_penalty", "n", "stop", "stream"]:
        if param in kwargs:
            model_params[param] = kwargs.get(param)
    return model_params

def get_output(response: openai.types.chat.ChatCompletion) -> Dict[str, Any]:
    output = {
        "choices": []
    }
    for choice in response.choices:
        if choice.message.content:
            output["choices"].append({
                "content": choice.message.content,
                "role": choice.message.role,
            })
    return output


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
            posthog_client: If provided, events will be captured via this client instance instead 
                of the global posthog module.
            **openai_config: Any additional keyword args to set on openai (e.g. organization="xxx").
        """
        # Initialize OpenAI client instead of setting global config
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
        Wraps open ai chat completions and captures a $ai_generation event in PostHog.

        PostHog-specific parameters:
            posthog_distinct_id: Ties the resulting event to a user in PostHog.
            posthog_trace_id: For grouping multiple calls into a single trace.
            posthog_properties: Additional custom properties to include on the PostHog event.
        """
        start_time = time.time()
        response = None
        error = None
        http_status = 200
        usage: Dict[str, Any] = {}

        try:
            response = self._openai_client.chat.completions.create(**kwargs)
        except Exception as exc:
            error = exc
            http_status = getattr(exc, 'status_code', 500)
        finally:
            end_time = time.time()
            latency = end_time - start_time

            # Update usage extraction for new response format
            if response and hasattr(response, "usage"):
                usage = response.usage.model_dump()

            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            # Build PostHog event properties
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
            }

            # If not streaming and no error, try storing some output detail
            # TODO: we need to support streaming responses
            stream = kwargs.get("stream", False)
            if response and not error and not stream:
                event_properties["$ai_output"] = get_output(response)

            # Merge in any custom PostHog properties
            if posthog_properties:
                event_properties.update(posthog_properties)

            # Capture event in PostHog
            if hasattr(self._ph_client, "capture") and callable(self._ph_client.capture):
                distinct_id = posthog_distinct_id or "anonymous_ai_user"
                self._ph_client.capture(
                    distinct_id=distinct_id,
                    event="$ai_generation",
                    properties=event_properties,
                )

        if error:
            raise error

        return response
