from unittest.mock import MagicMock

import pytest
from openai.resources.beta import AsyncBeta, Beta
from openai.resources.chat import AsyncChat, Chat
from openai.resources.embeddings import AsyncEmbeddings, Embeddings
from openai.resources.responses import AsyncResponses, Responses

from posthog.ai.openai import AsyncAzureOpenAI, AsyncOpenAI, AzureOpenAI, OpenAI
from posthog.ai.openai.openai import (
    WrappedBeta,
    WrappedChat,
    WrappedEmbeddings,
    WrappedResponses,
)
from posthog.ai.openai.openai_async import (
    WrappedBeta as AsyncWrappedBeta,
)
from posthog.ai.openai.openai_async import (
    WrappedChat as AsyncWrappedChat,
)
from posthog.ai.openai.openai_async import (
    WrappedEmbeddings as AsyncWrappedEmbeddings,
)
from posthog.ai.openai.openai_async import (
    WrappedResponses as AsyncWrappedResponses,
)

_SYNC_WRAPPERS = {
    "chat": WrappedChat,
    "embeddings": WrappedEmbeddings,
    "beta": WrappedBeta,
    "responses": WrappedResponses,
}
_ASYNC_WRAPPERS = {
    "chat": AsyncWrappedChat,
    "embeddings": AsyncWrappedEmbeddings,
    "beta": AsyncWrappedBeta,
    "responses": AsyncWrappedResponses,
}
_SYNC_RESOURCES = {
    "chat": Chat,
    "embeddings": Embeddings,
    "beta": Beta,
    "responses": Responses,
}
_ASYNC_RESOURCES = {
    "chat": AsyncChat,
    "embeddings": AsyncEmbeddings,
    "beta": AsyncBeta,
    "responses": AsyncResponses,
}
_AZURE_KWARGS = {
    "api_key": "test-key",
    "azure_endpoint": "https://example.openai.azure.com",
    "api_version": "2024-02-01",
}


@pytest.mark.parametrize(
    "client_type, client_kwargs, wrappers, resource_types",
    [
        (OpenAI, {"api_key": "test-key"}, _SYNC_WRAPPERS, _SYNC_RESOURCES),
        (AsyncOpenAI, {"api_key": "test-key"}, _ASYNC_WRAPPERS, _ASYNC_RESOURCES),
        (AzureOpenAI, _AZURE_KWARGS, _SYNC_WRAPPERS, _SYNC_RESOURCES),
        (AsyncAzureOpenAI, _AZURE_KWARGS, _ASYNC_WRAPPERS, _ASYNC_RESOURCES),
    ],
)
def test_client_resources_are_discovered_and_wrapped(
    client_type, client_kwargs, wrappers, resource_types
):
    client = client_type(posthog_client=MagicMock(), **client_kwargs)

    for resource_name, wrapper_type in wrappers.items():
        wrapped = getattr(client, resource_name)
        original = getattr(client, f"_original_{resource_name}")

        assert type(wrapped) is wrapper_type
        assert type(original) is resource_types[resource_name]
        assert wrapped._client is client
        assert wrapped._original is original
