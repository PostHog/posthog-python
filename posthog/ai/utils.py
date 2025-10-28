import time
import uuid
from typing import Any, Callable, Dict, List, Optional, cast

from posthog.client import Client as PostHogClient
from posthog.ai.types import FormattedMessage, StreamingEventData, TokenUsage
from posthog.ai.sanitization import (
    sanitize_openai,
    sanitize_anthropic,
    sanitize_gemini,
    sanitize_langchain,
)


def merge_usage_stats(
    target: TokenUsage, source: TokenUsage, mode: str = "incremental"
) -> None:
    """
    Merge streaming usage statistics into target dict, handling None values.

    Supports two modes:
    - "incremental": Add source values to target (for APIs that report new tokens)
    - "cumulative": Replace target with source values (for APIs that report totals)

    Args:
        target: Dictionary to update with usage stats
        source: TokenUsage that may contain None values
        mode: Either "incremental" or "cumulative"
    """
    if mode == "incremental":
        # Add new values to existing totals
        source_input = source.get("input_tokens")
        if source_input is not None:
            current = target.get("input_tokens") or 0
            target["input_tokens"] = current + source_input

        source_output = source.get("output_tokens")
        if source_output is not None:
            current = target.get("output_tokens") or 0
            target["output_tokens"] = current + source_output

        source_cache_read = source.get("cache_read_input_tokens")
        if source_cache_read is not None:
            current = target.get("cache_read_input_tokens") or 0
            target["cache_read_input_tokens"] = current + source_cache_read

        source_cache_creation = source.get("cache_creation_input_tokens")
        if source_cache_creation is not None:
            current = target.get("cache_creation_input_tokens") or 0
            target["cache_creation_input_tokens"] = current + source_cache_creation

        source_reasoning = source.get("reasoning_tokens")
        if source_reasoning is not None:
            current = target.get("reasoning_tokens") or 0
            target["reasoning_tokens"] = current + source_reasoning
    elif mode == "cumulative":
        # Replace with latest values (already cumulative)
        if source.get("input_tokens") is not None:
            target["input_tokens"] = source["input_tokens"]
        if source.get("output_tokens") is not None:
            target["output_tokens"] = source["output_tokens"]
        if source.get("cache_read_input_tokens") is not None:
            target["cache_read_input_tokens"] = source["cache_read_input_tokens"]
        if source.get("cache_creation_input_tokens") is not None:
            target["cache_creation_input_tokens"] = source[
                "cache_creation_input_tokens"
            ]
        if source.get("reasoning_tokens") is not None:
            target["reasoning_tokens"] = source["reasoning_tokens"]
    else:
        raise ValueError(f"Invalid mode: {mode}. Must be 'incremental' or 'cumulative'")


def get_model_params(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts model parameters from the kwargs dictionary.
    """
    model_params = {}
    for param in [
        "temperature",
        "max_tokens",  # Deprecated field
        "max_completion_tokens",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "n",
        "stop",
        "stream",  # OpenAI-specific field
        "streaming",  # Anthropic-specific field
    ]:
        if param in kwargs and kwargs[param] is not None:
            model_params[param] = kwargs[param]
    return model_params


def get_usage(response, provider: str) -> TokenUsage:
    """
    Extract usage statistics from response based on provider.
    Delegates to provider-specific converter functions.
    """
    if provider == "anthropic":
        from posthog.ai.anthropic.anthropic_converter import (
            extract_anthropic_usage_from_response,
        )

        return extract_anthropic_usage_from_response(response)
    elif provider == "openai":
        from posthog.ai.openai.openai_converter import (
            extract_openai_usage_from_response,
        )

        return extract_openai_usage_from_response(response)
    elif provider == "gemini":
        from posthog.ai.gemini.gemini_converter import (
            extract_gemini_usage_from_response,
        )

        return extract_gemini_usage_from_response(response)

    return TokenUsage(input_tokens=0, output_tokens=0)


def format_response(response, provider: str):
    """
    Format a regular (non-streaming) response.
    """
    if provider == "anthropic":
        from posthog.ai.anthropic.anthropic_converter import format_anthropic_response

        return format_anthropic_response(response)
    elif provider == "openai":
        from posthog.ai.openai.openai_converter import format_openai_response

        return format_openai_response(response)
    elif provider == "gemini":
        from posthog.ai.gemini.gemini_converter import format_gemini_response

        return format_gemini_response(response)
    return []


def extract_available_tool_calls(provider: str, kwargs: Dict[str, Any]):
    """
    Extract available tool calls for the given provider.
    """
    if provider == "anthropic":
        from posthog.ai.anthropic.anthropic_converter import extract_anthropic_tools

        return extract_anthropic_tools(kwargs)
    elif provider == "gemini":
        from posthog.ai.gemini.gemini_converter import extract_gemini_tools

        return extract_gemini_tools(kwargs)
    elif provider == "openai":
        from posthog.ai.openai.openai_converter import extract_openai_tools

        return extract_openai_tools(kwargs)
    return None


def merge_system_prompt(
    kwargs: Dict[str, Any], provider: str
) -> List[FormattedMessage]:
    """
    Merge system prompts and format messages for the given provider.
    """
    if provider == "anthropic":
        from posthog.ai.anthropic.anthropic_converter import format_anthropic_input

        messages = kwargs.get("messages") or []
        system = kwargs.get("system")
        return format_anthropic_input(messages, system)
    elif provider == "gemini":
        from posthog.ai.gemini.gemini_converter import format_gemini_input_with_system

        contents = kwargs.get("contents", [])
        config = kwargs.get("config")
        return format_gemini_input_with_system(contents, config)
    elif provider == "openai":
        from posthog.ai.openai.openai_converter import format_openai_input

        # For OpenAI, handle both Chat Completions and Responses API
        messages_param = kwargs.get("messages")
        input_param = kwargs.get("input")

        # Get base formatted messages
        messages = format_openai_input(messages_param, input_param)

        # Check if system prompt is provided as a separate parameter
        if kwargs.get("system") is not None:
            has_system = any(msg.get("role") == "system" for msg in messages)
            if not has_system:
                system_msg = cast(
                    FormattedMessage,
                    {"role": "system", "content": kwargs.get("system")},
                )
                messages = [system_msg] + messages

        # For Responses API, add instructions to the system prompt if provided
        if kwargs.get("instructions") is not None:
            # Find the system message if it exists
            system_idx = next(
                (i for i, msg in enumerate(messages) if msg.get("role") == "system"),
                None,
            )

            if system_idx is not None:
                # Append instructions to existing system message
                system_content = messages[system_idx].get("content", "")
                messages[system_idx]["content"] = (
                    f"{system_content}\n\n{kwargs.get('instructions')}"
                )
            else:
                # Create a new system message with instructions
                instruction_msg = cast(
                    FormattedMessage,
                    {"role": "system", "content": kwargs.get("instructions")},
                )
                messages = [instruction_msg] + messages

        return messages

    # Default case - return empty list
    return []


def call_llm_and_track_usage(
    posthog_distinct_id: Optional[str],
    ph_client: PostHogClient,
    provider: str,
    posthog_trace_id: Optional[str],
    posthog_properties: Optional[Dict[str, Any]],
    posthog_privacy_mode: bool,
    posthog_groups: Optional[Dict[str, Any]],
    base_url: str,
    call_method: Callable[..., Any],
    **kwargs: Any,
) -> Any:
    """
    Common usage-tracking logic for both sync and async calls.
    call_method: the llm call method (e.g. openai.chat.completions.create)
    """
    start_time = time.time()
    response = None
    error = None
    http_status = 200
    usage: TokenUsage = TokenUsage()
    error_params: Dict[str, Any] = {}

    try:
        response = call_method(**kwargs)
    except Exception as exc:
        error = exc
        http_status = getattr(
            exc, "status_code", 0
        )  # default to 0 becuase its likely an SDK error
        error_params = {
            "$ai_is_error": True,
            "$ai_error": exc.__str__(),
        }
    finally:
        end_time = time.time()
        latency = end_time - start_time

        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        if response and (
            hasattr(response, "usage")
            or (provider == "gemini" and hasattr(response, "usage_metadata"))
        ):
            usage = get_usage(response, provider)

        messages = merge_system_prompt(kwargs, provider)
        sanitized_messages = sanitize_messages(messages, provider)

        event_properties = {
            "$ai_provider": provider,
            "$ai_model": kwargs.get("model"),
            "$ai_model_parameters": get_model_params(kwargs),
            "$ai_input": with_privacy_mode(
                ph_client, posthog_privacy_mode, sanitized_messages
            ),
            "$ai_output_choices": with_privacy_mode(
                ph_client, posthog_privacy_mode, format_response(response, provider)
            ),
            "$ai_http_status": http_status,
            "$ai_input_tokens": usage.get("input_tokens", 0),
            "$ai_output_tokens": usage.get("output_tokens", 0),
            "$ai_latency": latency,
            "$ai_trace_id": posthog_trace_id,
            "$ai_base_url": str(base_url),
            **(posthog_properties or {}),
            **(error_params or {}),
        }

        available_tool_calls = extract_available_tool_calls(provider, kwargs)

        if available_tool_calls:
            event_properties["$ai_tools"] = available_tool_calls

        cache_read = usage.get("cache_read_input_tokens")
        if cache_read is not None and cache_read > 0:
            event_properties["$ai_cache_read_input_tokens"] = cache_read

        cache_creation = usage.get("cache_creation_input_tokens")
        if cache_creation is not None and cache_creation > 0:
            event_properties["$ai_cache_creation_input_tokens"] = cache_creation

        reasoning = usage.get("reasoning_tokens")
        if reasoning is not None and reasoning > 0:
            event_properties["$ai_reasoning_tokens"] = reasoning

        if posthog_distinct_id is None:
            event_properties["$process_person_profile"] = False

        # Process instructions for Responses API
        if provider == "openai" and kwargs.get("instructions") is not None:
            event_properties["$ai_instructions"] = with_privacy_mode(
                ph_client, posthog_privacy_mode, kwargs.get("instructions")
            )

        # send the event to posthog
        if hasattr(ph_client, "capture") and callable(ph_client.capture):
            ph_client.capture(
                distinct_id=posthog_distinct_id or posthog_trace_id,
                event="$ai_generation",
                properties=event_properties,
                groups=posthog_groups,
            )

    if error:
        raise error

    return response


async def call_llm_and_track_usage_async(
    posthog_distinct_id: Optional[str],
    ph_client: PostHogClient,
    provider: str,
    posthog_trace_id: Optional[str],
    posthog_properties: Optional[Dict[str, Any]],
    posthog_privacy_mode: bool,
    posthog_groups: Optional[Dict[str, Any]],
    base_url: str,
    call_async_method: Callable[..., Any],
    **kwargs: Any,
) -> Any:
    start_time = time.time()
    response = None
    error = None
    http_status = 200
    usage: TokenUsage = TokenUsage()
    error_params: Dict[str, Any] = {}

    try:
        response = await call_async_method(**kwargs)
    except Exception as exc:
        error = exc
        http_status = getattr(
            exc, "status_code", 0
        )  # default to 0 because its likely an SDK error
        error_params = {
            "$ai_is_error": True,
            "$ai_error": exc.__str__(),
        }
    finally:
        end_time = time.time()
        latency = end_time - start_time

        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        if response and (
            hasattr(response, "usage")
            or (provider == "gemini" and hasattr(response, "usage_metadata"))
        ):
            usage = get_usage(response, provider)

        messages = merge_system_prompt(kwargs, provider)
        sanitized_messages = sanitize_messages(messages, provider)

        event_properties = {
            "$ai_provider": provider,
            "$ai_model": kwargs.get("model"),
            "$ai_model_parameters": get_model_params(kwargs),
            "$ai_input": with_privacy_mode(
                ph_client, posthog_privacy_mode, sanitized_messages
            ),
            "$ai_output_choices": with_privacy_mode(
                ph_client, posthog_privacy_mode, format_response(response, provider)
            ),
            "$ai_http_status": http_status,
            "$ai_input_tokens": usage.get("input_tokens", 0),
            "$ai_output_tokens": usage.get("output_tokens", 0),
            "$ai_latency": latency,
            "$ai_trace_id": posthog_trace_id,
            "$ai_base_url": str(base_url),
            **(posthog_properties or {}),
            **(error_params or {}),
        }

        available_tool_calls = extract_available_tool_calls(provider, kwargs)

        if available_tool_calls:
            event_properties["$ai_tools"] = available_tool_calls

        cache_read = usage.get("cache_read_input_tokens")
        if cache_read is not None and cache_read > 0:
            event_properties["$ai_cache_read_input_tokens"] = cache_read

        cache_creation = usage.get("cache_creation_input_tokens")
        if cache_creation is not None and cache_creation > 0:
            event_properties["$ai_cache_creation_input_tokens"] = cache_creation

        if posthog_distinct_id is None:
            event_properties["$process_person_profile"] = False

        # Process instructions for Responses API
        if provider == "openai" and kwargs.get("instructions") is not None:
            event_properties["$ai_instructions"] = with_privacy_mode(
                ph_client, posthog_privacy_mode, kwargs.get("instructions")
            )

        # send the event to posthog
        if hasattr(ph_client, "capture") and callable(ph_client.capture):
            ph_client.capture(
                distinct_id=posthog_distinct_id or posthog_trace_id,
                event="$ai_generation",
                properties=event_properties,
                groups=posthog_groups,
            )

    if error:
        raise error

    return response


def sanitize_messages(data: Any, provider: str) -> Any:
    """Sanitize messages using provider-specific sanitization functions."""
    if provider == "anthropic":
        return sanitize_anthropic(data)
    elif provider == "openai":
        return sanitize_openai(data)
    elif provider == "gemini":
        return sanitize_gemini(data)
    elif provider == "langchain":
        return sanitize_langchain(data)
    return data


def with_privacy_mode(ph_client: PostHogClient, privacy_mode: bool, value: Any):
    if ph_client.privacy_mode or privacy_mode:
        return None
    return value


def capture_streaming_event(
    ph_client: PostHogClient,
    event_data: StreamingEventData,
):
    """
    Unified streaming event capture for all LLM providers.

    This function handles the common logic for capturing streaming events across all providers.
    All provider-specific formatting should be done BEFORE calling this function.

    The function handles:
    - Building PostHog event properties
    - Extracting and adding tools based on provider
    - Applying privacy mode
    - Adding special token fields (cache, reasoning)
    - Provider-specific fields (e.g., OpenAI instructions)
    - Sending the event to PostHog

    Args:
        ph_client: PostHog client instance
        event_data: Standardized streaming event data containing all necessary information
    """
    trace_id = event_data.get("trace_id") or str(uuid.uuid4())

    # Build base event properties
    event_properties = {
        "$ai_provider": event_data["provider"],
        "$ai_model": event_data["model"],
        "$ai_model_parameters": get_model_params(event_data["kwargs"]),
        "$ai_input": with_privacy_mode(
            ph_client,
            event_data["privacy_mode"],
            event_data["formatted_input"],
        ),
        "$ai_output_choices": with_privacy_mode(
            ph_client,
            event_data["privacy_mode"],
            event_data["formatted_output"],
        ),
        "$ai_http_status": 200,
        "$ai_input_tokens": event_data["usage_stats"].get("input_tokens", 0),
        "$ai_output_tokens": event_data["usage_stats"].get("output_tokens", 0),
        "$ai_latency": event_data["latency"],
        "$ai_trace_id": trace_id,
        "$ai_base_url": str(event_data["base_url"]),
        **(event_data.get("properties") or {}),
    }

    # Extract and add tools based on provider
    available_tools = extract_available_tool_calls(
        event_data["provider"],
        event_data["kwargs"],
    )
    if available_tools:
        event_properties["$ai_tools"] = available_tools

    # Add optional token fields
    # For Anthropic, always include cache fields even if 0 (backward compatibility)
    # For others, only include if present and non-zero
    if event_data["provider"] == "anthropic":
        # Anthropic always includes cache fields
        cache_read = event_data["usage_stats"].get("cache_read_input_tokens", 0)
        cache_creation = event_data["usage_stats"].get("cache_creation_input_tokens", 0)
        event_properties["$ai_cache_read_input_tokens"] = cache_read
        event_properties["$ai_cache_creation_input_tokens"] = cache_creation
    else:
        # Other providers only include if non-zero
        optional_token_fields = [
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "reasoning_tokens",
        ]

        for field in optional_token_fields:
            value = event_data["usage_stats"].get(field)
            if value is not None and isinstance(value, int) and value > 0:
                event_properties[f"$ai_{field}"] = value

    # Handle provider-specific fields
    if (
        event_data["provider"] == "openai"
        and event_data["kwargs"].get("instructions") is not None
    ):
        event_properties["$ai_instructions"] = with_privacy_mode(
            ph_client,
            event_data["privacy_mode"],
            event_data["kwargs"]["instructions"],
        )

    if event_data.get("distinct_id") is None:
        event_properties["$process_person_profile"] = False

    # Send event to PostHog
    if hasattr(ph_client, "capture"):
        ph_client.capture(
            distinct_id=event_data.get("distinct_id") or trace_id,
            event="$ai_generation",
            properties=event_properties,
            groups=event_data.get("groups"),
        )
