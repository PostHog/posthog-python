"""Capture invariant: no known provider content kind may capture as [], None, or a repr string."""

import base64
from uuid import uuid4

import pytest

from posthog.ai.sanitization import redact_media

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PNG_B64 = base64.b64encode(PNG).decode()

# Long enough to clear the structural redactor's strong-context length floor
# (_STRONG_CONTEXT_MIN_LEN) so the healthy-path pins actually exercise redaction.
LONG_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 400).decode()
PLACEHOLDER = "[base64 image redacted]"


def _flat_reprs(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [r for v in value.values() for r in _flat_reprs(v)]
    if isinstance(value, list):
        return [r for v in value for r in _flat_reprs(v)]
    return []


def _assert_faithful(formatted_messages):
    assert formatted_messages, "captured nothing"
    for message in formatted_messages:
        content = message.get("content")
        assert content != [], f"empty content for {message}"
        for leaf in _flat_reprs(message):
            assert "object at 0x" not in leaf and not leaf.startswith(
                ("TextBlock(", "ToolUseBlock(", "ResponseReasoningItem(")
            ), f"repr leaked: {leaf[:80]}"


GEMINI_PART_KINDS = [
    {"text": "hi"},
    {"inline_data": {"mime_type": "image/png", "data": PNG_B64}},
    {"inline_data": {"mime_type": "video/mp4", "data": PNG_B64}},
    {"file_data": {"mime_type": "video/mp4", "file_uri": "https://f/1"}},
    {"function_call": {"name": "f", "args": {}}},
    {"function_response": {"name": "f", "response": {}}},
    {"executable_code": {"language": "PYTHON", "code": "1"}},
    {"code_execution_result": {"outcome": "OUTCOME_OK", "output": "1"}},
]


@pytest.mark.parametrize("part", GEMINI_PART_KINDS)
def test_gemini_kind_coverage_dict(part):
    from posthog.ai.gemini.gemini_converter import format_gemini_input

    out = format_gemini_input([{"role": "user", "parts": [part]}])
    _assert_faithful(out)
    assert out[0]["content"], f"part dropped: {part}"


@pytest.mark.parametrize("part", GEMINI_PART_KINDS)
def test_gemini_kind_coverage_typed(part):
    types = pytest.importorskip("google.genai").types
    from posthog.ai.gemini.gemini_converter import format_gemini_input

    typed = types.Part.model_validate(part)
    out = format_gemini_input([types.Content(role="user", parts=[typed])])
    _assert_faithful(out)
    assert out[0]["content"], f"typed part dropped: {part}"


ANTHROPIC_BLOCK_KINDS = [
    {"type": "text", "text": "hi"},
    {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
    },
    {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
    {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": PNG_B64},
    },
    {"type": "tool_use", "id": "t", "name": "f", "input": {}},
    {"type": "tool_result", "tool_use_id": "t", "content": "ok"},
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
    },
    {"type": "thinking", "thinking": "hmm", "signature": "s"},
]


@pytest.mark.parametrize("block", ANTHROPIC_BLOCK_KINDS)
def test_anthropic_input_kind_coverage(block):
    from posthog.ai.anthropic.anthropic_converter import format_anthropic_input

    out = format_anthropic_input([{"role": "user", "content": [block]}])
    _assert_faithful(out)
    assert out[0]["content"], f"block dropped: {block}"


OPENAI_CHAT_PART_KINDS = [
    {"type": "text", "text": "hi"},
    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}},
    {"type": "input_audio", "input_audio": {"data": PNG_B64, "format": "wav"}},
    {"type": "file", "file": {"file_id": "f1"}},
]


@pytest.mark.parametrize("part", OPENAI_CHAT_PART_KINDS)
def test_openai_chat_input_kind_coverage(part):
    from posthog.ai.openai.openai_converter import format_openai_input

    out = format_openai_input([{"role": "user", "content": [part]}])
    _assert_faithful(out)
    assert out[0]["content"], f"part dropped: {part}"


OPENAI_RESPONSES_INPUT_KINDS = [
    {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    {
        "type": "function_call",
        "call_id": "c1",
        "name": "get_weather",
        "arguments": "{}",
    },
    {"type": "function_call_output", "call_id": "c1", "output": "72F"},
    {
        "type": "reasoning",
        "id": "r1",
        "summary": [{"type": "summary_text", "text": "thinking"}],
    },
]


@pytest.mark.parametrize("item", OPENAI_RESPONSES_INPUT_KINDS)
def test_openai_responses_input_kind_coverage(item):
    from posthog.ai.openai.openai_converter import format_openai_input

    out = format_openai_input(input_data=[item])
    _assert_faithful(out)
    assert out[0] != {"role": "user", "content": ""}, f"item collapsed: {item}"


# Typed variants of the same kinds. The canonical Responses agent loop is
# `input_list += response.output`, which appends typed SDK objects — those must
# normalize like the dict shapes above instead of capturing as repr strings.
def _typed_responses_input_items():
    responses = pytest.importorskip("openai").types.responses
    return [
        responses.response_function_tool_call.ResponseFunctionToolCall(
            arguments="{}", call_id="c1", name="get_weather", type="function_call"
        ),
        responses.response_reasoning_item.ResponseReasoningItem(
            id="r1", summary=[], type="reasoning"
        ),
        responses.response_output_message.ResponseOutputMessage(
            id="m1",
            role="assistant",
            status="completed",
            type="message",
            content=[
                responses.response_output_text.ResponseOutputText(
                    annotations=[], text="hi", type="output_text"
                )
            ],
        ),
    ]


def test_openai_responses_input_kind_coverage_typed():
    from posthog.ai.openai.openai_converter import format_openai_input

    for item in _typed_responses_input_items():
        out = format_openai_input(input_data=[item])
        _assert_faithful(out)
        assert out[0] != {"role": "user", "content": ""}, f"item collapsed: {item!r}"


def test_openai_responses_streaming_empty_output_not_fabricated():
    from posthog.ai.openai.openai_converter import format_openai_streaming_output

    assert format_openai_streaming_output([], "responses") == []


# --- Healthy-path pins -------------------------------------------------
#
# The kind-coverage tests above only prove a block survives *formatting*
# faithfully; they don't touch redaction. These pin the structural redactor
# (posthog/ai/sanitization.py `redact_media`) actually strips base64 media
# for the shapes each provider/framework sends it in, at the layer each
# capture path calls it from.


def test_anthropic_top_level_image_redacted():
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
                            "data": LONG_PNG_B64,
                        },
                    }
                ],
            }
        ]
    )
    assert out[0]["content"][0]["source"]["data"] == PLACEHOLDER


def test_openai_chat_image_url_data_url_redacted():
    out = redact_media(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{LONG_PNG_B64}"},
                    }
                ],
            }
        ]
    )
    assert out[0]["content"][0]["image_url"]["url"] == PLACEHOLDER


class FakePH:
    privacy_mode = False

    def __init__(self):
        self.events = []

    def capture(self, *args, **kwargs):
        self.events.append(kwargs)

    def flush(self):
        pass


def _run_langchain_input(content):
    """Run the CallbackHandler harness (on_chat_model_start + on_llm_end) and
    return the captured $ai_input, mirroring how a real LangChain run drives
    the handler."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, LLMResult
    from posthog.ai.langchain.callbacks import CallbackHandler

    fake = FakePH()
    cb = CallbackHandler(client=fake)
    run_id = uuid4()
    cb.on_chat_model_start(
        serialized={},
        messages=[[HumanMessage(content=content)]],
        run_id=run_id,
    )
    cb.on_llm_end(
        LLMResult(
            generations=[[ChatGeneration(message=AIMessage(content="ok"))]],
            llm_output={},
        ),
        run_id=run_id,
    )
    return fake.events[-1]["properties"]["$ai_input"]


LANGCHAIN_IMAGE_SHAPES = [
    pytest.param(
        [
            {"type": "text", "text": "what is this"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": LONG_PNG_B64,
                },
            },
        ],
        lambda content: content[1]["source"]["data"],
        id="anthropic-style",
    ),
    pytest.param(
        [
            {"type": "text", "text": "describe"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{LONG_PNG_B64}"},
            },
        ],
        lambda content: content[1]["image_url"]["url"],
        id="openai-style",
    ),
    pytest.param(
        [
            {"type": "text", "text": "describe"},
            {
                "type": "image",
                "source_type": "base64",
                "data": LONG_PNG_B64,
                "mime_type": "image/png",
            },
        ],
        lambda content: content[1]["data"],
        id="langchain-v0.3-image-block",
    ),
]


@pytest.mark.parametrize("content, extract_data", LANGCHAIN_IMAGE_SHAPES)
def test_langchain_callback_input_image_redacted(content, extract_data):
    pytest.importorskip("langchain_core")

    captured = _run_langchain_input(content)
    assert extract_data(captured[0]["content"]) == PLACEHOLDER


def test_langchain_callback_v03_file_block_redacted():
    """The v0.3 standard file content block redacts its base64 `data` via the
    `data` strong key with a `mime_type` sibling."""
    pytest.importorskip("langchain_core")

    content = [
        {
            "type": "file",
            "source_type": "base64",
            "data": LONG_PNG_B64,
            "mime_type": "application/pdf",
        }
    ]
    captured = _run_langchain_input(content)
    assert captured[0]["content"][0]["data"] == "[base64 file redacted]"
