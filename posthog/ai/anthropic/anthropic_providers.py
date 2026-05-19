try:
    import anthropic
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Anthropic SDK to use this feature: 'pip install anthropic'"
    )

from typing import Optional

from posthog.ai.anthropic.anthropic import WrappedMessages
from posthog.ai.anthropic.anthropic_async import AsyncWrappedMessages
from posthog.client import Client as PostHogClient
from posthog import setup


class AnthropicBedrock(anthropic.AnthropicBedrock):
    """
    A wrapper around the Anthropic Bedrock SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    def __init__(self, posthog_client: Optional[PostHogClient] = None, **kwargs):
        """
        Args:
            posthog_client: If provided, events will be captured via this client
                instead of the global ``posthog`` client.
            **kwargs: Arguments passed to ``anthropic.AnthropicBedrock``.
        """
        super().__init__(**kwargs)
        self._ph_client = posthog_client or setup()
        self.messages = WrappedMessages(self)


class AsyncAnthropicBedrock(anthropic.AsyncAnthropicBedrock):
    """
    A wrapper around the Anthropic Bedrock SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    def __init__(self, posthog_client: Optional[PostHogClient] = None, **kwargs):
        """
        Args:
            posthog_client: If provided, events will be captured via this client
                instead of the global ``posthog`` client.
            **kwargs: Arguments passed to ``anthropic.AsyncAnthropicBedrock``.
        """
        super().__init__(**kwargs)
        self._ph_client = posthog_client or setup()
        self.messages = AsyncWrappedMessages(self)


class AnthropicVertex(anthropic.AnthropicVertex):
    """
    A wrapper around the Anthropic Vertex SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    def __init__(self, posthog_client: Optional[PostHogClient] = None, **kwargs):
        """
        Args:
            posthog_client: If provided, events will be captured via this client
                instead of the global ``posthog`` client.
            **kwargs: Arguments passed to ``anthropic.AnthropicVertex``.
        """
        super().__init__(**kwargs)
        self._ph_client = posthog_client or setup()
        self.messages = WrappedMessages(self)


class AsyncAnthropicVertex(anthropic.AsyncAnthropicVertex):
    """
    A wrapper around the Anthropic Vertex SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    def __init__(self, posthog_client: Optional[PostHogClient] = None, **kwargs):
        """
        Args:
            posthog_client: If provided, events will be captured via this client
                instead of the global ``posthog`` client.
            **kwargs: Arguments passed to ``anthropic.AsyncAnthropicVertex``.
        """
        super().__init__(**kwargs)
        self._ph_client = posthog_client or setup()
        self.messages = AsyncWrappedMessages(self)
