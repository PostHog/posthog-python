import base64
import types
from dataclasses import dataclass

import pytest

try:
    from posthog.ai.claude_agent_sdk import client as cas_client
    from posthog.ai.claude_agent_sdk import processor as cas_processor

    CLAUDE_AGENT_SDK_AVAILABLE = True
except ImportError:
    CLAUDE_AGENT_SDK_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not CLAUDE_AGENT_SDK_AVAILABLE, reason="Claude Agent SDK is not available"
)

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 400).decode()
IMAGE_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
}


@dataclass
class FakeToolResultBlock:
    tool_use_id: str = "t1"
    content: object = None


@dataclass
class FakeThinkingBlock:
    thinking: str = "let me reason"
    signature: str = "sig"


@dataclass
class FakeTextBlock:
    text: str = ""


@dataclass
class FakeThinkingWithEmptyTextBlock:
    thinking: str = "let me reason"
    text: str = ""


@pytest.mark.parametrize(
    "mod", [cas_processor, cas_client] if CLAUDE_AGENT_SDK_AVAILABLE else []
)
def test_tool_result_keeps_structure_and_redacts_image(mod):
    block = FakeToolResultBlock(
        content=[IMAGE_BLOCK, {"type": "text", "text": "x" * 6000}]
    )
    out = mod.format_tool_result_content(block)
    assert isinstance(out, list)
    assert out[0]["source"]["data"] == "[base64 image redacted]"
    assert out[1]["text"].endswith("... [truncated]")
    assert len(out[1]["text"]) == 5000 + len("... [truncated]")


@pytest.mark.parametrize(
    "mod", [cas_processor, cas_client] if CLAUDE_AGENT_SDK_AVAILABLE else []
)
def test_empty_string_tool_content_not_none(mod):
    assert mod.format_tool_result_content(FakeToolResultBlock(content="")) == ""


@pytest.mark.parametrize(
    "mod", [cas_processor, cas_client] if CLAUDE_AGENT_SDK_AVAILABLE else []
)
def test_passthrough_tool_result_media_not_truncated(mod):
    # A client opted into multimodal capture must not get its tool-result media
    # cut at max_string_len — a 5000-char slice through base64 corrupts it.
    client = types.SimpleNamespace(_enable_multimodal_capture=True)
    long_b64 = "A" * 6000
    block = FakeToolResultBlock(
        content=[
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": long_b64,
                },
            }
        ]
    )
    out = mod.format_tool_result_content(block, ph_client=client)
    assert out[0]["source"]["data"] == long_b64


@pytest.mark.parametrize(
    "mod", [cas_processor, cas_client] if CLAUDE_AGENT_SDK_AVAILABLE else []
)
def test_thinking_block_captured(mod):
    out = mod.format_assistant_blocks([FakeThinkingBlock()])
    assert {
        "type": "thinking",
        "thinking": "let me reason",
        "signature": "sig",
    } in out


@pytest.mark.parametrize(
    "mod", [cas_processor, cas_client] if CLAUDE_AGENT_SDK_AVAILABLE else []
)
def test_empty_text_block_keeps_type_label(mod):
    out = mod.format_assistant_blocks([FakeTextBlock()])
    assert out == [{"type": "text", "text": ""}]


@pytest.mark.parametrize(
    "mod", [cas_processor, cas_client] if CLAUDE_AGENT_SDK_AVAILABLE else []
)
def test_thinking_takes_precedence_over_empty_text(mod):
    out = mod.format_assistant_blocks([FakeThinkingWithEmptyTextBlock()])
    assert out == [{"type": "thinking", "thinking": "let me reason"}]
