import base64
import types
import unittest
from unittest import mock

from posthog.ai.sanitization import (
    redact_base64_data_url,
    redact_media,
    sanitize_openai,
    sanitize_openai_response,
    sanitize_anthropic,
    sanitize_gemini,
    sanitize_langchain,
    REDACTED_IMAGE_PLACEHOLDER,
)

# Raw-base64 redaction requires a strong-context key and len >= 200, so
# fixtures standing in for "some base64" must be realistically long.
LONG_RAW_BASE64 = base64.b64encode(b"\x00" * 200).decode()


class TestSanitization(unittest.TestCase):
    def setUp(self):
        self.sample_base64_image = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ..."
        self.sample_base64_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA..."
        self.regular_url = "https://example.com/image.jpg"

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

    def test_sanitize_openai_input_image(self):
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

        result = sanitize_openai(input_data)

        self.assertEqual(
            result[0]["content"][0]["image_url"], REDACTED_IMAGE_PLACEHOLDER
        )

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
                            "data": LONG_RAW_BASE64,
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
                            "data": LONG_RAW_BASE64,
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
                        "source": {"data": LONG_RAW_BASE64},
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
        # Raw base64 strings (no data URL prefix) are redacted under a strong
        # key once they clear the len >= 200 floor.
        raw_base64 = LONG_RAW_BASE64

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


class TestClientMultimodalPassthrough(unittest.TestCase):
    """Multimodal passthrough is gated on the client's _enable_multimodal_capture."""

    def setUp(self):
        self.image = "data:image/jpeg;base64," + "A" * 64
        self.openai_input = [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": self.image}}],
            }
        ]

    def _client(self, enabled):
        return types.SimpleNamespace(_enable_multimodal_capture=enabled)

    def test_flag_preserves_media_across_entry_points(self):
        client = self._client(True)
        for fn, data in [
            (sanitize_openai, self.openai_input),
            (
                sanitize_anthropic,
                [
                    {
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": "A" * 64,
                                },
                            }
                        ]
                    }
                ],
            ),
            (
                sanitize_gemini,
                [
                    {
                        "parts": [
                            {
                                "inline_data": {
                                    "mime_type": "image/jpeg",
                                    "data": "A" * 64,
                                }
                            }
                        ]
                    }
                ],
            ),
            (
                sanitize_langchain,
                [
                    {
                        "content": [
                            {"type": "image_url", "image_url": {"url": self.image}}
                        ]
                    }
                ],
            ),
        ]:
            self.assertEqual(fn(data, ph_client=client), data)

    def test_flag_off_still_redacts(self):
        result = sanitize_openai(self.openai_input, ph_client=self._client(False))
        self.assertEqual(
            result[0]["content"][0]["image_url"]["url"], REDACTED_IMAGE_PLACEHOLDER
        )

    def test_no_client_redacts(self):
        result = sanitize_openai(self.openai_input)
        self.assertEqual(
            result[0]["content"][0]["image_url"]["url"], REDACTED_IMAGE_PLACEHOLDER
        )

    def test_unspecced_mock_client_still_redacts(self):
        result = sanitize_openai(self.openai_input, ph_client=mock.Mock())
        self.assertEqual(
            result[0]["content"][0]["image_url"]["url"], REDACTED_IMAGE_PLACEHOLDER
        )


class TestAudioRedaction(unittest.TestCase):
    def _client(self, enabled):
        return types.SimpleNamespace(_enable_multimodal_capture=enabled)

    def test_openai_audio_redacted_by_default(self):
        input_data = [
            {
                "role": "assistant",
                "content": [
                    {"type": "audio", "data": LONG_RAW_BASE64, "id": "audio_123"}
                ],
            }
        ]

        result = sanitize_openai(input_data)
        # The placeholder says "audio" (from the sibling "type" field) instead of
        # the generic "image" default, since there's no mime_type/format sibling.
        self.assertEqual(result[0]["content"][0]["data"], "[base64 audio redacted]")
        self.assertEqual(result[0]["content"][0]["id"], "audio_123")

    def test_openai_audio_preserved_with_flag(self):
        input_data = [
            {
                "role": "assistant",
                "content": [
                    {"type": "audio", "data": "base64audiodata", "id": "audio_123"}
                ],
            }
        ]

        result = sanitize_openai(input_data, ph_client=self._client(True))
        self.assertEqual(result[0]["content"][0]["data"], "base64audiodata")

    def test_gemini_audio_redacted_by_default(self):
        input_data = [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "audio/L16;codec=pcm;rate=24000",
                            "data": LONG_RAW_BASE64,
                        }
                    }
                ]
            }
        ]

        result = sanitize_gemini(input_data)
        # Placeholder text reflects the sibling mime type instead of always "image".
        self.assertEqual(
            result[0]["parts"][0]["inline_data"]["data"], "[base64 audio redacted]"
        )

    def test_gemini_audio_preserved_with_flag(self):
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

        result = sanitize_gemini(input_data, ph_client=self._client(True))
        self.assertEqual(
            result[0]["parts"][0]["inline_data"]["data"], "base64audiodata"
        )


PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 400).decode()


class TestMediaRedactor:
    def test_data_url_redacted_anywhere(self):
        out = redact_media({"note": f"data:image/png;base64,{PNG_B64}"})
        assert out == {"note": REDACTED_IMAGE_PLACEHOLDER}

    def test_anthropic_image_source_redacted(self):
        out = redact_media(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": PNG_B64,
                            },
                        }
                    ],
                }
            ]
        )
        assert out[0]["content"][0]["source"]["data"] == REDACTED_IMAGE_PLACEHOLDER

    def test_nested_tool_result_image_redacted(self):
        out = redact_media(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": PNG_B64,
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        )
        assert (
            out[0]["content"][0]["content"][0]["source"]["data"]
            == REDACTED_IMAGE_PLACEHOLDER
        )

    def test_document_pdf_redacted(self):
        out = redact_media(
            [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": PNG_B64,
                    },
                }
            ]
        )
        assert out[0]["source"]["data"] == "[base64 file redacted]"

    def test_input_audio_redacted(self):
        out = redact_media(
            [{"type": "input_audio", "input_audio": {"data": PNG_B64, "format": "wav"}}]
        )
        assert out[0]["input_audio"]["data"] == "[base64 audio redacted]"

    def test_gemini_inline_data_redacted(self):
        out = redact_media(
            {
                "type": "image",
                "inline_data": {"mime_type": "image/png", "data": PNG_B64},
            }
        )
        assert out["inline_data"]["data"] == REDACTED_IMAGE_PLACEHOLDER

    def test_bytes_redacted_by_default(self):
        out = redact_media(
            {"inline_data": {"mime_type": "video/mp4", "data": b"\x00" * 300}}
        )
        assert out["inline_data"]["data"] == "[base64 video redacted]"

    def test_bytes_base64d_in_passthrough(self):
        client = types.SimpleNamespace(_enable_multimodal_capture=True)
        raw = b"\x00\x01\x02"
        out = redact_media(
            {"inline_data": {"mime_type": "video/mp4", "data": raw}}, ph_client=client
        )
        assert out["inline_data"]["data"] == base64.b64encode(raw).decode()

    def test_passthrough_leaves_strings(self):
        client = types.SimpleNamespace(_enable_multimodal_capture=True)
        val = {"inline_data": {"mime_type": "image/png", "data": PNG_B64}}
        assert redact_media(val, ph_client=client) == val

    def test_shared_reference_not_nulled(self):
        img = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
        }
        out = redact_media(
            [{"role": "user", "content": [img]}, {"role": "user", "content": [img]}]
        )
        assert out[1]["content"][0] is not None
        assert out[1]["content"][0]["source"]["data"] == REDACTED_IMAGE_PLACEHOLDER

    def test_cycle_does_not_hang(self):
        a: dict = {"x": 1}
        a["self"] = a
        out = redact_media(a)
        assert out["x"] == 1

    def test_short_token_in_result_key_not_redacted(self):
        tok = "A1b2C3d4" * 10  # 80 chars, plausible hash/JWT-ish token
        out = redact_media({"type": "tool_result", "result": tok})
        assert out["result"] == tok

    def test_long_raw_base64_in_data_key_redacted(self):
        long_b64 = base64.b64encode(b"\xff" * 600).decode()  # 800 chars
        out = redact_media({"data": long_b64, "mime_type": "image/png"})
        assert out["data"] == REDACTED_IMAGE_PLACEHOLDER

    def test_long_string_weak_context_untouched(self):
        s = "A" * 800
        assert redact_media({"note": s}) == {"note": s}

    def test_max_string_len_truncates_leaves(self):
        out = redact_media({"content": "x" * 100}, max_string_len=10)
        assert out["content"] == "x" * 10 + "... [truncated]"

    def test_placeholder_idempotent(self):
        v = {"data": REDACTED_IMAGE_PLACEHOLDER, "mime_type": "image/png"}
        assert redact_media(v) == v

    def test_raw_base64_under_image_url_url_redacted(self):
        long_b64 = base64.b64encode(b"\xff" * 300).decode()
        out = redact_media({"type": "image_url", "image_url": {"url": long_b64}})
        assert out["image_url"]["url"] == REDACTED_IMAGE_PLACEHOLDER

    def test_https_url_under_image_url_url_untouched(self):
        url = "https://example.com/" + "a" * 300
        out = redact_media({"type": "image_url", "image_url": {"url": url}})
        assert out["image_url"]["url"] == url

    def test_data_url_under_image_url_url_still_redacted(self):
        data_url = f"data:image/png;base64,{PNG_B64}"
        out = redact_media({"image_url": {"url": data_url}})
        assert out["image_url"]["url"] == REDACTED_IMAGE_PLACEHOLDER


def test_sanitize_messages_importable_from_utils():
    """Back-compat: sanitize_messages was previously only in utils, now re-exported."""
    from posthog.ai.utils import sanitize_messages

    data_url = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ..."
    result = sanitize_messages([{"role": "user", "content": data_url}])
    assert result[0]["content"] == REDACTED_IMAGE_PLACEHOLDER


if __name__ == "__main__":
    unittest.main()
