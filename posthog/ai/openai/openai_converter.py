"""
OpenAI-specific conversion utilities.

This module handles the conversion of OpenAI API responses and inputs
into standardized formats for PostHog tracking. It supports both
Chat Completions API and Responses API formats.
"""

from typing import Any, Dict, List, Optional

from posthog.ai.types import (
    FormattedContentItem,
    FormattedFunctionCall,
    FormattedImageContent,
    FormattedMessage,
    FormattedTextContent,
)


def format_openai_response(response: Any) -> List[FormattedMessage]:
    """
    Format an OpenAI response into standardized message format.
    
    Handles both Chat Completions API and Responses API formats.
    
    Args:
        response: The response object from OpenAI API
        
    Returns:
        List of formatted messages with role and content
    """
    output = []
    
    if response is None:
        return output
    
    # Handle Chat Completions response format
    if hasattr(response, "choices"):
        content: List[FormattedContentItem] = []
        role = "assistant"
        
        for choice in response.choices:
            if hasattr(choice, "message") and choice.message:
                if choice.message.role:
                    role = choice.message.role
                
                if choice.message.content:
                    text_content: FormattedTextContent = {
                        "type": "text",
                        "text": choice.message.content
                    }
                    content.append(text_content)
                
                if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
                    for tool_call in choice.message.tool_calls:
                        function_call: FormattedFunctionCall = {
                            "type": "function",
                            "id": tool_call.id,
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            }
                        }
                        content.append(function_call)
        
        if content:
            message: FormattedMessage = {
                "role": role,
                "content": content,
            }
            output.append(message)
    
    # Handle Responses API format
    if hasattr(response, "output"):
        content = []
        role = "assistant"
        
        for item in response.output:
            if item.type == "message":
                role = item.role
                
                if hasattr(item, "content") and isinstance(item.content, list):
                    for content_item in item.content:
                        if (
                            hasattr(content_item, "type")
                            and content_item.type == "output_text"
                            and hasattr(content_item, "text")
                        ):
                            text_content: FormattedTextContent = {
                                "type": "text",
                                "text": content_item.text
                            }
                            content.append(text_content)
                        elif hasattr(content_item, "text"):
                            text_content = {
                                "type": "text",
                                "text": content_item.text
                            }
                            content.append(text_content)
                        elif (
                            hasattr(content_item, "type")
                            and content_item.type == "input_image"
                            and hasattr(content_item, "image_url")
                        ):
                            image_content: FormattedImageContent = {
                                "type": "image",
                                "image": content_item.image_url,
                            }
                            content.append(image_content)
                elif hasattr(item, "content"):
                    text_content = {
                        "type": "text",
                        "text": str(item.content)
                    }
                    content.append(text_content)
            
            elif hasattr(item, "type") and item.type == "function_call":
                function_call: FormattedFunctionCall = {
                    "type": "function",
                    "id": getattr(item, "call_id", getattr(item, "id", "")),
                    "function": {
                        "name": item.name,
                        "arguments": getattr(item, "arguments", {}),
                    }
                }
                content.append(function_call)
        
        if content:
            message: FormattedMessage = {
                "role": role,
                "content": content,
            }
            output.append(message)
    
    return output


def format_openai_input(messages: Optional[List[Dict[str, Any]]] = None, 
                        input_data: Optional[Any] = None) -> List[FormattedMessage]:
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
    
    # Handle Chat Completions API format
    if messages is not None:
        for msg in messages:
            formatted_msg: FormattedMessage = {
                "role": msg.get("role", "user"),
                "content": msg.get("content", "")
            }
            formatted_messages.append(formatted_msg)
    
    # Handle Responses API format
    if input_data is not None:
        if isinstance(input_data, list):
            for item in input_data:
                role = "user"
                content = ""
                
                if isinstance(item, dict):
                    role = item.get("role", "user")
                    content = item.get("content", "")
                elif isinstance(item, str):
                    content = item
                else:
                    content = str(item)
                
                formatted_msg: FormattedMessage = {
                    "role": role,
                    "content": content
                }
                formatted_messages.append(formatted_msg)
        elif isinstance(input_data, str):
            formatted_messages.append({
                "role": "user",
                "content": input_data
            })
        else:
            formatted_messages.append({
                "role": "user",
                "content": str(input_data)
            })
    
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
        return kwargs["tools"]
    
    # Check for functions parameter (older API)
    if "functions" in kwargs:
        return kwargs["functions"]
    
    return None


def format_openai_streaming_content(accumulated_content: str,
                                   tool_calls: Optional[List[Dict[str, Any]]] = None) -> List[FormattedContentItem]:
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
            "text": accumulated_content
        }
        formatted.append(text_content)
    
    # Add tool calls if present
    if tool_calls:
        for tool_call in tool_calls:
            function_call: FormattedFunctionCall = {
                "type": "function",
                "id": tool_call.get("id"),
                "function": tool_call.get("function", {})
            }
            formatted.append(function_call)
    
    return formatted