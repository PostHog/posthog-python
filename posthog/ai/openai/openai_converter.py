"""
OpenAI-specific conversion utilities.

This module handles the conversion of OpenAI API responses and inputs
into standardized formats for PostHog tracking. It supports both
Chat Completions API and Responses API formats.
"""

from typing import Any, Dict, List, Optional, Union, cast

from posthog.ai.media import to_plain
from posthog.ai.types import (
    FormattedContentItem,
    FormattedFunctionCall,
    FormattedImageContent,
    FormattedMessage,
    FormattedTextContent,
    TokenUsage,
)
from posthog.ai.utils import (
    _responses_stop_reason,
    _redact_mcp_tool_credentials,
    serialize_raw_usage,
)


def _item_attr(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _format_responses_output_items(items: Any) -> List[FormattedContentItem]:
    content: List[FormattedContentItem] = []

    for item in items:
        item_type = _item_attr(item, "type")

        if item_type == "message":
            content.extend(_format_response_message_content(item))

        elif item_type == "function_call":
            call_id = _item_attr(item, "call_id")
            if call_id is None:
                call_id = _item_attr(item, "id", "")
            content.append(
                {
                    "type": "function",
                    "id": call_id,
                    "function": {
                        "name": _item_attr(item, "name"),
                        "arguments": _item_attr(item, "arguments", {}),
                    },
                }
            )

        elif item_type == "reasoning":
            content.append(to_plain(item))

        elif item_type == "image_generation_call":
            content.append(
                {
                    "type": "image_generation_call",
                    "result": _item_attr(item, "result"),
                    "status": _item_attr(item, "status"),
                }
            )

        elif item_type is not None:
            plain_item = to_plain(item)
            content.append(
                plain_item if isinstance(plain_item, dict) else {"type": item_type}
            )

    return content


def _responses_output_role(items: Any) -> str:
    role = "assistant"

    for item in items:
        if _item_attr(item, "type") == "message":
            item_role = _item_attr(item, "role")
            if item_role is not None:
                role = item_role

    return role


def format_openai_response(response: Any) -> List[FormattedMessage]:
    """
    Format an OpenAI response into standardized message format.

    Handles both Chat Completions API and Responses API formats.

    Args:
        response: The response object from OpenAI API

    Returns:
        List of formatted messages with role and content
    """

    output: List[FormattedMessage] = []

    if response is None:
        return output

    # Handle Chat Completions response format
    if hasattr(response, "choices"):
        message = _format_chat_completion_choices(response.choices)
        if message is not None:
            output.append(message)

    # Handle Responses API format
    if hasattr(response, "output"):
        content = _format_responses_output_items(response.output)
        role = _responses_output_role(response.output)

        if content:
            output.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    return output


def format_openai_input(
    messages: Optional[List[Dict[str, Any]]] = None, input_data: Optional[Any] = None
) -> List[FormattedMessage]:
    """
    Format OpenAI input messages.

    Handles both messages parameter (Chat Completions) and input parameter (Responses API).

    Args:
        messages: List of message dictionaries for Chat Completions API
        input_data: Input data for Responses API

    Returns:
        List of formatted messages
    """

    formatted_messages: List[FormattedMessage] = []

    if messages is not None:
        for msg in messages:
            formatted_messages.append(_format_chat_input_message(msg))

    # Handle Responses API format
    if input_data is not None:
        if isinstance(input_data, list):
            for item in input_data:
                formatted_messages.append(_format_response_input_item(item))

        elif isinstance(input_data, str):
            formatted_messages.append({"role": "user", "content": input_data})

        else:
            formatted_messages.append({"role": "user", "content": str(input_data)})

    return formatted_messages


def extract_openai_tools(kwargs: Dict[str, Any]) -> Optional[Any]:
    """
    Extract tool definitions from OpenAI API kwargs.

    Args:
        kwargs: Keyword arguments passed to OpenAI API

    Returns:
        Tool definitions if present, None otherwise
    """

    # Check for tools parameter (newer API)
    if "tools" in kwargs:
        return _redact_mcp_tool_credentials(kwargs["tools"])

    # Check for functions parameter (older API)
    if "functions" in kwargs:
        return kwargs["functions"]

    return None


def format_openai_streaming_content(
    accumulated_content: str, tool_calls: Optional[List[Dict[str, Any]]] = None
) -> List[FormattedContentItem]:
    """
    Format content from OpenAI streaming response.

    Used by streaming handlers to format accumulated content.

    Args:
        accumulated_content: Accumulated text content from streaming
        tool_calls: Optional list of tool calls accumulated during streaming

    Returns:
        List of formatted content items
    """
    formatted: List[FormattedContentItem] = []

    # Add text content if present
    if accumulated_content:
        text_content: FormattedTextContent = {
            "type": "text",
            "text": accumulated_content,
        }
        formatted.append(text_content)

    # Add tool calls if present
    if tool_calls:
        for tool_call in tool_calls:
            function_call: FormattedFunctionCall = {
                "type": "function",
                "id": tool_call.get("id"),
                "function": tool_call.get("function", {}),
            }
            formatted.append(function_call)

    return formatted


def extract_openai_web_search_count(response: Any) -> int:
    """
    Extract web search count from OpenAI response.

    Uses a two-tier detection strategy:
    1. Priority 1 (exact count): Check for output[].type == "web_search_call" (Responses API)
    2. Priority 2 (binary detection): Check for various web search indicators:
       - Root-level citations, search_results, or usage.search_context_size (Perplexity)
       - Annotations with type "url_citation" in choices/output (including delta for streaming)

    Args:
        response: The response from OpenAI API

    Returns:
        Number of web search requests (exact count or binary 1/0)
    """

    count = _count_response_web_search_calls(response)
    if count > 0:
        return count
    if _has_root_web_search(response):
        return 1
    if _has_choice_web_citations(response):
        return 1
    if _has_response_web_citations(response):
        return 1
    return 0


def extract_openai_stop_reason(response: Any) -> Optional[str]:
    """Extract stop reason from OpenAI response."""
    # Chat Completions API
    if hasattr(response, "choices") and response.choices:
        return getattr(response.choices[0], "finish_reason", None)
    # Responses API
    if hasattr(response, "status"):
        return _responses_stop_reason(response)
    return None


def extract_openai_usage_from_response(response: Any) -> TokenUsage:
    """
    Extract usage statistics from a full OpenAI response (non-streaming).
    Handles both Chat Completions and Responses API.

    Args:
        response: The complete response from OpenAI API

    Returns:
        TokenUsage with standardized usage statistics
    """
    if not hasattr(response, "usage") or not response.usage:
        return TokenUsage()

    result = _extract_openai_response_token_usage(response.usage)

    web_search_count = extract_openai_web_search_count(response)
    if web_search_count > 0:
        result["web_search_count"] = web_search_count

    # Capture raw usage metadata for backend processing
    # Serialize to dict here in the converter (not in utils)
    serialized = serialize_raw_usage(response.usage)
    if serialized:
        result["raw_usage"] = serialized

    return result


# Stream events that carry the final Responses API response, with its usage and
# output. Exactly one of them ends a stream.
_RESPONSES_TERMINAL_EVENT_TYPES = (
    "response.completed",
    "response.incomplete",
    "response.failed",
)


def extract_openai_usage_from_chunk(
    chunk: Any, provider_type: str = "chat"
) -> TokenUsage:
    """
    Extract usage statistics from an OpenAI streaming chunk.

    Handles both Chat Completions and Responses API formats.

    Args:
        chunk: Streaming chunk from OpenAI API
        provider_type: Either "chat" or "responses" to handle different API formats

    Returns:
        Dictionary of usage statistics
    """

    usage: TokenUsage = TokenUsage()

    if provider_type == "chat":
        # Extract web search count from the chunk before checking for usage
        # Web search indicators (citations, annotations) can appear on any chunk,
        # not just those with usage data
        web_search_count = extract_openai_web_search_count(chunk)
        if web_search_count > 0:
            usage["web_search_count"] = web_search_count

        if not hasattr(chunk, "usage") or not chunk.usage:
            return usage

        usage.update(_extract_openai_stream_token_usage(chunk.usage, "chat"))
        _add_openai_raw_usage(usage, chunk.usage)

    elif provider_type == "responses":
        usage.update(_extract_openai_response_chunk_usage(chunk))

    return usage


def extract_openai_content_from_chunk(
    chunk: Any, provider_type: str = "chat"
) -> Optional[Any]:
    """
    Extract content from an OpenAI streaming chunk.

    Handles both Chat Completions and Responses API formats.

    Args:
        chunk: Streaming chunk from OpenAI API
        provider_type: Either "chat" or "responses" to handle different API formats

    Returns:
        For "chat": text content (str), or an audio/refusal delta block (dict),
        if present. For "responses": the full `response.output` list on the
        terminal (`response.completed`, `response.incomplete` or
        `response.failed`) event. None otherwise.
    """

    if provider_type == "chat":
        return _extract_openai_chat_content(chunk)

    elif provider_type == "responses":
        # Responses API format
        if getattr(chunk, "type", None) in _RESPONSES_TERMINAL_EVENT_TYPES:
            if hasattr(chunk, "response") and chunk.response:
                res = chunk.response
                if res.output:
                    return res.output

    return None


def extract_openai_tool_calls_from_chunk(chunk: Any) -> Optional[List[Dict[str, Any]]]:
    """
    Extract tool calls from an OpenAI streaming chunk.

    Args:
        chunk: Streaming chunk from OpenAI API

    Returns:
        List of tool call deltas if present, None otherwise
    """
    if (
        _has_openai_chat_delta(chunk)
        and hasattr(chunk.choices[0].delta, "tool_calls")
        and chunk.choices[0].delta.tool_calls
    ):
        tool_calls = []
        for tool_call in chunk.choices[0].delta.tool_calls:
            tool_calls.append(_format_openai_tool_delta(tool_call))

        return tool_calls

    return None


def accumulate_openai_tool_calls(
    accumulated_tool_calls: Dict[Union[int, str], Dict[str, Any]],
    chunk_tool_calls: List[Dict[str, Any]],
) -> None:
    """
    Accumulate tool calls from streaming chunks.

    OpenAI sends tool calls incrementally:
    - First chunk has id, type, function.name and partial function.arguments
    - Subsequent chunks have more function.arguments
    Deltas without an index are matched by id, start a call when they carry an id
    or a function name, and otherwise continue the latest call.

    Args:
        accumulated_tool_calls: Dictionary mapping index to accumulated tool call data
        chunk_tool_calls: List of tool call deltas from current chunk
    """
    for tool_call_delta in chunk_tool_calls:
        index = _openai_tool_delta_index(accumulated_tool_calls, tool_call_delta)

        # Initialize tool call if first time seeing this index
        if index not in accumulated_tool_calls:
            accumulated_tool_calls[index] = {
                "id": "",
                "type": "function",
                "function": {
                    "name": "",
                    "arguments": "",
                },
            }

        _apply_openai_tool_delta(accumulated_tool_calls[index], tool_call_delta)


def format_openai_streaming_output(
    accumulated_content: Any,
    provider_type: str = "chat",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> List[FormattedMessage]:
    """
    Format the final output from OpenAI streaming.

    Args:
        accumulated_content: Accumulated content from streaming (string for chat, list for responses)
        provider_type: Either "chat" or "responses" to handle different API formats
        tool_calls: Optional list of accumulated tool calls

    Returns:
        List of formatted messages
    """

    if provider_type == "chat":
        return _format_openai_chat_stream_output(accumulated_content, tool_calls)

    elif provider_type == "responses":
        output = _format_openai_responses_stream_output(accumulated_content)
        if output is not None:
            return output

    # Fallback for any other format
    return [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": str(accumulated_content)}],
        }
    ]


def format_openai_streaming_input(
    kwargs: Dict[str, Any], api_type: str = "chat"
) -> Any:
    """
    Format OpenAI streaming input based on API type.

    Args:
        kwargs: Keyword arguments passed to OpenAI API
        api_type: Either "chat" or "responses"

    Returns:
        Formatted input ready for PostHog tracking
    """
    from posthog.ai.utils import merge_system_prompt

    return merge_system_prompt(kwargs, "openai")


def _format_response_message_content(item: Any) -> List[FormattedContentItem]:
    content: List[FormattedContentItem] = []
    message_content = _item_attr(item, "content")
    if isinstance(message_content, list):
        for content_item in message_content:
            block = _format_response_content_item(content_item)
            if block is not None:
                content.append(block)

    elif message_content is not None:
        content.append({"type": "text", "text": str(message_content)})

    return content


def _format_response_content_item(content_item: Any) -> Optional[FormattedContentItem]:
    content_item_type = _item_attr(content_item, "type")
    content_item_text = _item_attr(content_item, "text")
    content_item_refusal = _item_attr(content_item, "refusal")

    if content_item_type == "output_text" and (content_item_text is not None):
        return {"type": "text", "text": content_item_text}

    elif content_item_type == "refusal" and (content_item_refusal is not None):
        return {"type": "refusal", "refusal": content_item_refusal}

    elif content_item_text is not None:
        return {"type": "text", "text": content_item_text}

    elif content_item_type == "input_image" and (
        _item_attr(content_item, "image_url") is not None
    ):
        image_content: FormattedImageContent = {
            "type": "image",
            "image": _item_attr(content_item, "image_url"),
        }
        return image_content

    return None


def _format_chat_message(message: Any) -> List[FormattedContentItem]:
    content: List[FormattedContentItem] = []
    if message.content:
        content.append(
            {
                "type": "text",
                "text": message.content,
            }
        )

    # A refused request carries its text in `refusal` and leaves `content` empty
    refusal = getattr(message, "refusal", None)
    if refusal:
        content.append({"type": "refusal", "refusal": refusal})

    if hasattr(message, "tool_calls") and message.tool_calls:
        for tool_call in message.tool_calls:
            block = _format_chat_tool_call(tool_call)
            if block is not None:
                content.append(block)

    # Handle audio output (gpt-4o-audio-preview)
    if hasattr(message, "audio") and message.audio:
        # Convert Pydantic model to dict to capture all fields from OpenAI
        audio_dict = message.audio.model_dump()
        content.append({"type": "audio", **audio_dict})

    return content


def _format_chat_tool_call(tool_call: Any) -> Optional[FormattedContentItem]:
    # Custom tools carry their free-form text in `custom.input`,
    # with no `function` object.
    is_custom = getattr(tool_call, "type", None) == "custom"
    call = getattr(tool_call, "custom" if is_custom else "function", None)
    if call is None:
        return None
    return {
        "type": "function",
        "id": tool_call.id,
        "function": {
            "name": call.name,
            "arguments": call.input if is_custom else call.arguments,
        },
    }


def _format_chat_input_message(msg: Any) -> FormattedMessage:
    plain = to_plain(msg)
    if not isinstance(plain, dict):
        plain = {"role": "user", "content": str(plain)}

    formatted: Dict[str, Any] = {
        "role": plain.get("role", "user"),
        "content": plain.get("content"),
    }

    for key in ("tool_calls", "tool_call_id", "name", "audio", "refusal"):
        if plain.get(key) is not None:
            formatted[key] = to_plain(plain[key]) if key == "audio" else plain[key]

    return cast(FormattedMessage, formatted)


def _format_response_input_item(item: Any) -> FormattedMessage:
    if not isinstance(item, (dict, str)):
        item = to_plain(item)

    if (
        isinstance(item, dict)
        and "type" in item
        and "role" not in item
        and "content" not in item
    ):
        return cast(FormattedMessage, to_plain(item))

    role = "user"
    content = ""

    if isinstance(item, dict):
        role = item.get("role", "user")
        content = item.get("content", "")

    elif isinstance(item, str):
        content = item

    else:
        content = str(item)

    return {"role": role, "content": content}


def _count_response_web_search_calls(response: Any) -> int:
    # Priority 1: exact count in Responses API output.
    count = 0
    if hasattr(response, "output"):
        for item in response.output:
            if hasattr(item, "type") and item.type == "web_search_call":
                count += 1
    return max(0, count)


def _has_root_web_search(response: Any) -> bool:
    # Priority 2: binary detection, including Perplexity root-level indicators.
    for field in ("citations", "search_results"):
        if hasattr(response, field):
            values = getattr(response, field)
            if values and len(values) > 0:
                return True
    if hasattr(response, "usage") and hasattr(response.usage, "search_context_size"):
        if response.usage.search_context_size:
            return True
    return False


def _has_url_citation_annotations(item: Any) -> bool:
    if hasattr(item, "annotations"):
        annotations = item.annotations
        if annotations:
            for annotation in annotations:
                if _item_attr(annotation, "type") == "url_citation":
                    return True
    return False


def _has_choice_web_citations(response: Any) -> bool:
    if hasattr(response, "choices"):
        for choice in response.choices:
            # Check message before delta, including streaming chunks.
            for field in ("message", "delta"):
                if hasattr(choice, field):
                    if _has_url_citation_annotations(getattr(choice, field)):
                        return True
    return False


def _has_response_web_citations(response: Any) -> bool:
    if hasattr(response, "output"):
        for item in response.output:
            if hasattr(item, "content") and isinstance(item.content, list):
                for content_item in item.content:
                    if _has_url_citation_annotations(content_item):
                        return True
    return False


def _extract_openai_response_token_usage(usage: Any) -> TokenUsage:
    # Read Responses fields first, then Chat Completions fields. The latter
    # override only fields they report, including explicitly null values.
    counts = _read_openai_response_usage_fields(usage, "responses")
    counts.update(_read_openai_response_usage_fields(usage, "chat"))
    result: Dict[str, Any] = {}
    for field in ("input_tokens", "output_tokens"):
        value = counts.get(field)
        if value is not None:
            result[field] = value
    _add_openai_response_details(result, counts)
    return cast(TokenUsage, result)


def _read_openai_response_usage_fields(usage: Any, api_type: str) -> Dict[str, Any]:
    if api_type == "responses":
        token_fields = (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
        )
        detail_fields = (
            ("cache_read_input_tokens", "input_tokens_details", "cached_tokens"),
            ("reasoning_tokens", "output_tokens_details", "reasoning_tokens"),
        )
    else:
        token_fields = (
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
        )
        detail_fields = (
            ("cache_read_input_tokens", "prompt_tokens_details", "cached_tokens"),
            ("reasoning_tokens", "completion_tokens_details", "reasoning_tokens"),
        )
    counts = {}
    for output, field in token_fields:
        if hasattr(usage, field):
            counts[output] = getattr(usage, field)
    for output, field, detail in detail_fields:
        if hasattr(usage, field) and hasattr(getattr(usage, field), detail):
            counts[output] = getattr(getattr(usage, field), detail)
    return counts


def _add_openai_response_details(
    result: Dict[str, Any], counts: Dict[str, Any]
) -> None:
    for field in ("cache_read_input_tokens", "reasoning_tokens"):
        value = counts.get(field)
        if value is not None and value > 0:
            result[field] = value


def _extract_openai_stream_token_usage(raw: Any, provider_type: str) -> TokenUsage:
    if provider_type == "chat":
        input_field, output_field = "prompt_tokens", "completion_tokens"
        input_details, output_details = (
            "prompt_tokens_details",
            "completion_tokens_details",
        )
    else:
        input_field, output_field = "input_tokens", "output_tokens"
        input_details, output_details = "input_tokens_details", "output_tokens_details"
    usage: Dict[str, Any] = {
        "input_tokens": getattr(raw, input_field, 0),
        "output_tokens": getattr(raw, output_field, 0),
    }
    # Unlike non-streaming usage, explicitly reported zero details are retained.
    for output, field, detail in (
        ("cache_read_input_tokens", input_details, "cached_tokens"),
        ("reasoning_tokens", output_details, "reasoning_tokens"),
    ):
        if hasattr(raw, field) and hasattr(getattr(raw, field), detail):
            value = getattr(getattr(raw, field), detail)
            if value is not None:
                usage[output] = value
    return cast(TokenUsage, usage)


def _add_openai_raw_usage(usage: TokenUsage, raw: Any) -> None:
    # Capture raw usage metadata for backend processing. Serialize in the converter.
    serialized = serialize_raw_usage(raw)
    if serialized:
        usage["raw_usage"] = serialized


def _has_openai_chat_delta(chunk: Any) -> bool:
    return bool(
        hasattr(chunk, "choices")
        and chunk.choices
        and len(chunk.choices) > 0
        and chunk.choices[0].delta
    )


def _format_openai_tool_delta(tool_call: Any) -> Dict[str, Any]:
    tc_dict = {
        "index": getattr(tool_call, "index", None),
    }

    if hasattr(tool_call, "id") and tool_call.id:
        tc_dict["id"] = tool_call.id

    if hasattr(tool_call, "type") and tool_call.type:
        tc_dict["type"] = tool_call.type

    if hasattr(tool_call, "function") and tool_call.function:
        tc_dict["function"] = _format_openai_function_delta(tool_call.function)

    return tc_dict


def _format_openai_function_delta(function: Any) -> Dict[str, Any]:
    function_dict = {}
    if hasattr(function, "name") and function.name:
        function_dict["name"] = function.name
    if hasattr(function, "arguments") and function.arguments:
        function_dict["arguments"] = function.arguments
    return function_dict


def _openai_tool_delta_index(
    accumulated_tool_calls: Dict[Union[int, str], Dict[str, Any]],
    tool_call_delta: Dict[str, Any],
) -> Union[int, str]:
    index = tool_call_delta.get("index")
    if index is None:
        # Some OpenAI-compatible providers omit the index. A delta with a new id
        # or a function name starts a call, and one with neither continues the
        # latest call. Those calls get keys of their own, so they can never
        # collide with an index the provider sends later.
        call_id = tool_call_delta.get("id")
        index = _openai_tool_index_by_id(accumulated_tool_calls, call_id)

        if index is None:
            has_name = bool((tool_call_delta.get("function") or {}).get("name"))
            if accumulated_tool_calls and not call_id and not has_name:
                index = next(reversed(accumulated_tool_calls))
            else:
                index = f"no-index-{len(accumulated_tool_calls)}"

    return index


def _apply_openai_tool_delta(
    tc: Dict[str, Any], tool_call_delta: Dict[str, Any]
) -> None:
    # Update with new data from delta

    for field in ("id", "type"):
        if field in tool_call_delta and tool_call_delta[field]:
            tc[field] = tool_call_delta[field]

    if "function" in tool_call_delta:
        func_delta = tool_call_delta["function"]
        if "name" in func_delta and func_delta["name"]:
            tc["function"]["name"] = func_delta["name"]
        if "arguments" in func_delta and func_delta["arguments"]:
            # Arguments are sent incrementally, concatenate them
            tc["function"]["arguments"] += func_delta["arguments"]


def _format_openai_chat_stream_output(
    accumulated_content: Any, tool_calls: Optional[List[Dict[str, Any]]]
) -> List[FormattedMessage]:
    content_items: List[FormattedContentItem] = []

    # Add text content if present
    if isinstance(accumulated_content, str) and accumulated_content:
        content_items.append({"type": "text", "text": accumulated_content})
    elif isinstance(accumulated_content, list):
        content_items.extend(_merge_openai_chat_stream_parts(accumulated_content))

    # Add tool calls if present
    if tool_calls:
        for tool_call in tool_calls:
            if "function" in tool_call:
                function_call: FormattedFunctionCall = {
                    "type": "function",
                    "id": tool_call.get("id", ""),
                    "function": tool_call["function"],
                }
                content_items.append(function_call)

    # Return formatted message with content
    if content_items:
        return [{"role": "assistant", "content": content_items}]
    else:
        # Empty response
        return [{"role": "assistant", "content": []}]


def _merge_openai_chat_stream_parts(
    accumulated_content: List[Any],
) -> List[FormattedContentItem]:
    text_parts: List[str] = []
    audio_id: Optional[str] = None
    audio_data_parts: List[str] = []
    audio_transcript_parts: List[str] = []
    refusal_parts: List[str] = []

    for item in accumulated_content:
        if isinstance(item, str):
            if item:
                text_parts.append(item)
        elif isinstance(item, dict) and item.get("type") == "audio":
            audio_id = _append_openai_audio_delta(
                item, audio_id, audio_data_parts, audio_transcript_parts
            )
        elif isinstance(item, dict) and item.get("type") == "refusal":
            if item.get("refusal"):
                refusal_parts.append(item["refusal"])

    return _format_openai_chat_stream_parts(
        text_parts, audio_id, audio_data_parts, audio_transcript_parts, refusal_parts
    )


def _append_openai_audio_delta(
    item: Dict[str, Any],
    audio_id: Optional[str],
    data_parts: List[str],
    transcript_parts: List[str],
) -> Optional[str]:
    if audio_id is None and item.get("id"):
        audio_id = item["id"]
    if item.get("data"):
        data_parts.append(item["data"])
    if item.get("transcript"):
        transcript_parts.append(item["transcript"])
    return audio_id


def _format_openai_chat_stream_parts(
    text_parts: List[str],
    audio_id: Optional[str],
    audio_data_parts: List[str],
    audio_transcript_parts: List[str],
    refusal_parts: List[str],
) -> List[FormattedContentItem]:
    content_items: List[FormattedContentItem] = []
    if text_parts:
        content_items.append({"type": "text", "text": "".join(text_parts)})

    if audio_data_parts or audio_transcript_parts:
        audio_block: Dict[str, Any] = {"type": "audio"}
        if audio_id is not None:
            audio_block["id"] = audio_id
        if audio_data_parts:
            audio_block["data"] = "".join(audio_data_parts)
        if audio_transcript_parts:
            audio_block["transcript"] = "".join(audio_transcript_parts)
        content_items.append(audio_block)

    if refusal_parts:
        content_items.append({"type": "refusal", "refusal": "".join(refusal_parts)})

    return content_items


def _format_openai_responses_stream_output(
    accumulated_content: Any,
) -> Optional[List[FormattedMessage]]:
    if isinstance(accumulated_content, list) and not accumulated_content:
        return []
    if isinstance(accumulated_content, list) and accumulated_content:
        items: List[Any] = []
        for entry in accumulated_content:
            if isinstance(entry, list):
                items.extend(entry)
            else:
                items.append(entry)

        content_items = _format_responses_output_items(items)
        role = _responses_output_role(items)

        if content_items:
            return [{"role": role, "content": content_items}]
        return []
    elif isinstance(accumulated_content, str):
        return [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": accumulated_content}],
            }
        ]

    return None


def _format_chat_completion_choices(choices: Any) -> Optional[FormattedMessage]:
    content: List[FormattedContentItem] = []
    role = "assistant"

    for choice in choices:
        if hasattr(choice, "message") and choice.message:
            if choice.message.role:
                role = choice.message.role
            content.extend(_format_chat_message(choice.message))

    if content:
        return {
            "role": role,
            "content": content,
        }

    return None


def _extract_openai_response_chunk_usage(chunk: Any) -> TokenUsage:
    usage = TokenUsage()
    # For Responses API, usage is only in chunk.response.usage on the terminal
    # event. A run cut short (e.g. by max_output_tokens) ends on
    # response.incomplete instead of response.completed, and is still billed.
    if getattr(chunk, "type", None) in _RESPONSES_TERMINAL_EVENT_TYPES:
        if (
            hasattr(chunk, "response")
            and hasattr(chunk.response, "usage")
            and chunk.response.usage
        ):
            response_usage = chunk.response.usage
            usage.update(
                _extract_openai_stream_token_usage(response_usage, "responses")
            )
            # Extract web search count from the complete response.
            if hasattr(chunk, "response"):
                web_search_count = extract_openai_web_search_count(chunk.response)
                if web_search_count > 0:
                    usage["web_search_count"] = web_search_count
            _add_openai_raw_usage(usage, response_usage)

    return usage


def _extract_openai_chat_content(chunk: Any) -> Optional[Any]:
    # Chat Completions API format
    if _has_openai_chat_delta(chunk):
        delta = chunk.choices[0].delta

        if delta.content:
            return delta.content

        audio_delta = getattr(delta, "audio", None)
        if audio_delta is not None:
            plain_audio = to_plain(audio_delta)
            if isinstance(plain_audio, dict):
                return {"type": "audio", **plain_audio}
            return {"type": "audio"}

        refusal_delta = getattr(delta, "refusal", None)
        if refusal_delta:
            return {"type": "refusal", "refusal": refusal_delta}

    return None


def _openai_tool_index_by_id(
    accumulated_tool_calls: Dict[Union[int, str], Dict[str, Any]], call_id: Any
) -> Optional[Union[int, str]]:
    return next(
        (
            key
            for key, tc in accumulated_tool_calls.items()
            if call_id and tc["id"] == call_id
        ),
        None,
    )
