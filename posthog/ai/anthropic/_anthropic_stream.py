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

    def _apply_cumulative_input_usage(self, usage: Any) -> None:
        # Usage on message_delta is cumulative. With server tools (web search,
        # web fetch, code execution) its input and cache counts grow past
        # message_start's, which is all the per-event merge records for them.
        # The totals never shrink, so keep the larger value; a field the event
        # omits leaves message_start's count in place.
        input_tokens = getattr(usage, "input_tokens", None)
        if isinstance(input_tokens, int):
            self.usage_stats["input_tokens"] = max(
                self.usage_stats.get("input_tokens") or 0, input_tokens
            )
        cache_creation = getattr(usage, "cache_creation_input_tokens", None)
        if isinstance(cache_creation, int):
            self.usage_stats["cache_creation_input_tokens"] = max(
                self.usage_stats.get("cache_creation_input_tokens") or 0,
                cache_creation,
            )
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        if isinstance(cache_read, int):
            self.usage_stats["cache_read_input_tokens"] = max(
                self.usage_stats.get("cache_read_input_tokens") or 0, cache_read
            )

    def consume(self, event: Any) -> None:
        event_usage = extract_anthropic_usage_from_event(event)
        merge_usage_stats(self.usage_stats, event_usage)
        if getattr(event, "type", None) == "message_delta":
            self._apply_cumulative_input_usage(getattr(event, "usage", None))

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
