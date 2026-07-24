import base64

import pytest

try:
    from google.genai import types

    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from posthog.ai.gemini.gemini_converter import (
    extract_gemini_content_from_chunk,
    format_gemini_input,
    format_gemini_response,
    format_gemini_streaming_output,
)

pytestmark = pytest.mark.skipif(
    not GEMINI_AVAILABLE, reason="google-genai not available"
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


class TestGeminiInputParts:
    def test_typed_video_file_part_preserved(self):
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(text="watch this"),
                    types.Part(
                        file_data=types.FileData(
                            file_uri="https://files/abc", mime_type="video/mp4"
                        )
                    ),
                ],
            )
        ]
        out = format_gemini_input(contents)
        assert out[0]["content"][0] == {"type": "text", "text": "watch this"}
        assert out[0]["content"][1] == {
            "type": "video",
            "file_data": {"mime_type": "video/mp4", "file_uri": "https://files/abc"},
        }

    def test_typed_inline_image_part_bytes_base64d(self):
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(inline_data=types.Blob(data=PNG, mime_type="image/png"))
                ],
            )
        ]
        out = format_gemini_input(contents)
        block = out[0]["content"][0]
        assert block["type"] == "image"
        assert block["inline_data"]["data"] == base64.b64encode(PNG).decode()

    def test_function_call_and_response_turns_preserved(self):
        contents = [
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name="get_weather", args={"city": "SF"}
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="get_weather", response={"temp": "18C"}
                        )
                    )
                ],
            ),
        ]
        out = format_gemini_input(contents)
        assert out[0]["content"][0]["type"] == "function_call"
        assert out[0]["content"][0]["function_call"]["name"] == "get_weather"
        assert out[1]["content"][0]["type"] == "function_response"

    def test_camel_case_dict_part_preserved(self):
        out = format_gemini_input(
            [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(PNG).decode(),
                            }
                        }
                    ],
                }
            ]
        )
        assert out[0]["content"][0]["type"] == "image"
        assert out[0]["content"][0]["inline_data"]["mime_type"] == "image/png"

    def test_model_dumped_part_with_none_text_keeps_image(self):
        out = format_gemini_input(
            [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": None,
                            "inline_data": {"mime_type": "image/png", "data": "AAAA"},
                        }
                    ],
                }
            ]
        )
        assert out[0]["content"] == [
            {"type": "image", "inline_data": {"mime_type": "image/png", "data": "AAAA"}}
        ]

    def test_bare_part_list_same_as_wrapped(self):
        bare = format_gemini_input(
            [types.Part(inline_data=types.Blob(data=PNG, mime_type="image/png")), "hi"]
        )
        assert bare[0]["content"][0]["type"] == "image"
        assert bare[1] == {"role": "user", "content": "hi"}

    def test_unknown_part_kind_preserved_with_label(self):
        out = format_gemini_input(
            [
                {
                    "role": "user",
                    "parts": [
                        {"executable_code": {"language": "PYTHON", "code": "1+1"}}
                    ],
                }
            ]
        )
        assert out[0]["content"][0]["type"] == "executable_code"

    def test_empty_string_text_kept(self):
        out = format_gemini_input([{"role": "user", "parts": [{"text": ""}]}])
        assert out[0]["content"] == [{"type": "text", "text": ""}]


class TestGeminiResponse:
    def test_image_generation_output_labeled_image(self):
        resp = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(text="here you go:"),
                            types.Part(
                                inline_data=types.Blob(data=PNG, mime_type="image/png")
                            ),
                        ],
                    ),
                )
            ]
        )
        out = format_gemini_response(resp)
        blocks = out[0]["content"]
        assert blocks[0] == {"type": "text", "text": "here you go:"}
        assert blocks[1]["type"] == "image"
        assert blocks[1]["inline_data"]["mime_type"] == "image/png"
        assert blocks[1]["inline_data"]["data"] == base64.b64encode(PNG).decode()

    def test_streaming_keeps_inline_data_chunks(self):
        chunks = [
            types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(
                            role="model", parts=[types.Part(text="sure: ")]
                        )
                    )
                ]
            ),
            types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part(
                                    inline_data=types.Blob(
                                        data=PNG, mime_type="image/png"
                                    )
                                )
                            ],
                        )
                    )
                ]
            ),
        ]
        acc: list = []
        for ch in chunks:
            blocks = extract_gemini_content_from_chunk(ch)
            if blocks:
                acc.extend(blocks)
        out = format_gemini_streaming_output(acc)
        types_seen = [b["type"] for b in out[0]["content"]]
        assert "text" in types_seen and "image" in types_seen

    def test_streaming_chunk_with_mixed_text_and_image_captures_both(self):
        chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(text="here: "),
                            types.Part(
                                inline_data=types.Blob(data=PNG, mime_type="image/png")
                            ),
                        ],
                    )
                )
            ]
        )
        blocks = extract_gemini_content_from_chunk(chunk)
        assert blocks == [
            {"type": "text", "text": "here: "},
            {
                "type": "image",
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(PNG).decode(),
                },
            },
        ]

        out = format_gemini_streaming_output(blocks)
        content = out[0]["content"]
        types_seen = [b["type"] for b in content]
        assert types_seen == ["text", "image"]
        assert content[0] == {"type": "text", "text": "here: "}
        assert content[1]["inline_data"]["data"] == base64.b64encode(PNG).decode()
