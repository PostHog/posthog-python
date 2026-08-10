"""Fixtures shared by the OpenAI test modules.

Defined here rather than in a test module so pytest supplies them by discovery. Importing them
between test files bound the names in the importing module and tripped Ruff F811.
"""

from unittest.mock import patch

import pytest
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_chunk import (
    Choice as ChoiceChunk,
)
from openai.types.completion_usage import CompletionUsage


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        yield mock_client


@pytest.fixture
def streaming_tool_call_chunks():
    return [
        ChatCompletionChunk(
            id="chunk1",
            model="gpt-4",
            object="chat.completion.chunk",
            created=1234567890,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(
                        role="assistant",
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id="call_abc123",
                                type="function",
                                function=ChoiceDeltaToolCallFunction(
                                    name="get_weather",
                                    arguments='{"location": "',
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
        ),
        ChatCompletionChunk(
            id="chunk2",
            model="gpt-4",
            object="chat.completion.chunk",
            created=1234567891,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id="call_abc123",
                                type="function",
                                function=ChoiceDeltaToolCallFunction(
                                    arguments='San Francisco"',
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
        ),
        ChatCompletionChunk(
            id="chunk3",
            model="gpt-4",
            object="chat.completion.chunk",
            created=1234567892,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id="call_abc123",
                                type="function",
                                function=ChoiceDeltaToolCallFunction(
                                    arguments=', "unit": "celsius"}',
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
        ),
        ChatCompletionChunk(
            id="chunk4",
            model="gpt-4",
            object="chat.completion.chunk",
            created=1234567893,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(
                        content="The weather in San Francisco is 15°C.",
                    ),
                    finish_reason=None,
                )
            ],
            usage=CompletionUsage(
                prompt_tokens=20,
                completion_tokens=15,
                total_tokens=35,
            ),
        ),
    ]
