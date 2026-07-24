import pytest

try:
    from openai.types.chat import ChatCompletionMessage

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from posthog.ai.openai.openai_converter import format_openai_input
from posthog.test.ai.utils import make_response_usage

pytestmark = pytest.mark.skipif(not OPENAI_AVAILABLE, reason="openai not available")


def _build_reasoning_response():
    from openai.types.responses import Response
    from openai.types.responses.response_output_message import ResponseOutputMessage
    from openai.types.responses.response_output_text import ResponseOutputText
    from openai.types.responses.response_reasoning_item import ResponseReasoningItem

    return Response(
        id="r",
        created_at=1.0,
        model="o4-mini",
        object="response",
        output=[
            ResponseReasoningItem(id="rs", summary=[], type="reasoning"),
            ResponseOutputMessage(
                id="m",
                role="assistant",
                status="completed",
                type="message",
                content=[
                    ResponseOutputText(annotations=[], text="42", type="output_text")
                ],
            ),
        ],
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        usage=make_response_usage(
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
        ),
    )


def test_tool_calls_preserved_in_input():
    out = format_openai_input(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "42"},
        ]
    )
    assert out[0]["tool_calls"][0]["id"] == "c1"
    assert out[0]["content"] is None
    assert out[1]["tool_call_id"] == "c1"


def test_message_object_does_not_crash():
    msg = ChatCompletionMessage(role="assistant", content="hello", refusal=None)
    out = format_openai_input([{"role": "user", "content": "hi"}, msg])
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == "hello"


def test_responses_output_keeps_all_items():
    from posthog.ai.openai.openai_converter import format_openai_response

    resp = _build_reasoning_response()
    out = format_openai_response(resp)
    types_seen = [b["type"] for b in out[0]["content"]]
    assert "reasoning" in types_seen
    assert any(b.get("text") == "42" for b in out[0]["content"])


def test_responses_output_dict_items_not_dropped():
    """Proxied/serialized Responses arrive dict-shaped; typed-only access used
    to read type as None and drop every item, capturing []."""
    import types

    from posthog.ai.openai.openai_converter import format_openai_response

    resp = types.SimpleNamespace(
        output=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "42"}],
            },
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "get_weather",
                "arguments": "{}",
            },
        ]
    )

    out = format_openai_response(resp)
    content = out[0]["content"]
    assert {"type": "text", "text": "42"} in content
    assert {
        "type": "function",
        "id": "c1",
        "function": {"name": "get_weather", "arguments": "{}"},
    } in content


def test_responses_streaming_matches_nonstreaming():
    from posthog.ai.openai.openai_converter import (
        extract_openai_content_from_chunk,
        format_openai_response,
        format_openai_streaming_output,
    )

    class Completed:
        type = "response.completed"

        def __init__(self, response):
            self.response = response

    resp = _build_reasoning_response()
    acc = [extract_openai_content_from_chunk(Completed(resp), "responses")]
    streaming_out = format_openai_streaming_output(
        [a for a in acc if a is not None], "responses"
    )
    assert streaming_out == format_openai_response(resp)


def test_chat_streaming_audio_and_refusal_deltas():
    """
    `ChoiceDelta` has no declared `audio` field (only the non-streaming
    `ChatCompletionMessage` does), but the OpenAI SDK's pydantic models allow
    extra fields (`model_config = ConfigDict(extra="allow")`), so an `audio`
    delta from the gpt-4o-audio-preview streaming API round-trips as a plain
    dict via `model_validate` — no stub object needed.
    """
    from openai.types.chat.chat_completion_chunk import (
        ChatCompletionChunk,
        Choice,
        ChoiceDelta,
    )

    from posthog.ai.openai.openai_converter import (
        extract_openai_content_from_chunk,
        format_openai_streaming_output,
    )

    def _chunk(delta_kwargs):
        delta = ChoiceDelta.model_validate(delta_kwargs)
        return ChatCompletionChunk(
            id="c1",
            object="chat.completion.chunk",
            created=1,
            model="gpt-4o-audio-preview",
            choices=[Choice(index=0, delta=delta, finish_reason=None)],
        )

    chunks = [
        _chunk(
            {
                "role": "assistant",
                "audio": {"id": "a1", "transcript": "hel", "data": "AA=="},
            }
        ),
        _chunk({"audio": {"transcript": "lo", "data": "BB=="}}),
        _chunk({"refusal": "I can't help with that"}),
    ]

    accumulated = [
        item
        for item in (
            extract_openai_content_from_chunk(chunk, "chat") for chunk in chunks
        )
        if item is not None
    ]

    output = format_openai_streaming_output(accumulated, "chat")
    content = output[0]["content"]

    audio_block = next(b for b in content if b["type"] == "audio")
    assert audio_block["id"] == "a1"
    assert audio_block["transcript"] == "hello"
    assert audio_block["data"] == "AA==BB=="

    refusal_block = next(b for b in content if b["type"] == "refusal")
    assert refusal_block["refusal"] == "I can't help with that"
