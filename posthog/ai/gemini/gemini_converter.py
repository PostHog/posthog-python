"""
Gemini input format converter module.

This module handles the conversion of various Gemini input formats into a standardized
format for PostHog tracking. It eliminates code duplication between gemini.py and utils.py.
"""

from typing import Any, Dict, List, TypedDict, Union


class GeminiPart(TypedDict, total=False):
    """Represents a part in a Gemini message."""
    text: str


class GeminiMessage(TypedDict, total=False):
    """Represents a Gemini message with various possible fields."""
    role: str
    parts: List[Union[GeminiPart, Dict[str, Any]]]
    content: Union[str, List[Any]]
    text: str


class FormattedMessage(TypedDict):
    """Standardized message format for PostHog tracking."""
    role: str
    content: str


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
        formatted = []
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