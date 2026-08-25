from typing import TYPE_CHECKING as _TYPE_CHECKING, Optional

try:
    import openai
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Open AI SDK to use this feature: 'pip install openai'"
    )

from .openai import (
    WrappedBeta as WrappedBeta,
    WrappedChat as WrappedChat,
    WrappedEmbeddings as WrappedEmbeddings,
    WrappedResponses as WrappedResponses,
    _SYNC_RESOURCE_WRAPPERS,
)
from .openai_async import (
    WrappedBeta as AsyncWrappedBeta,
    WrappedChat as AsyncWrappedChat,
    WrappedEmbeddings as AsyncWrappedEmbeddings,
    WrappedResponses as AsyncWrappedResponses,
    _ASYNC_RESOURCE_WRAPPERS,
)
from .wrapper_utils import _wrap_openai_resources

from posthog.client import Client as PostHogClient
from posthog import setup


class AzureOpenAI(openai.AzureOpenAI):
    """
    A wrapper around the Azure OpenAI SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    if _TYPE_CHECKING:
        chat: "WrappedChat"
        embeddings: "WrappedEmbeddings"
        beta: "WrappedBeta"
        responses: "WrappedResponses"

    def __init__(self, posthog_client: Optional[PostHogClient] = None, **kwargs):
        """
        Args:
            posthog_client: If provided, events will be captured via this client
                instead of the global ``posthog`` client.
            **kwargs: Arguments passed to ``openai.AzureOpenAI`` such as
                ``api_key``, ``azure_endpoint``, or ``api_version``.
        """
        super().__init__(**kwargs)
        self._ph_client = posthog_client or setup()

        _wrap_openai_resources(self, _SYNC_RESOURCE_WRAPPERS)


class AsyncAzureOpenAI(openai.AsyncAzureOpenAI):
    """
    An async wrapper around the Azure OpenAI SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    if _TYPE_CHECKING:
        chat: "AsyncWrappedChat"
        embeddings: "AsyncWrappedEmbeddings"
        beta: "AsyncWrappedBeta"
        responses: "AsyncWrappedResponses"

    def __init__(self, posthog_client: Optional[PostHogClient] = None, **kwargs):
        """
        Args:
            posthog_client: If provided, events will be captured via this client
                instead of the global ``posthog`` client.
            **kwargs: Arguments passed to ``openai.AsyncAzureOpenAI`` such as
                ``api_key``, ``azure_endpoint``, or ``api_version``.
        """
        super().__init__(**kwargs)
        self._ph_client = posthog_client or setup()

        _wrap_openai_resources(self, _ASYNC_RESOURCE_WRAPPERS)
