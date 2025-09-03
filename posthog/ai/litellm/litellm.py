import os
import time
import uuid
from typing import Any, Dict, Optional

try:
    import litellm
except ImportError:
    raise ModuleNotFoundError(
        "Please install the LiteLLM SDK to use this feature: 'pip install litellm'"
    )

from posthog.ai.utils import (
    call_llm_and_track_usage,
    call_llm_and_track_usage_async,
    extract_available_tool_calls,
    get_model_params,
    with_privacy_mode,
)
from posthog.ai.sanitization import sanitize_openai
from posthog.client import Client as PostHogClient
from posthog import setup


def _setup_client_and_trace_id(
    posthog_client: Optional[PostHogClient], posthog_trace_id: Optional[str]
) -> tuple[PostHogClient, str]:
    """Common setup logic for both sync and async completion functions."""
    ph_client = posthog_client or setup()
    if posthog_trace_id is None:
        posthog_trace_id = str(uuid.uuid4())
    return ph_client, posthog_trace_id


def _resolve_base_url(kwargs: Dict[str, Any]) -> str:
    return str(
        kwargs.get("base_url")
        or kwargs.get("api_base")
        or os.getenv("LITELLM_BASE_URL")
        or "python-sdk"
    )


def _strip_provider_from_model(model: str) -> str:
    """
    Strip provider prefix from LiteLLM model name for proper price matching.
    Examples:
    - "openai/gpt-4" -> "gpt-4"
    - "anthropic/claude-3" -> "claude-3"
    - "gpt-4" -> "gpt-4" (no change if no provider prefix)
    """
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def completion(
    posthog_client: Optional[PostHogClient] = None,
    posthog_distinct_id: Optional[str] = None,
    posthog_trace_id: Optional[str] = None,
    posthog_properties: Optional[Dict[str, Any]] = None,
    posthog_privacy_mode: bool = False,
    posthog_groups: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
):
    ph_client, posthog_trace_id = _setup_client_and_trace_id(
        posthog_client, posthog_trace_id
    )

    if kwargs.get("stream", False):
        return _create_streaming(
            ph_client,
            posthog_distinct_id,
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            **kwargs,
        )

    tracking_model = None
    if "model" in kwargs and kwargs["model"]:
        tracking_model = _strip_provider_from_model(kwargs["model"])

    return call_llm_and_track_usage(
        posthog_distinct_id,
        ph_client,
        "litellm",
        posthog_trace_id,
        posthog_properties,
        posthog_privacy_mode,
        posthog_groups,
        _resolve_base_url(kwargs),
        litellm.completion,
        tracking_model=tracking_model,
        **kwargs,
    )


async def acompletion(
    posthog_client: Optional[PostHogClient] = None,
    posthog_distinct_id: Optional[str] = None,
    posthog_trace_id: Optional[str] = None,
    posthog_properties: Optional[Dict[str, Any]] = None,
    posthog_privacy_mode: bool = False,
    posthog_groups: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
):
    ph_client, posthog_trace_id = _setup_client_and_trace_id(
        posthog_client, posthog_trace_id
    )

    if kwargs.get("stream", False):
        return await _create_streaming_async(
            ph_client,
            posthog_distinct_id,
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            **kwargs,
        )

    # Strip provider prefix from model name for tracking
    tracking_model = None
    if "model" in kwargs and kwargs["model"]:
        tracking_model = _strip_provider_from_model(kwargs["model"])

    return await call_llm_and_track_usage_async(
        posthog_distinct_id,
        ph_client,
        "litellm",
        posthog_trace_id,
        posthog_properties,
        posthog_privacy_mode,
        posthog_groups,
        _resolve_base_url(kwargs),
        litellm.acompletion,
        tracking_model=tracking_model,
        **kwargs,
    )


def embedding(
    posthog_client: Optional[PostHogClient] = None,
    posthog_distinct_id: Optional[str] = None,
    posthog_trace_id: Optional[str] = None,
    posthog_properties: Optional[Dict[str, Any]] = None,
    posthog_privacy_mode: bool = False,
    posthog_groups: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
):
    """
    Create embeddings using LiteLLM, with automatic PostHog tracking.

    Args:
        posthog_client: Optional PostHog client instance
        posthog_distinct_id: Optional distinct ID for the user
        posthog_trace_id: Optional trace ID for linking events
        posthog_properties: Optional additional properties to track
        posthog_privacy_mode: Whether to sanitize input/output for privacy
        posthog_groups: Optional groups to associate with the event
        **kwargs: Additional arguments passed to litellm.embedding()

    Returns:
        The response from litellm.embedding()
    """
    ph_client, posthog_trace_id = _setup_client_and_trace_id(
        posthog_client, posthog_trace_id
    )

    if posthog_trace_id is None:
        posthog_trace_id = str(uuid.uuid4())

    start_time = time.time()
    response = litellm.embedding(**kwargs)
    end_time = time.time()

    # Extract usage statistics if available
    usage_stats = {}
    if hasattr(response, "usage") and response.usage:
        usage_stats = {
            "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
            "total_tokens": getattr(response.usage, "total_tokens", 0),
        }

    latency = end_time - start_time

    # Build the event properties for embeddings
    model = kwargs.get("model")
    stripped_model = _strip_provider_from_model(model) if model else model

    event_properties = {
        "$ai_provider": "litellm",
        "$ai_model": stripped_model,
        "$ai_input": with_privacy_mode(
            ph_client, posthog_privacy_mode, kwargs.get("input")
        ),
        "$ai_http_status": 200,
        "$ai_input_tokens": usage_stats.get("prompt_tokens", 0),
        "$ai_latency": latency,
        "$ai_trace_id": posthog_trace_id,
        "$ai_base_url": _resolve_base_url(kwargs),
        **(posthog_properties or {}),
    }

    if posthog_distinct_id is None:
        event_properties["$process_person_profile"] = False

    # Send capture event for embeddings
    if hasattr(ph_client, "capture"):
        ph_client.capture(
            distinct_id=posthog_distinct_id or posthog_trace_id,
            event="$ai_embedding",
            properties=event_properties,
            groups=posthog_groups,
        )

    return response


def _ensure_stream_usage(kwargs: Dict[str, Any]) -> None:
    if "stream_options" not in kwargs:
        kwargs["stream_options"] = {}
    kwargs["stream_options"]["include_usage"] = True  # per docs


def _extract_usage_stats(chunk) -> Dict[str, int]:
    """Extract usage statistics from a streaming chunk."""
    usage_stats = {
        "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0),
        "completion_tokens": getattr(chunk.usage, "completion_tokens", 0),
        "total_tokens": getattr(chunk.usage, "total_tokens", 0),
    }

    if hasattr(chunk.usage, "prompt_tokens_details") and getattr(
        chunk.usage.prompt_tokens_details, "cached_tokens", None
    ):
        usage_stats["cache_read_input_tokens"] = (
            chunk.usage.prompt_tokens_details.cached_tokens
        )

    if hasattr(chunk.usage, "output_tokens_details") and getattr(
        chunk.usage.output_tokens_details, "reasoning_tokens", None
    ):
        usage_stats["reasoning_tokens"] = (
            chunk.usage.output_tokens_details.reasoning_tokens
        )

    return usage_stats


def _extract_chunk_content(chunk) -> Optional[str]:
    """Extract content from a streaming chunk if available."""
    if getattr(chunk, "choices", None):
        if (
            chunk.choices
            and len(chunk.choices) > 0
            and getattr(chunk.choices[0], "delta", None)
            and getattr(chunk.choices[0].delta, "content", None)
        ):
            return chunk.choices[0].delta.content
    return None


def _create_streaming(
    ph_client: PostHogClient,
    posthog_distinct_id: Optional[str],
    posthog_trace_id: Optional[str],
    posthog_properties: Optional[Dict[str, Any]],
    posthog_privacy_mode: bool,
    posthog_groups: Optional[Dict[str, Any]],
    **kwargs: Any,
):
    _ensure_stream_usage(kwargs)
    start_time = time.time()
    usage_stats: Dict[str, int] = {}
    accumulated_content: list[str] = []
    response = litellm.completion(**kwargs)

    def generator():
        nonlocal usage_stats, accumulated_content

        try:
            for chunk in response:
                if getattr(chunk, "usage", None):
                    usage_stats = _extract_usage_stats(chunk)

                content = _extract_chunk_content(chunk)
                if content:
                    accumulated_content.append(content)

                yield chunk

        finally:
            latency = time.time() - start_time
            output = "".join(accumulated_content)
            _capture_streaming_event(
                ph_client,
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
                kwargs,
                usage_stats,
                latency,
                output,
                base_url=_resolve_base_url(kwargs),
                available_tool_calls=extract_available_tool_calls("openai", kwargs),
            )

    return generator()


async def _create_streaming_async(
    ph_client: PostHogClient,
    posthog_distinct_id: Optional[str],
    posthog_trace_id: Optional[str],
    posthog_properties: Optional[Dict[str, Any]],
    posthog_privacy_mode: bool,
    posthog_groups: Optional[Dict[str, Any]],
    **kwargs: Any,
):
    _ensure_stream_usage(kwargs)
    start_time = time.time()
    usage_stats: Dict[str, int] = {}
    accumulated_content: list[str] = []
    response = await litellm.acompletion(**kwargs)

    async def generator():
        nonlocal usage_stats, accumulated_content

        try:
            async for chunk in response:
                if getattr(chunk, "usage", None):
                    usage_stats = _extract_usage_stats(chunk)

                content = _extract_chunk_content(chunk)
                if content:
                    accumulated_content.append(content)

                yield chunk

        finally:
            latency = time.time() - start_time
            output = "".join(accumulated_content)
            _capture_streaming_event(
                ph_client,
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
                kwargs,
                usage_stats,
                latency,
                output,
                base_url=_resolve_base_url(kwargs),
                available_tool_calls=extract_available_tool_calls("openai", kwargs),
            )

    return generator()


def _capture_streaming_event(
    ph_client: PostHogClient,
    posthog_distinct_id: Optional[str],
    posthog_trace_id: Optional[str],
    posthog_properties: Optional[Dict[str, Any]],
    posthog_privacy_mode: bool,
    posthog_groups: Optional[Dict[str, Any]],
    kwargs: Dict[str, Any],
    usage_stats: Dict[str, int],
    latency: float,
    output: Any,
    *,
    base_url: str,
    available_tool_calls: Optional[list[dict]] = None,
):
    if posthog_trace_id is None:
        posthog_trace_id = str(uuid.uuid4())

    model = kwargs.get("model")
    stripped_model = _strip_provider_from_model(model) if model else model

    event_properties = {
        "$ai_provider": "litellm",
        "$ai_model": stripped_model,
        "$ai_model_parameters": get_model_params(kwargs),
        "$ai_input": with_privacy_mode(
            ph_client, posthog_privacy_mode, sanitize_openai(kwargs.get("messages"))
        ),
        "$ai_output_choices": with_privacy_mode(
            ph_client, posthog_privacy_mode, [{"content": output, "role": "assistant"}]
        ),
        "$ai_http_status": 200,
        "$ai_input_tokens": usage_stats.get("prompt_tokens", 0),
        "$ai_output_tokens": usage_stats.get("completion_tokens", 0),
        "$ai_cache_read_input_tokens": usage_stats.get("cache_read_input_tokens", 0),
        "$ai_reasoning_tokens": usage_stats.get("reasoning_tokens", 0),
        "$ai_latency": latency,
        "$ai_trace_id": posthog_trace_id,
        "$ai_base_url": base_url,
        **(posthog_properties or {}),
    }

    if available_tool_calls:
        event_properties["$ai_tools"] = available_tool_calls

    if posthog_distinct_id is None:
        event_properties["$process_person_profile"] = False

    if hasattr(ph_client, "capture"):
        ph_client.capture(
            distinct_id=posthog_distinct_id or posthog_trace_id,
            event="$ai_generation",
            properties=event_properties,
            groups=posthog_groups,
        )
