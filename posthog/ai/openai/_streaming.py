"""Sync-neutral state accumulation for OpenAI streaming endpoints."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from ..types import FormattedMessage, StreamingEventData, TokenUsage
from ..utils import merge_usage_stats, _responses_stop_reason
from .openai_converter import (
    accumulate_openai_tool_calls,
    extract_openai_content_from_chunk,
    extract_openai_tool_calls_from_chunk,
    extract_openai_usage_from_chunk,
    format_openai_streaming_output,
)


@dataclass
class _ResponsesStreamState:
    """Accumulates state specific to a Responses API stream."""

    usage_stats: TokenUsage = field(default_factory=lambda: TokenUsage())
    output: List[Any] = field(default_factory=list)
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    service_tier: Optional[str] = None

    def process_chunk(self, chunk: Any) -> None:
        response = getattr(chunk, "response", None)
        if response and self.model is None and hasattr(response, "model"):
            self.model = response.model
        if response and self.service_tier is None:
            self.service_tier = getattr(response, "service_tier", None)

        chunk_usage = extract_openai_usage_from_chunk(chunk, "responses")
        if chunk_usage:
            merge_usage_stats(self.usage_stats, chunk_usage)

        content = extract_openai_content_from_chunk(chunk, "responses")
        if content is not None:
            self.output.extend(content)

        # A stream can end on response.completed, response.incomplete, or
        # response.failed; any terminal response names the stop reason.
        if response:
            stop_reason = _responses_stop_reason(response)
            if stop_reason is not None:
                self.stop_reason = stop_reason


@dataclass
class _ChoiceStream:
    """One choice's view of a chunk, so the per-chunk helpers read that choice."""

    choices: List[Any]


@dataclass
class _ChatCompletionsStreamState:
    """Accumulates state specific to a Chat Completions stream.

    With ``n > 1`` each chunk carries the delta of one choice, named by its
    ``index``, so content and tool calls are kept per choice.
    """

    usage_stats: TokenUsage = field(default_factory=lambda: TokenUsage())
    _outputs: Dict[int, List[Any]] = field(default_factory=dict)
    _tool_calls: Dict[int, Dict[Union[int, str], Dict[str, Any]]] = field(
        default_factory=dict
    )
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    service_tier: Optional[str] = None

    def process_chunk(self, chunk: Any) -> None:
        if self.model is None and hasattr(chunk, "model"):
            self.model = chunk.model
        if self.service_tier is None:
            self.service_tier = getattr(chunk, "service_tier", None)

        chunk_usage = extract_openai_usage_from_chunk(chunk, "chat")
        if chunk_usage:
            merge_usage_stats(self.usage_stats, chunk_usage)

        for choice in getattr(chunk, "choices", None) or []:
            index = getattr(choice, "index", None)
            if not isinstance(index, int):
                index = 0
            view = _ChoiceStream([choice])

            content = extract_openai_content_from_chunk(view, "chat")
            if content is not None:
                self._outputs.setdefault(index, []).append(content)

            chunk_tool_calls = extract_openai_tool_calls_from_chunk(view)
            if chunk_tool_calls:
                accumulate_openai_tool_calls(
                    self._tool_calls.setdefault(index, {}), chunk_tool_calls
                )

            # Like a non-streamed completion, the stop reason is the first choice's.
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason is not None and index == 0:
                self.stop_reason = finish_reason

    @property
    def tool_calls(self) -> Optional[List[Dict[str, Any]]]:
        calls = [
            call
            for index in sorted(self._tool_calls)
            for call in self._tool_calls[index].values()
        ]
        return calls or None

    def _indexes(self) -> List[int]:
        return sorted(set(self._outputs) | set(self._tool_calls))

    def formatted_output(self) -> List[FormattedMessage]:
        """One assistant message with each choice's content in index order, the
        shape a non-streamed completion is captured with."""
        content: List[Any] = []
        for index in self._indexes():
            tool_calls = self._tool_calls.get(index)
            content.extend(
                format_openai_streaming_output(
                    self._outputs.get(index, []),
                    "chat",
                    list(tool_calls.values()) if tool_calls else None,
                )[0]["content"]
            )
        return [{"role": "assistant", "content": content}]


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
    service_tier: Optional[str] = None,
    error: Optional[BaseException] = None,
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
        service_tier=service_tier,
        error=error,
    )
