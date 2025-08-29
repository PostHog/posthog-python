"""
Common type definitions for PostHog AI SDK.

These types are used for formatting messages and responses across different AI providers
(Anthropic, OpenAI, Gemini, etc.) to ensure consistency in tracking and data structure.
"""

from typing import Any, Dict, List, Optional, TypedDict, Union


class FormattedTextContent(TypedDict):
    """Formatted text content item."""
    type: str  # Literal["text"]
    text: str


class FormattedFunctionCall(TypedDict, total=False):
    """Formatted function/tool call content item."""
    type: str  # Literal["function"]
    id: Optional[str]
    function: Dict[str, Any]  # Contains 'name' and 'arguments'


class FormattedImageContent(TypedDict):
    """Formatted image content item."""
    type: str  # Literal["image"]
    image: str


# Union type for all formatted content items
FormattedContentItem = Union[
    FormattedTextContent,
    FormattedFunctionCall,
    FormattedImageContent,
    Dict[str, Any]  # Fallback for unknown content types
]


class FormattedMessage(TypedDict):
    """
    Standardized message format for PostHog tracking.
    
    Used across all providers to ensure consistent message structure
    when sending events to PostHog.
    """
    role: str
    content: Union[str, List[FormattedContentItem], Any]


class TokenUsage(TypedDict, total=False):
    """
    Token usage information for AI model responses.
    
    Different providers may populate different fields.
    """
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: Optional[int]
    cache_creation_input_tokens: Optional[int]
    reasoning_tokens: Optional[int]


class ProviderResponse(TypedDict, total=False):
    """
    Standardized provider response format.
    
    Used for consistent response formatting across all providers.
    """
    messages: List[FormattedMessage]
    usage: TokenUsage
    error: Optional[str]