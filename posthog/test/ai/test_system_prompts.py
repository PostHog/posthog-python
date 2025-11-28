"""
Tests for system prompt capture across all LLM providers.

This test suite ensures that system prompts are correctly captured in analytics
regardless of how they're passed to the providers:
- As first message in messages/contents array (standard format)
- As separate system parameter (Anthropic, OpenAI)
- As instructions parameter (OpenAI Responses API)
- As system_instruction parameter (Gemini)
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY


class TestSystemPromptCapture(unittest.TestCase):
    """Test system prompt capture for all providers."""

    def setUp(self):
        super().setUp()
        self.test_system_prompt = "You are a helpful AI assistant."
        self.test_user_message = "Hello, how are you?"
        self.test_response = "I'm doing well, thank you!"

        # Create mock PostHog client
        self.client = Client(FAKE_TEST_API_KEY)
        self.client._enqueue = MagicMock()
        self.client.privacy_mode = False

    def _assert_system_prompt_captured(self, captured_input):
        """Helper to assert system prompt is correctly captured."""
        self.assertEqual(
            len(captured_input), 2, "Should have 2 messages (system + user)"
        )
        self.assertEqual(
            captured_input[0]["role"], "system", "First message should be system"
        )
        self.assertEqual(
            captured_input[0]["content"],
            self.test_system_prompt,
            "System content should match",
        )
        self.assertEqual(
            captured_input[1]["role"], "user", "Second message should be user"
        )
        self.assertEqual(
            captured_input[1]["content"],
            self.test_user_message,
            "User content should match",
        )

    # OpenAI Tests
    def test_openai_messages_array_system_prompt(self):
        """Test OpenAI with system prompt in messages array."""
        try:
            from openai.types.chat import ChatCompletion, ChatCompletionMessage
            from openai.types.chat.chat_completion import Choice
            from openai.types.completion_usage import CompletionUsage

            from posthog.ai.openai import OpenAI
        except ImportError:
            self.skipTest("OpenAI package not available")

        mock_response = ChatCompletion(
            id="test",
            model="gpt-4",
            object="chat.completion",
            created=int(time.time()),
            choices=[
                Choice(
                    finish_reason="stop",
                    index=0,
                    message=ChatCompletionMessage(
                        content=self.test_response, role="assistant"
                    ),
                )
            ],
            usage=CompletionUsage(
                completion_tokens=10, prompt_tokens=20, total_tokens=30
            ),
        )

        with patch(
            "openai.resources.chat.completions.Completions.create",
            return_value=mock_response,
        ):
            client = OpenAI(posthog_client=self.client, api_key="test")

            messages = [
                {"role": "system", "content": self.test_system_prompt},
                {"role": "user", "content": self.test_user_message},
            ]

            client.chat.completions.create(
                model="gpt-4", messages=messages, posthog_distinct_id="test-user"
            )

            self.assertEqual(len(self.client._enqueue.call_args_list), 1)
            properties = self.client._enqueue.call_args_list[0][0][0]["properties"]
            self._assert_system_prompt_captured(properties["$ai_input"])

    def test_openai_separate_system_parameter(self):
        """Test OpenAI with system prompt as separate parameter."""
        try:
            from openai.types.chat import ChatCompletion, ChatCompletionMessage
            from openai.types.chat.chat_completion import Choice
            from openai.types.completion_usage import CompletionUsage

            from posthog.ai.openai import OpenAI
        except ImportError:
            self.skipTest("OpenAI package not available")

        mock_response = ChatCompletion(
            id="test",
            model="gpt-4",
            object="chat.completion",
            created=int(time.time()),
            choices=[
                Choice(
                    finish_reason="stop",
                    index=0,
                    message=ChatCompletionMessage(
                        content=self.test_response, role="assistant"
                    ),
                )
            ],
            usage=CompletionUsage(
                completion_tokens=10, prompt_tokens=20, total_tokens=30
            ),
        )

        with patch(
            "openai.resources.chat.completions.Completions.create",
            return_value=mock_response,
        ):
            client = OpenAI(posthog_client=self.client, api_key="test")

            messages = [{"role": "user", "content": self.test_user_message}]

            client.chat.completions.create(
                model="gpt-4",
                messages=messages,
                system=self.test_system_prompt,
                posthog_distinct_id="test-user",
            )

            self.assertEqual(len(self.client._enqueue.call_args_list), 1)
            properties = self.client._enqueue.call_args_list[0][0][0]["properties"]
            self._assert_system_prompt_captured(properties["$ai_input"])

    def test_openai_streaming_system_parameter(self):
        """Test OpenAI streaming with system parameter."""
        try:
            from openai.types.chat.chat_completion_chunk import (
                ChatCompletionChunk,
                ChoiceDelta,
            )
            from openai.types.chat.chat_completion_chunk import Choice as ChoiceChunk
            from openai.types.completion_usage import CompletionUsage

            from posthog.ai.openai import OpenAI
        except ImportError:
            self.skipTest("OpenAI package not available")

        chunk1 = ChatCompletionChunk(
            id="test",
            model="gpt-4",
            object="chat.completion.chunk",
            created=int(time.time()),
            choices=[
                ChoiceChunk(
                    finish_reason=None,
                    index=0,
                    delta=ChoiceDelta(content="Hello", role="assistant"),
                )
            ],
        )

        chunk2 = ChatCompletionChunk(
            id="test",
            model="gpt-4",
            object="chat.completion.chunk",
            created=int(time.time()),
            choices=[
                ChoiceChunk(
                    finish_reason="stop",
                    index=0,
                    delta=ChoiceDelta(content=" there!", role=None),
                )
            ],
            usage=CompletionUsage(
                completion_tokens=10, prompt_tokens=20, total_tokens=30
            ),
        )

        with patch(
            "openai.resources.chat.completions.Completions.create",
            return_value=[chunk1, chunk2],
        ):
            client = OpenAI(posthog_client=self.client, api_key="test")

            messages = [{"role": "user", "content": self.test_user_message}]

            response_generator = client.chat.completions.create(
                model="gpt-4",
                messages=messages,
                system=self.test_system_prompt,
                stream=True,
                posthog_distinct_id="test-user",
            )

            list(response_generator)  # Consume generator

            self.assertEqual(len(self.client._enqueue.call_args_list), 1)
            properties = self.client._enqueue.call_args_list[0][0][0]["properties"]
            self._assert_system_prompt_captured(properties["$ai_input"])

    # Anthropic Tests
    def test_anthropic_messages_array_system_prompt(self):
        """Test Anthropic with system prompt in messages array."""
        try:
            from posthog.ai.anthropic import Anthropic
        except ImportError:
            self.skipTest("Anthropic package not available")

        with patch("anthropic.resources.messages.Messages.create") as mock_create:
            mock_response = MagicMock()
            mock_response.usage.input_tokens = 20
            mock_response.usage.output_tokens = 10
            mock_response.usage.cache_read_input_tokens = None
            mock_response.usage.cache_creation_input_tokens = None
            mock_create.return_value = mock_response

            client = Anthropic(posthog_client=self.client, api_key="test")

            messages = [
                {"role": "system", "content": self.test_system_prompt},
                {"role": "user", "content": self.test_user_message},
            ]

            client.messages.create(
                model="claude-3-5-sonnet-20241022",
                messages=messages,
                posthog_distinct_id="test-user",
            )

            self.assertEqual(len(self.client._enqueue.call_args_list), 1)
            properties = self.client._enqueue.call_args_list[0][0][0]["properties"]
            self._assert_system_prompt_captured(properties["$ai_input"])

    def test_anthropic_separate_system_parameter(self):
        """Test Anthropic with system prompt as separate parameter."""
        try:
            from posthog.ai.anthropic import Anthropic
        except ImportError:
            self.skipTest("Anthropic package not available")

        with patch("anthropic.resources.messages.Messages.create") as mock_create:
            mock_response = MagicMock()
            mock_response.usage.input_tokens = 20
            mock_response.usage.output_tokens = 10
            mock_response.usage.cache_read_input_tokens = None
            mock_response.usage.cache_creation_input_tokens = None
            mock_create.return_value = mock_response

            client = Anthropic(posthog_client=self.client, api_key="test")

            messages = [{"role": "user", "content": self.test_user_message}]

            client.messages.create(
                model="claude-3-5-sonnet-20241022",
                messages=messages,
                system=self.test_system_prompt,
                posthog_distinct_id="test-user",
            )

            self.assertEqual(len(self.client._enqueue.call_args_list), 1)
            properties = self.client._enqueue.call_args_list[0][0][0]["properties"]
            self._assert_system_prompt_captured(properties["$ai_input"])

    # Gemini Tests
    def test_gemini_contents_array_system_prompt(self):
        """Test Gemini with system prompt in contents array."""
        try:
            from posthog.ai.gemini import Client
        except ImportError:
            self.skipTest("Gemini package not available")

        with patch("google.genai.Client") as mock_genai_class:
            mock_response = MagicMock()
            mock_response.candidates = [MagicMock()]
            mock_response.candidates[0].content.parts = [MagicMock()]
            mock_response.candidates[0].content.parts[0].text = self.test_response
            mock_response.usage_metadata.prompt_token_count = 20
            mock_response.usage_metadata.candidates_token_count = 10
            mock_response.usage_metadata.cached_content_token_count = None
            mock_response.usage_metadata.thoughts_token_count = None

            mock_client_instance = MagicMock()
            mock_models_instance = MagicMock()
            mock_models_instance.generate_content.return_value = mock_response
            mock_client_instance.models = mock_models_instance
            mock_genai_class.return_value = mock_client_instance

            client = Client(posthog_client=self.client, api_key="test")

            contents = [
                {"role": "system", "content": self.test_system_prompt},
                {"role": "user", "content": self.test_user_message},
            ]

            client.models.generate_content(
                model="gemini-2.0-flash",
                contents=contents,
                posthog_distinct_id="test-user",
            )

            self.assertEqual(len(self.client._enqueue.call_args_list), 1)
            properties = self.client._enqueue.call_args_list[0][0][0]["properties"]
            self._assert_system_prompt_captured(properties["$ai_input"])

    def test_gemini_system_instruction_parameter(self):
        """Test Gemini with system_instruction in config parameter."""
        try:
            from posthog.ai.gemini import Client
        except ImportError:
            self.skipTest("Gemini package not available")

        with patch("google.genai.Client") as mock_genai_class:
            mock_response = MagicMock()
            mock_response.candidates = [MagicMock()]
            mock_response.candidates[0].content.parts = [MagicMock()]
            mock_response.candidates[0].content.parts[0].text = self.test_response
            mock_response.usage_metadata.prompt_token_count = 20
            mock_response.usage_metadata.candidates_token_count = 10
            mock_response.usage_metadata.cached_content_token_count = None
            mock_response.usage_metadata.thoughts_token_count = None

            mock_client_instance = MagicMock()
            mock_models_instance = MagicMock()
            mock_models_instance.generate_content.return_value = mock_response
            mock_client_instance.models = mock_models_instance
            mock_genai_class.return_value = mock_client_instance

            client = Client(posthog_client=self.client, api_key="test")

            contents = [{"role": "user", "content": self.test_user_message}]
            config = {"system_instruction": self.test_system_prompt}

            client.models.generate_content(
                model="gemini-2.0-flash",
                contents=contents,
                config=config,
                posthog_distinct_id="test-user",
            )

            self.assertEqual(len(self.client._enqueue.call_args_list), 1)
            properties = self.client._enqueue.call_args_list[0][0][0]["properties"]
            self._assert_system_prompt_captured(properties["$ai_input"])
