import re
import unittest

from posthog.ai.sanitization import (
    redact_base64_data_url,
    sanitize_openai,
    sanitize_openai_response,
    sanitize_anthropic,
    sanitize_gemini,
    sanitize_langchain,
    is_base64_data_url,
    is_raw_base64,
    REDACTED_IMAGE_PLACEHOLDER,
)
from posthog.request import build_ai_multipart_request


class TestSanitization(unittest.TestCase):
    def setUp(self):
        self.sample_base64_image = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ..."
        self.sample_base64_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA..."
        self.regular_url = "https://example.com/image.jpg"
        self.raw_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUl=="

    def test_is_base64_data_url(self):
        self.assertTrue(is_base64_data_url(self.sample_base64_image))
        self.assertTrue(is_base64_data_url(self.sample_base64_png))
        self.assertFalse(is_base64_data_url(self.regular_url))
        self.assertFalse(is_base64_data_url("regular text"))

    def test_is_raw_base64(self):
        self.assertTrue(is_raw_base64(self.raw_base64))
        self.assertFalse(is_raw_base64("short"))
        self.assertFalse(is_raw_base64(self.regular_url))
        self.assertFalse(is_raw_base64("/path/to/file"))

    def test_redact_base64_data_url(self):
        self.assertEqual(
            redact_base64_data_url(self.sample_base64_image), REDACTED_IMAGE_PLACEHOLDER
        )
        self.assertEqual(
            redact_base64_data_url(self.sample_base64_png), REDACTED_IMAGE_PLACEHOLDER
        )
        self.assertEqual(redact_base64_data_url(self.regular_url), self.regular_url)
        self.assertEqual(redact_base64_data_url(None), None)
        self.assertEqual(redact_base64_data_url(123), 123)

    def test_sanitize_openai(self):
        input_data = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": self.sample_base64_image,
                            "detail": "high",
                        },
                    },
                ],
            }
        ]

        result = sanitize_openai(input_data)

        self.assertEqual(result[0]["content"][0]["text"], "What is in this image?")
        self.assertEqual(
            result[0]["content"][1]["image_url"]["url"], REDACTED_IMAGE_PLACEHOLDER
        )
        self.assertEqual(result[0]["content"][1]["image_url"]["detail"], "high")

    def test_sanitize_openai_preserves_regular_urls(self):
        input_data = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": self.regular_url},
                    }
                ],
            }
        ]

        result = sanitize_openai(input_data)
        self.assertEqual(result[0]["content"][0]["image_url"]["url"], self.regular_url)

    def test_sanitize_openai_response(self):
        input_data = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": self.sample_base64_image,
                    }
                ],
            }
        ]

        result = sanitize_openai_response(input_data)
        self.assertEqual(
            result[0]["content"][0]["image_url"], REDACTED_IMAGE_PLACEHOLDER
        )

    def test_sanitize_anthropic(self):
        input_data = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "base64data",
                        },
                    },
                ],
            }
        ]

        result = sanitize_anthropic(input_data)

        self.assertEqual(result[0]["content"][0]["text"], "What is in this image?")
        self.assertEqual(
            result[0]["content"][1]["source"]["data"], REDACTED_IMAGE_PLACEHOLDER
        )
        self.assertEqual(result[0]["content"][1]["source"]["type"], "base64")
        self.assertEqual(result[0]["content"][1]["source"]["media_type"], "image/jpeg")

    def test_sanitize_gemini(self):
        input_data = [
            {
                "parts": [
                    {"text": "What is in this image?"},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": "base64data",
                        }
                    },
                ]
            }
        ]

        result = sanitize_gemini(input_data)

        self.assertEqual(result[0]["parts"][0]["text"], "What is in this image?")
        self.assertEqual(
            result[0]["parts"][1]["inline_data"]["data"], REDACTED_IMAGE_PLACEHOLDER
        )
        self.assertEqual(
            result[0]["parts"][1]["inline_data"]["mime_type"], "image/jpeg"
        )

    def test_sanitize_langchain_openai_style(self):
        input_data = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": self.sample_base64_image},
                    }
                ],
            }
        ]

        result = sanitize_langchain(input_data)
        self.assertEqual(
            result[0]["content"][0]["image_url"]["url"], REDACTED_IMAGE_PLACEHOLDER
        )

    def test_sanitize_langchain_anthropic_style(self):
        input_data = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"data": "base64data"},
                    }
                ],
            }
        ]

        result = sanitize_langchain(input_data)
        self.assertEqual(
            result[0]["content"][0]["source"]["data"], REDACTED_IMAGE_PLACEHOLDER
        )

    def test_sanitize_with_data_url_format(self):
        # Test that data URLs are properly detected and redacted across providers
        data_url = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD"

        # OpenAI format
        openai_data = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": data_url}}],
            }
        ]
        result = sanitize_openai(openai_data)
        self.assertEqual(
            result[0]["content"][0]["image_url"]["url"], REDACTED_IMAGE_PLACEHOLDER
        )

        # Anthropic format
        anthropic_data = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": data_url,
                        },
                    }
                ],
            }
        ]
        result = sanitize_anthropic(anthropic_data)
        self.assertEqual(
            result[0]["content"][0]["source"]["data"], REDACTED_IMAGE_PLACEHOLDER
        )

        # LangChain format
        langchain_data = [
            {"role": "user", "content": [{"type": "image", "data": data_url}]}
        ]
        result = sanitize_langchain(langchain_data)
        self.assertEqual(result[0]["content"][0]["data"], REDACTED_IMAGE_PLACEHOLDER)

    def test_sanitize_with_raw_base64(self):
        # Test that raw base64 strings (without data URL prefix) are detected
        raw_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUl=="

        # Test with Anthropic format
        anthropic_data = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": raw_base64,
                        },
                    }
                ],
            }
        ]
        result = sanitize_anthropic(anthropic_data)
        self.assertEqual(
            result[0]["content"][0]["source"]["data"], REDACTED_IMAGE_PLACEHOLDER
        )

        # Test with Gemini format
        gemini_data = [
            {"parts": [{"inline_data": {"mime_type": "image/png", "data": raw_base64}}]}
        ]
        result = sanitize_gemini(gemini_data)
        self.assertEqual(
            result[0]["parts"][0]["inline_data"]["data"], REDACTED_IMAGE_PLACEHOLDER
        )

    def test_sanitize_preserves_regular_content(self):
        # Ensure non-base64 content is preserved across all providers
        regular_url = "https://example.com/image.jpg"
        text_content = "What do you see?"

        # OpenAI
        openai_data = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text_content},
                    {"type": "image_url", "image_url": {"url": regular_url}},
                ],
            }
        ]
        result = sanitize_openai(openai_data)
        self.assertEqual(result[0]["content"][0]["text"], text_content)
        self.assertEqual(result[0]["content"][1]["image_url"]["url"], regular_url)

        # Anthropic
        anthropic_data = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text_content},
                    {"type": "image", "source": {"type": "url", "url": regular_url}},
                ],
            }
        ]
        result = sanitize_anthropic(anthropic_data)
        self.assertEqual(result[0]["content"][0]["text"], text_content)
        # URL-based images should remain unchanged
        self.assertEqual(result[0]["content"][1]["source"]["url"], regular_url)

    def test_sanitize_handles_non_dict_content(self):
        input_data = [{"role": "user", "content": "Just text"}]

        result = sanitize_openai(input_data)
        self.assertEqual(result, input_data)

    def test_sanitize_handles_none_input(self):
        self.assertIsNone(sanitize_openai(None))
        self.assertIsNone(sanitize_anthropic(None))
        self.assertIsNone(sanitize_gemini(None))
        self.assertIsNone(sanitize_langchain(None))

    def test_sanitize_handles_single_message(self):
        input_data = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": self.sample_base64_image},
                }
            ],
        }

        result = sanitize_openai(input_data)
        self.assertEqual(
            result["content"][0]["image_url"]["url"], REDACTED_IMAGE_PLACEHOLDER
        )


class TestAIMultipartRequest(unittest.TestCase):
    """Tests for building AI multipart requests."""

    def test_build_basic_multipart_request(self):
        """Test building a basic multipart request with one blob."""
        event_name = "$ai_generation"
        distinct_id = "test_user"
        properties = {"$ai_model": "gpt-4", "$ai_provider": "openai"}
        blobs = {"$ai_input": [{"role": "user", "content": "test message"}]}
        timestamp = "2024-01-15T10:30:00Z"
        event_uuid = "test-uuid-123"

        body, boundary = build_ai_multipart_request(
            event_name=event_name,
            distinct_id=distinct_id,
            properties=properties,
            blobs=blobs,
            timestamp=timestamp,
            event_uuid=event_uuid,
        )

        # Verify body is bytes
        assert isinstance(body, bytes)
        assert isinstance(boundary, str)

        # Decode body for inspection
        body_str = body.decode("utf-8")

        # Verify boundary format
        assert boundary.startswith("----WebKitFormBoundary")
        assert (
            len(boundary) == 54
        )  # "----WebKitFormBoundary" (22 chars) + token_hex(16) (32 chars)

        # Verify all parts are present
        assert f"--{boundary}" in body_str
        assert 'name="event"' in body_str
        assert 'name="event.properties"' in body_str
        assert 'name="event.properties.$ai_input"' in body_str

        # Verify event part contains expected data
        assert '"event": "$ai_generation"' in body_str
        assert '"distinct_id": "test_user"' in body_str
        assert '"uuid": "test-uuid-123"' in body_str
        assert '"timestamp": "2024-01-15T10:30:00Z"' in body_str

        # Verify properties part
        assert '"$ai_model": "gpt-4"' in body_str
        assert '"$ai_provider": "openai"' in body_str

        # Verify blob part
        assert '"role": "user"' in body_str
        assert '"content": "test message"' in body_str

        # Verify final boundary
        assert f"--{boundary}--" in body_str

    def test_build_multipart_with_multiple_blobs(self):
        """Test building a multipart request with multiple blobs."""
        event_name = "$ai_generation"
        distinct_id = "test_user"
        properties = {"$ai_model": "gpt-4"}
        blobs = {
            "$ai_input": [{"role": "user", "content": "input"}],
            "$ai_output_choices": [{"role": "assistant", "content": "output"}],
        }

        body, boundary = build_ai_multipart_request(
            event_name=event_name,
            distinct_id=distinct_id,
            properties=properties,
            blobs=blobs,
        )

        body_str = body.decode("utf-8")

        # Verify both blob parts are present
        assert 'name="event.properties.$ai_input"' in body_str
        assert 'name="event.properties.$ai_output_choices"' in body_str
        assert '"content": "input"' in body_str
        assert '"content": "output"' in body_str

    def test_build_multipart_no_blobs(self):
        """Test building a multipart request with no blobs."""
        event_name = "$ai_generation"
        distinct_id = "test_user"
        properties = {"$ai_model": "gpt-4"}
        blobs = {}

        body, boundary = build_ai_multipart_request(
            event_name=event_name,
            distinct_id=distinct_id,
            properties=properties,
            blobs=blobs,
        )

        body_str = body.decode("utf-8")

        # Should still have event and properties parts
        assert 'name="event"' in body_str
        assert 'name="event.properties"' in body_str

        # Should not have any blob parts
        assert 'name="event.properties.$ai_input"' not in body_str
        assert 'name="event.properties.$ai_output_choices"' not in body_str

    def test_build_multipart_auto_generates_uuid(self):
        """Test that UUID is auto-generated if not provided."""
        event_name = "$ai_generation"
        distinct_id = "test_user"
        properties = {}
        blobs = {}

        body, boundary = build_ai_multipart_request(
            event_name=event_name,
            distinct_id=distinct_id,
            properties=properties,
            blobs=blobs,
            event_uuid=None,  # Don't provide UUID
        )

        body_str = body.decode("utf-8")

        # Should have a UUID in the event part
        assert '"uuid":' in body_str

        # Extract and verify it's a valid UUID format (basic check)
        uuid_pattern = r'"uuid":\s*"([0-9a-f-]+)"'
        match = re.search(uuid_pattern, body_str)
        assert match is not None
        uuid_str = match.group(1)
        assert len(uuid_str) == 36  # Standard UUID string length

    def test_build_multipart_without_timestamp(self):
        """Test building request without timestamp."""
        event_name = "$ai_generation"
        distinct_id = "test_user"
        properties = {}
        blobs = {}

        body, boundary = build_ai_multipart_request(
            event_name=event_name,
            distinct_id=distinct_id,
            properties=properties,
            blobs=blobs,
            timestamp=None,
        )

        body_str = body.decode("utf-8")

        # Should not have timestamp in event part
        assert '"timestamp"' not in body_str

    def test_build_multipart_content_types(self):
        """Test that all parts have correct Content-Type headers."""
        event_name = "$ai_generation"
        distinct_id = "test_user"
        properties = {"$ai_model": "gpt-4"}
        blobs = {"$ai_input": [{"role": "user", "content": "test"}]}

        body, boundary = build_ai_multipart_request(
            event_name=event_name,
            distinct_id=distinct_id,
            properties=properties,
            blobs=blobs,
        )

        body_str = body.decode("utf-8")

        # All parts should have application/json Content-Type
        parts = body_str.split(f"--{boundary}")
        for part in parts:
            if 'name="' in part:  # Skip empty parts
                assert "Content-Type: application/json" in part

    def test_build_multipart_complex_nested_data(self):
        """Test with complex nested JSON structures in blobs."""
        event_name = "$ai_generation"
        distinct_id = "test_user"
        properties = {"$ai_model": "gpt-4"}
        blobs = {
            "$ai_input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What's in this image?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/image.jpg"},
                        },
                    ],
                }
            ]
        }

        body, boundary = build_ai_multipart_request(
            event_name=event_name,
            distinct_id=distinct_id,
            properties=properties,
            blobs=blobs,
        )

        body_str = body.decode("utf-8")

        # Verify nested structure is properly encoded
        assert '"type": "text"' in body_str
        assert '"type": "image_url"' in body_str
        assert "https://example.com/image.jpg" in body_str


if __name__ == "__main__":
    unittest.main()
