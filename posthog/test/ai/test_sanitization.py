import os
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
    """Test that _INTERNAL_LLMA_MULTIMODAL environment variable controls sanitization."""

    def tearDown(self):
        # Clean up environment variable after each test
        if "_INTERNAL_LLMA_MULTIMODAL" in os.environ:
            del os.environ["_INTERNAL_LLMA_MULTIMODAL"]

    def test_multimodal_disabled_redacts_images(self):
        """When _INTERNAL_LLMA_MULTIMODAL is not set, images should be redacted."""
        if "_INTERNAL_LLMA_MULTIMODAL" in os.environ:
            del os.environ["_INTERNAL_LLMA_MULTIMODAL"]

        base64_image = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ..."
        result = redact_base64_data_url(base64_image)
        self.assertEqual(result, REDACTED_IMAGE_PLACEHOLDER)

    def test_multimodal_enabled_preserves_images(self):
        """When _INTERNAL_LLMA_MULTIMODAL is true, images should be preserved."""
        os.environ["_INTERNAL_LLMA_MULTIMODAL"] = "true"

        base64_image = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ..."
        result = redact_base64_data_url(base64_image)
        self.assertEqual(result, base64_image)

    def test_multimodal_enabled_with_1(self):
        """_INTERNAL_LLMA_MULTIMODAL=1 should enable multimodal."""
        os.environ["_INTERNAL_LLMA_MULTIMODAL"] = "1"

        base64_image = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ..."
        result = redact_base64_data_url(base64_image)
        self.assertEqual(result, base64_image)

    def test_multimodal_enabled_with_yes(self):
        """_INTERNAL_LLMA_MULTIMODAL=yes should enable multimodal."""
        os.environ["_INTERNAL_LLMA_MULTIMODAL"] = "yes"

        base64_image = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ..."
        result = redact_base64_data_url(base64_image)
        self.assertEqual(result, base64_image)

    def test_multimodal_false_redacts_images(self):
        """_INTERNAL_LLMA_MULTIMODAL=false should still redact."""
        os.environ["_INTERNAL_LLMA_MULTIMODAL"] = "false"

        base64_image = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ..."
        result = redact_base64_data_url(base64_image)
        self.assertEqual(result, REDACTED_IMAGE_PLACEHOLDER)

    def test_anthropic_multimodal_enabled(self):
        """Anthropic images should be preserved when _INTERNAL_LLMA_MULTIMODAL is enabled."""
        os.environ["_INTERNAL_LLMA_MULTIMODAL"] = "true"

        input_data = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "base64data",
                        },
                    }
                ],
            }
        ]

        result = sanitize_anthropic(input_data)
        self.assertEqual(result[0]["content"][0]["source"]["data"], "base64data")

    def test_gemini_multimodal_enabled(self):
        """Gemini images should be preserved when _INTERNAL_LLMA_MULTIMODAL is enabled."""
        os.environ["_INTERNAL_LLMA_MULTIMODAL"] = "true"

        input_data = [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": "base64data"}}
                ]
            }
        ]

        result = sanitize_gemini(input_data)
        self.assertEqual(result[0]["parts"][0]["inline_data"]["data"], "base64data")

    def test_langchain_anthropic_style_multimodal_enabled(self):
        """LangChain Anthropic-style images should be preserved when _INTERNAL_LLMA_MULTIMODAL is enabled."""
        os.environ["_INTERNAL_LLMA_MULTIMODAL"] = "true"

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
        self.assertEqual(result[0]["content"][0]["source"]["data"], "base64data")

    def test_openai_audio_redacted_by_default(self):
        """OpenAI audio should be redacted when _INTERNAL_LLMA_MULTIMODAL is not set."""
        if "_INTERNAL_LLMA_MULTIMODAL" in os.environ:
            del os.environ["_INTERNAL_LLMA_MULTIMODAL"]

        input_data = [
            {
                "role": "assistant",
                "content": [
                    {"type": "audio", "data": "base64audiodata", "id": "audio_123"}
                ],
            }
        ]

        result = sanitize_openai(input_data)
        self.assertEqual(result[0]["content"][0]["data"], REDACTED_IMAGE_PLACEHOLDER)
        self.assertEqual(result[0]["content"][0]["id"], "audio_123")

    def test_openai_audio_preserved_with_flag(self):
        """OpenAI audio should be preserved when _INTERNAL_LLMA_MULTIMODAL is enabled."""
        os.environ["_INTERNAL_LLMA_MULTIMODAL"] = "true"

        input_data = [
            {
                "role": "assistant",
                "content": [
                    {"type": "audio", "data": "base64audiodata", "id": "audio_123"}
                ],
            }
        ]

        result = sanitize_openai(input_data)
        self.assertEqual(result[0]["content"][0]["data"], "base64audiodata")

    def test_gemini_audio_redacted_by_default(self):
        """Gemini audio should be redacted when _INTERNAL_LLMA_MULTIMODAL is not set."""
        if "_INTERNAL_LLMA_MULTIMODAL" in os.environ:
            del os.environ["_INTERNAL_LLMA_MULTIMODAL"]

        input_data = [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "audio/L16;codec=pcm;rate=24000",
                            "data": "base64audiodata",
                        }
                    }
                ]
            }
        ]

        result = sanitize_gemini(input_data)
        self.assertEqual(
            result[0]["parts"][0]["inline_data"]["data"], REDACTED_IMAGE_PLACEHOLDER
        )

    def test_gemini_audio_preserved_with_flag(self):
        """Gemini audio should be preserved when _INTERNAL_LLMA_MULTIMODAL is enabled."""
        os.environ["_INTERNAL_LLMA_MULTIMODAL"] = "true"

        input_data = [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "audio/L16;codec=pcm;rate=24000",
                            "data": "base64audiodata",
                        }
                    }
                ]
            }
        ]

        result = sanitize_gemini(input_data)
        self.assertEqual(
            result[0]["parts"][0]["inline_data"]["data"], "base64audiodata"
        )


if __name__ == "__main__":
    unittest.main()
