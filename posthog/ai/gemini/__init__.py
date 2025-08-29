from .gemini import Client
from .gemini_converter import format_gemini_input, FormattedMessage


# Create a genai-like module for perfect drop-in replacement
class _GenAI:
    Client = Client


genai = _GenAI()

__all__ = ["Client", "genai", "format_gemini_input", "FormattedMessage"]
