from typing import Any, Dict, List, Optional

from ..types import StreamingContentBlock, TokenUsage, ToolInProgress
from ..utils import merge_usage_stats
from .anthropic_converter import (
    extract_anthropic_usage_from_event,
    finalize_anthropic_tool_input,
    handle_anthropic_content_block_start,
    handle_anthropic_text_delta,
    handle_anthropic_tool_delta,
)


class _AnthropicStreamAccumulator:
    """Accumulates sync-neutral capture state from Anthropic stream events."""

    def __init__(self) -> None:
        self.usage_stats: TokenUsage = TokenUsage()
        self.accumulated_content = ""
        self.content_blocks: List[StreamingContentBlock] = []
        self.tools_in_progress: Dict[str, ToolInProgress] = {}
        self.current_text_block: Optional[StreamingContentBlock] = None
        self.stop_reason: Optional[str] = None
        # Delta and stop events address blocks by their index in the message. That
        # index also counts blocks content_blocks leaves out (server_tool_use,
        # web_search_tool_result, ...), so keep a list that lines up with it.
        self._blocks_by_index: List[StreamingContentBlock] = []

    def consume(self, event: Any) -> None:
        event_usage = extract_anthropic_usage_from_event(event)
        merge_usage_stats(self.usage_stats, event_usage)

        if getattr(event, "type", None) == "content_block_start":
            block, tool = handle_anthropic_content_block_start(event)

            self._blocks_by_index.append(block or {})

            if block:
                self.content_blocks.append(block)
                if block.get("type") in ("text", "thinking"):
                    self.current_text_block = block
                else:
                    self.current_text_block = None

            if tool:
                tool_id = tool["block"].get("id")
                if tool_id:
                    self.tools_in_progress[tool_id] = tool

        delta_text = handle_anthropic_text_delta(event, self.current_text_block)
        if delta_text:
            self.accumulated_content += delta_text

        handle_anthropic_tool_delta(
            event, self._blocks_by_index, self.tools_in_progress
        )

        if getattr(event, "type", None) == "content_block_stop":
            self.current_text_block = None
            finalize_anthropic_tool_input(
                event, self._blocks_by_index, self.tools_in_progress
            )

        if getattr(event, "type", None) == "message_delta":
            delta = getattr(event, "delta", None)
            delta_stop_reason = getattr(delta, "stop_reason", None)
            if delta_stop_reason is not None:
                self.stop_reason = delta_stop_reason
