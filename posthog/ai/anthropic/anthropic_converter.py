"""
Anthropic-specific conversion utilities.

This module handles the conversion of Anthropic API responses and inputs
into standardized formats for PostHog tracking.
"""

from typing import Any, Dict, List, Optional

from posthog.ai.types import (
    FormattedContentItem,
    FormattedFunctionCall,
    FormattedMessage,
    FormattedTextContent,
)


def format_anthropic_response(response: Any) -> List[FormattedMessage]:
    """
    Format an Anthropic response into standardized message format.
    
    Args:
        response: The response object from Anthropic API
        
    Returns:
        List of formatted messages with role and content
    """
    output = []
    
    if response is None:
        return output
    
    content: List[FormattedContentItem] = []
    
    # Process content blocks from the response
    if hasattr(response, "content"):
        for choice in response.content:
            if (
                hasattr(choice, "type")
                and choice.type == "text"
                and hasattr(choice, "text")
                and choice.text
            ):
                text_content: FormattedTextContent = {
                    "type": "text",
                    "text": choice.text
                }
                content.append(text_content)
            elif (
                hasattr(choice, "type")
                and choice.type == "tool_use"
                and hasattr(choice, "name")
                and hasattr(choice, "id")
            ):
                function_call: FormattedFunctionCall = {
                    "type": "function",
                    "id": choice.id,
                    "function": {
                        "name": choice.name,
                        "arguments": getattr(choice, "input", {}),
                    }
                }
                content.append(function_call)
    
    if content:
        message: FormattedMessage = {
            "role": "assistant",
            "content": content,
        }
        output.append(message)
    
    return output


def format_anthropic_input(messages: List[Dict[str, Any]], system: Optional[str] = None) -> List[FormattedMessage]:
    """
    Format Anthropic input messages with optional system prompt.
    
    Args:
        messages: List of message dictionaries
        system: Optional system prompt to prepend
        
    Returns:
        List of formatted messages
    """
    formatted_messages: List[FormattedMessage] = []
    
    # Add system message if provided
    if system is not None:
        formatted_messages.append({
            "role": "system",
            "content": system
        })
    
    # Add user messages
    if messages:
        for msg in messages:
            # Messages are already in the correct format, just ensure type safety
            formatted_msg: FormattedMessage = {
                "role": msg.get("role", "user"),
                "content": msg.get("content", "")
            }
            formatted_messages.append(formatted_msg)
    
    return formatted_messages


def extract_anthropic_tools(kwargs: Dict[str, Any]) -> Optional[Any]:
    """
    Extract tool definitions from Anthropic API kwargs.
    
    Args:
        kwargs: Keyword arguments passed to Anthropic API
        
    Returns:
        Tool definitions if present, None otherwise
    """
    return kwargs.get("tools", None)


def format_anthropic_streaming_content(content_blocks: List[Dict[str, Any]]) -> List[FormattedContentItem]:
    """
    Format content blocks from Anthropic streaming response.
    
    Used by streaming handlers to format accumulated content blocks.
    
    Args:
        content_blocks: List of content block dictionaries from streaming
        
    Returns:
        List of formatted content items
    """
    formatted: List[FormattedContentItem] = []
    
    for block in content_blocks:
        if block.get("type") == "text":
            text_content: FormattedTextContent = {
                "type": "text",
                "text": block.get("text", "")
            }
            formatted.append(text_content)
        elif block.get("type") == "function":
            function_call: FormattedFunctionCall = {
                "type": "function",
                "id": block.get("id"),
                "function": block.get("function", {})
            }
            formatted.append(function_call)
    
    return formatted