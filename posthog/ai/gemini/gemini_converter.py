"""
Gemini-specific conversion utilities.

This module handles the conversion of Gemini API responses and inputs
into standardized formats for PostHog tracking.
"""

from typing import Any, Dict, List, Optional, TypedDict, Union

from posthog.ai.types import (
    FormattedContentItem,
    FormattedMessage,
    StreamingUsageStats,
    TokenUsage,
)


class GeminiPart(TypedDict, total=False):
    """Represents a part in a Gemini message."""

    text: str


class GeminiMessage(TypedDict, total=False):
    """Represents a Gemini message with various possible fields."""

    role: str
    parts: List[Union[GeminiPart, Dict[str, Any]]]
    content: Union[str, List[Any]]
    text: str


def _extract_text_from_parts(parts: List[Any]) -> str:
    """
    Extract and concatenate text from a parts array.

    Args:
        parts: List of parts that may contain text content

    Returns:
        Concatenated text from all parts
    """

    content_parts = []

    for part in parts:
        if isinstance(part, dict) and "text" in part:
            content_parts.append(part["text"])

        elif isinstance(part, str):
            content_parts.append(part)

        elif hasattr(part, "text"):
            # Get the text attribute value
            text_value = getattr(part, "text", "")
            content_parts.append(text_value if text_value else str(part))

        else:
            content_parts.append(str(part))

    return "".join(content_parts)


def _format_dict_message(item: Dict[str, Any]) -> FormattedMessage:
    """
    Format a dictionary message into standardized format.

    Args:
        item: Dictionary containing message data

    Returns:
        Formatted message with role and content
    """

    # Handle dict format with parts array (Gemini-specific format)
    if "parts" in item and isinstance(item["parts"], list):
        content = _extract_text_from_parts(item["parts"])
        return {"role": item.get("role", "user"), "content": content}

    # Handle dict with content field
    if "content" in item:
        content = item["content"]

        if isinstance(content, list):
            # If content is a list, extract text from it
            content = _extract_text_from_parts(content)

        elif not isinstance(content, str):
            content = str(content)

        return {"role": item.get("role", "user"), "content": content}

    # Handle dict with text field
    if "text" in item:
        return {"role": item.get("role", "user"), "content": item["text"]}

    # Fallback to string representation
    return {"role": "user", "content": str(item)}


def _format_object_message(item: Any) -> FormattedMessage:
    """
    Format an object (with attributes) into standardized format.

    Args:
        item: Object that may have text or parts attributes

    Returns:
        Formatted message with role and content
    """

    # Handle object with parts attribute
    if hasattr(item, "parts") and hasattr(item.parts, "__iter__"):
        content = _extract_text_from_parts(item.parts)
        role = getattr(item, "role", "user") if hasattr(item, "role") else "user"

        # Ensure role is a string
        if not isinstance(role, str):
            role = "user"

        return {"role": role, "content": content}

    # Handle object with text attribute
    if hasattr(item, "text"):
        role = getattr(item, "role", "user") if hasattr(item, "role") else "user"

        # Ensure role is a string
        if not isinstance(role, str):
            role = "user"

        return {"role": role, "content": item.text}

    # Handle object with content attribute
    if hasattr(item, "content"):
        role = getattr(item, "role", "user") if hasattr(item, "role") else "user"

        # Ensure role is a string
        if not isinstance(role, str):
            role = "user"

        content = item.content

        if isinstance(content, list):
            content = _extract_text_from_parts(content)

        elif not isinstance(content, str):
            content = str(content)
        return {"role": role, "content": content}

    # Fallback to string representation
    return {"role": "user", "content": str(item)}


def format_gemini_response(response: Any) -> List[FormattedMessage]:
    """
    Format a Gemini response into standardized message format.

    Args:
        response: The response object from Gemini API

    Returns:
        List of formatted messages with role and content
    """

    output: List[FormattedMessage] = []

    if response is None:
        return output

    if hasattr(response, "candidates") and response.candidates:
        for candidate in response.candidates:
            if hasattr(candidate, "content") and candidate.content:
                content: List[FormattedContentItem] = []

                if hasattr(candidate.content, "parts") and candidate.content.parts:
                    for part in candidate.content.parts:
                        if hasattr(part, "text") and part.text:
                            content.append(
                                {
                                    "type": "text",
                                    "text": part.text,
                                }
                            )

                        elif hasattr(part, "function_call") and part.function_call:
                            function_call = part.function_call
                            content.append(
                                {
                                    "type": "function",
                                    "function": {
                                        "name": function_call.name,
                                        "arguments": function_call.args,
                                    },
                                }
                            )

                if content:
                    output.append(
                        {
                            "role": "assistant",
                            "content": content,
                        }
                    )

            elif hasattr(candidate, "text") and candidate.text:
                output.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": candidate.text}],
                    }
                )

    elif hasattr(response, "text") and response.text:
        output.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": response.text}],
            }
        )

    return output


def extract_gemini_tools(kwargs: Dict[str, Any]) -> Optional[Any]:
    """
    Extract tool definitions from Gemini API kwargs.

    Args:
        kwargs: Keyword arguments passed to Gemini API

    Returns:
        Tool definitions if present, None otherwise
    """

    if "config" in kwargs and hasattr(kwargs["config"], "tools"):
        return kwargs["config"].tools

    return None


def format_gemini_input(contents: Any) -> List[FormattedMessage]:
    """
    Format Gemini input contents into standardized message format for PostHog tracking.

    This function handles various input formats:
    - String inputs
    - List of strings, dicts, or objects
    - Single dict or object
    - Gemini-specific format with parts array

    Args:
        contents: Input contents in various possible formats

    Returns:
        List of formatted messages with role and content fields
    """

    # Handle string input
    if isinstance(contents, str):
        return [{"role": "user", "content": contents}]

    # Handle list input
    if isinstance(contents, list):
        formatted: List[FormattedMessage] = []

        for item in contents:
            if isinstance(item, str):
                formatted.append({"role": "user", "content": item})

            elif isinstance(item, dict):
                formatted.append(_format_dict_message(item))

            else:
                formatted.append(_format_object_message(item))

        return formatted

    # Handle single dict input
    if isinstance(contents, dict):
        return [_format_dict_message(contents)]

    # Handle single object input
    return [_format_object_message(contents)]


def extract_gemini_usage_from_chunk(chunk: Any) -> StreamingUsageStats:
    """
    Extract usage statistics from a Gemini streaming chunk.

    Args:
        chunk: Streaming chunk from Gemini API

    Returns:
        Dictionary of usage statistics
    """

    usage: StreamingUsageStats = {}

    if not hasattr(chunk, "usage_metadata") or not chunk.usage_metadata:
        return usage

    # Gemini uses prompt_token_count and candidates_token_count
    usage["input_tokens"] = getattr(chunk.usage_metadata, "prompt_token_count", 0)
    usage["output_tokens"] = getattr(chunk.usage_metadata, "candidates_token_count", 0)

    # Calculate total if both values are defined (including 0)
    if "input_tokens" in usage and "output_tokens" in usage:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]

    return usage


def extract_gemini_content_from_chunk(chunk: Any) -> Optional[Dict[str, Any]]:
    """
    Extract content (text or function call) from a Gemini streaming chunk.

    Args:
        chunk: Streaming chunk from Gemini API

    Returns:
        Content block dictionary if present, None otherwise
    """

    # Check for text content
    if hasattr(chunk, "text") and chunk.text:
        return {"type": "text", "text": chunk.text}

    # Check for function calls in candidates
    if hasattr(chunk, "candidates") and chunk.candidates:
        for candidate in chunk.candidates:
            if hasattr(candidate, "content") and candidate.content:
                if hasattr(candidate.content, "parts") and candidate.content.parts:
                    for part in candidate.content.parts:
                        # Check for function_call part
                        if hasattr(part, "function_call") and part.function_call:
                            function_call = part.function_call
                            return {
                                "type": "function",
                                "function": {
                                    "name": function_call.name,
                                    "arguments": function_call.args,
                                },
                            }
                        # Also check for text in parts
                        elif hasattr(part, "text") and part.text:
                            return {"type": "text", "text": part.text}

    return None


def format_gemini_streaming_output(
    accumulated_content: Union[str, List[Any]],
) -> List[FormattedMessage]:
    """
    Format the final output from Gemini streaming.

    Args:
        accumulated_content: Accumulated content from streaming (string, list of strings, or list of content blocks)

    Returns:
        List of formatted messages
    """

    # Handle legacy string input (backward compatibility)
    if isinstance(accumulated_content, str):
        return [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": accumulated_content}],
            }
        ]

    # Handle list input
    if isinstance(accumulated_content, list):
        content: List[FormattedContentItem] = []
        text_parts = []

        for item in accumulated_content:
            if isinstance(item, str):
                # Legacy support: accumulate strings
                text_parts.append(item)
            elif isinstance(item, dict):
                # New format: content blocks
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "function":
                    # If we have accumulated text, add it first
                    if text_parts:
                        content.append(
                            {
                                "type": "text",
                                "text": "".join(text_parts),
                            }
                        )
                        text_parts = []

                    # Add the function call
                    content.append(
                        {
                            "type": "function",
                            "function": item.get("function", {}),
                        }
                    )

        # Add any remaining text
        if text_parts:
            content.append(
                {
                    "type": "text",
                    "text": "".join(text_parts),
                }
            )

        # If we have content, return it
        if content:
            return [{"role": "assistant", "content": content}]

    # Fallback for empty or unexpected input
    return [{"role": "assistant", "content": [{"type": "text", "text": ""}]}]


def standardize_gemini_usage(usage: Dict[str, Any]) -> TokenUsage:
    """
    Standardize Gemini usage statistics to common TokenUsage format.

    Gemini already uses standard field names (input_tokens/output_tokens).

    Args:
        usage: Raw usage statistics from Gemini

    Returns:
        Standardized TokenUsage dict
    """
    return TokenUsage(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        # Gemini doesn't currently support cache or reasoning tokens
    )
