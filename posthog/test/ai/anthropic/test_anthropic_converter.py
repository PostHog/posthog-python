import pytest

try:
    from anthropic.types import (
        Message,
        RawContentBlockDeltaEvent,
        RawContentBlockStartEvent,
        SignatureDelta,
        TextBlock,
        ThinkingBlock,
        ThinkingDelta,
        ToolUseBlock,
        Usage,
    )

    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from posthog.ai.anthropic.anthropic_converter import (
    format_anthropic_input,
    format_anthropic_response,
    format_anthropic_streaming_content,
    format_anthropic_streaming_output_complete,
    handle_anthropic_content_block_start,
    handle_anthropic_text_delta,
)

pytestmark = pytest.mark.skipif(
    not ANTHROPIC_AVAILABLE, reason="anthropic not available"
)


def _msg(content):
    return Message(
        id="m",
        content=content,
        model="claude-sonnet-4-5",
        role="assistant",
        stop_reason="end_turn",
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def test_round_tripped_blocks_become_dicts():
    out = format_anthropic_input(
        [
            {
                "role": "assistant",
                "content": [
                    TextBlock(text="let me check", type="text"),
                    ToolUseBlock(
                        id="t1", name="search", input={"q": "x"}, type="tool_use"
                    ),
                ],
            },
        ]
    )
    content = out[0]["content"]
    assert content[0]["type"] == "text" and content[0]["text"] == "let me check"
    assert content[1]["type"] == "tool_use" and content[1]["name"] == "search"
    assert all(isinstance(block, dict) for block in content)


def test_thinking_blocks_survive_response():
    out = format_anthropic_response(
        _msg(
            [
                ThinkingBlock(thinking="hmm", signature="s", type="thinking"),
                TextBlock(text="answer", type="text"),
            ]
        )
    )
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "hmm", "signature": "s"}
    assert blocks[1] == {"type": "text", "text": "answer"}


def test_empty_text_block_survives_response():
    out = format_anthropic_response(_msg([TextBlock(text="", type="text")]))
    assert out[0]["content"] == [{"type": "text", "text": ""}]


def test_unknown_block_kind_preserved():
    out = format_anthropic_input(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "web_search_tool_result",
                        "content": [{"url": "https://x"}],
                    }
                ],
            }
        ]
    )
    assert out[0]["content"][0]["type"] == "web_search_tool_result"


def test_streaming_thinking_deltas_accumulate():
    start_event = RawContentBlockStartEvent(
        type="content_block_start",
        index=0,
        content_block=ThinkingBlock(type="thinking", thinking="", signature=""),
    )
    block, tool = handle_anthropic_content_block_start(start_event)
    assert tool is None
    content_blocks = [block]
    current_block = (
        block
        if block is not None and block.get("type") in ("text", "thinking")
        else None
    )

    delta_events = [
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=ThinkingDelta(type="thinking_delta", thinking="Let me think"),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=ThinkingDelta(type="thinking_delta", thinking=" it through."),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=SignatureDelta(type="signature_delta", signature="sig-123"),
        ),
    ]
    for event in delta_events:
        handle_anthropic_text_delta(event, current_block)

    formatted = format_anthropic_streaming_content(content_blocks)
    assert formatted == [
        {
            "type": "thinking",
            "thinking": "Let me think it through.",
            "signature": "sig-123",
        }
    ]

    output = format_anthropic_streaming_output_complete(content_blocks, "")
    assert output == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Let me think it through.",
                    "signature": "sig-123",
                }
            ],
        }
    ]
