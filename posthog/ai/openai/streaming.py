"""Sync-neutral state accumulation for OpenAI streaming endpoints."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..types import StreamingEventData, TokenUsage
from ..utils import merge_usage_stats
from .openai_converter import (
    accumulate_openai_tool_calls,
    extract_openai_content_from_chunk,
    extract_openai_tool_calls_from_chunk,
    extract_openai_usage_from_chunk,
)


@dataclass
class _ResponsesStreamState:
    """Accumulates state specific to a Responses API stream."""

    usage_stats: TokenUsage = field(default_factory=lambda: TokenUsage())
    output: List[Any] = field(default_factory=list)
    model: Optional[str] = None
    stop_reason: Optional[str] = None

    def process_chunk(self, chunk: Any) -> None:
        response = getattr(chunk, "response", None)
        if response and self.model is None and hasattr(response, "model"):
            self.model = response.model

        chunk_usage = extract_openai_usage_from_chunk(chunk, "responses")
        if chunk_usage:
            merge_usage_stats(self.usage_stats, chunk_usage)

        content = extract_openai_content_from_chunk(chunk, "responses")
        if content is not None:
            self.output.extend(content)

        if getattr(chunk, "type", None) == "response.completed" and response:
            status = getattr(response, "status", None)
            if status is not None:
                self.stop_reason = status


@dataclass
class _ChatCompletionsStreamState:
    """Accumulates state specific to a Chat Completions stream."""

    usage_stats: TokenUsage = field(default_factory=lambda: TokenUsage())
    output: List[Any] = field(default_factory=list)
    _tool_calls: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    model: Optional[str] = None
    stop_reason: Optional[str] = None

    def process_chunk(self, chunk: Any) -> None:
        if self.model is None and hasattr(chunk, "model"):
            self.model = chunk.model

        chunk_usage = extract_openai_usage_from_chunk(chunk, "chat")
        if chunk_usage:
            merge_usage_stats(self.usage_stats, chunk_usage)

        content = extract_openai_content_from_chunk(chunk, "chat")
        if content is not None:
            self.output.append(content)

        chunk_tool_calls = extract_openai_tool_calls_from_chunk(chunk)
        if chunk_tool_calls:
            accumulate_openai_tool_calls(self._tool_calls, chunk_tool_calls)

        choices = getattr(chunk, "choices", None)
        if choices:
            finish_reason = getattr(choices[0], "finish_reason", None)
            if finish_reason is not None:
                self.stop_reason = finish_reason

    @property
    def tool_calls(self) -> Optional[List[Dict[str, Any]]]:
        return list(self._tool_calls.values()) if self._tool_calls else None


def _build_streaming_event_data(
    *,
    base_url: Any,
    kwargs: Dict[str, Any],
    formatted_input: Any,
    formatted_output: Any,
    usage_stats: TokenUsage,
    latency: float,
    distinct_id: Optional[str],
    trace_id: Optional[str],
    properties: Optional[Dict[str, Any]],
    privacy_mode: bool,
    groups: Optional[Dict[str, Any]],
    model_from_response: Optional[str],
    stop_reason: Optional[str],
) -> StreamingEventData:
    """Build the fields shared by both OpenAI streaming endpoint events."""

    return StreamingEventData(
        provider="openai",
        model=kwargs.get("model") or model_from_response or "unknown",
        base_url=str(base_url),
        kwargs=kwargs,
        formatted_input=formatted_input,
        formatted_output=formatted_output,
        usage_stats=usage_stats,
        latency=latency,
        distinct_id=distinct_id,
        trace_id=trace_id,
        properties=properties,
        privacy_mode=privacy_mode,
        groups=groups,
        stop_reason=stop_reason,
    )
