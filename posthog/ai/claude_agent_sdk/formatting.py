from typing import Any, Dict, List

try:
    from claude_agent_sdk import ToolUseBlock
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Claude Agent SDK to use this feature: 'pip install claude-agent-sdk'"
    )

from posthog.ai.media import to_plain
from posthog.ai.sanitization import redact_media


def format_tool_result_content(block: Any, ph_client: Any = None) -> Any:
    """Structured, redacted tool-result content (replaces str(block.content)[:500]).

    Calls redact_media directly (not finalize_ai_content) for the
    max_string_len truncation, which finalize_ai_content doesn't support.
    The processor.py/client.py emit sites re-run finalize_ai_content on the
    already-redacted result when building $ai_input; that's a no-op since
    placeholders don't re-match.
    """
    content = block.content
    if isinstance(content, list):
        content = [to_plain(c) for c in content]
    return redact_media(content, max_string_len=5000, ph_client=ph_client)


def format_assistant_blocks(blocks: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for block in blocks:
        if getattr(block, "thinking", None) is not None:
            thinking_block: Dict[str, Any] = {
                "type": "thinking",
                "thinking": block.thinking,
            }
            signature = getattr(block, "signature", None)
            if signature is not None:
                thinking_block["signature"] = signature
            out.append(thinking_block)
        elif isinstance(block, ToolUseBlock):
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": block.input,
                    },
                }
            )
        elif getattr(block, "text", None) is not None:
            out.append({"type": "text", "text": block.text})
        else:
            out.append(to_plain(block))
    return out
