from typing import Any, Dict, List, Optional

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


def _append_assistant_output(
    pending: List[Dict[str, Any]], blocks: Any
) -> List[Dict[str, Any]]:
    """Add one AssistantMessage's blocks to the output of the current generation.

    The CLI sends each content block of a response as its own AssistantMessage.
    """
    output_content = format_assistant_blocks(blocks)
    if not output_content:
        return pending
    previous = pending[0]["content"] if pending else []
    return [{"role": "assistant", "content": previous + output_content}]


def _append_user_input(
    previous: Optional[List[Dict[str, Any]]], content: Any
) -> List[Dict[str, Any]]:
    queued = previous or []
    last = queued[-1] if queued else None
    if (
        isinstance(content, list)
        and last is not None
        and last.get("role") == "user"
        and isinstance(last.get("content"), list)
    ):
        return queued[:-1] + [{"role": "user", "content": last["content"] + content}]
    return queued + [{"role": "user", "content": content}]


def _format_user_input(
    content: Any, client: Any, previous: Optional[List[Dict[str, Any]]]
) -> Optional[List[Dict[str, Any]]]:
    """Queue a UserMessage as input for the next generation.

    The CLI sends each tool result as its own UserMessage, so results are
    appended to what is already queued instead of replacing it.
    """
    if isinstance(content, str):
        return _append_user_input(previous, content)
    if isinstance(content, list):
        formatted: List[Dict[str, Any]] = []
        for block in content:
            if hasattr(block, "tool_use_id"):
                formatted.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": format_tool_result_content(block, client),
                    }
                )
            elif hasattr(block, "text"):
                formatted.append({"type": "text", "text": block.text})
        if formatted:
            return _append_user_input(previous, formatted)
    return previous
