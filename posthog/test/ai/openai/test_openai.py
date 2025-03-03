import time
from unittest.mock import patch

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage
from openai.types.create_embedding_response import CreateEmbeddingResponse, Usage
from openai.types.embedding import Embedding

from posthog.ai.openai import OpenAI


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        yield mock_client


@pytest.fixture
def mock_openai_response():
    return ChatCompletion(
        id="test",
        model="gpt-4",
        object="chat.completion",
        created=int(time.time()),
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(
                    content="Test response",
                    role="assistant",
                ),
            )
        ],
        usage=CompletionUsage(
            completion_tokens=10,
            prompt_tokens=20,
            total_tokens=30,
        ),
    )


@pytest.fixture
def mock_embedding_response():
    return CreateEmbeddingResponse(
        data=[
            Embedding(
                embedding=[0.1, 0.2, 0.3],
                index=0,
                object="embedding",
            )
        ],
        model="text-embedding-3-small",
        object="list",
        usage=Usage(
            prompt_tokens=10,
            total_tokens=10,
        ),
    )


@pytest.fixture
def mock_openai_response_with_cached_tokens():
    return ChatCompletion(
        id="test",
        model="gpt-4",
        object="chat.completion",
        created=int(time.time()),
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(
                    content="Test response",
                    role="assistant",
                ),
            )
        ],
        usage=CompletionUsage(
            completion_tokens=10,
            prompt_tokens=20,
            total_tokens=30,
            prompt_tokens_details={"cached_tokens": 15},
        ),
    )


def test_basic_completion(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", return_value=mock_openai_response):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_openai_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4"
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "Test response"}]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_embeddings(mock_client, mock_embedding_response):
    with patch("openai.resources.embeddings.Embeddings.create", return_value=mock_embedding_response):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input="Hello world",
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_embedding_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_embedding"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "text-embedding-3-small"
        assert props["$ai_input"] == "Hello world"
        assert props["$ai_input_tokens"] == 10
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_groups(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", return_value=mock_openai_response):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_groups={"company": "test_company"},
        )

        assert response == mock_openai_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]

        assert call_args["groups"] == {"company": "test_company"}


def test_privacy_mode_local(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", return_value=mock_openai_response):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_privacy_mode=True,
        )

        assert response == mock_openai_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_input"] is None
        assert props["$ai_output_choices"] is None


def test_privacy_mode_global(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", return_value=mock_openai_response):
        mock_client.privacy_mode = True
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_privacy_mode=False,
        )

        assert response == mock_openai_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_input"] is None
        assert props["$ai_output_choices"] is None


def test_error(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", side_effect=Exception("Test error")):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        with pytest.raises(Exception):
            client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "Hello"}])

        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_is_error"] is True
        assert props["$ai_error"] == "Test error"


def test_cached_tokens(mock_client, mock_openai_response_with_cached_tokens):
    with patch("openai.resources.chat.completions.Completions.create", return_value=mock_openai_response_with_cached_tokens):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_openai_response_with_cached_tokens
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4"
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "Test response"}]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_cached_tokens"] == 15
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)