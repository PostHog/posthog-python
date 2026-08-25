import time
import uuid
from typing import TYPE_CHECKING as _TYPE_CHECKING, Any, Dict, Optional

from posthog.ai.types import TokenUsage as TokenUsage

try:
    import openai
except ImportError:
    raise ModuleNotFoundError(
        "Please install the OpenAI SDK to use this feature: 'pip install openai'"
    )

from posthog.ai.utils import (
    call_llm_and_track_usage,
    extract_available_tool_calls as extract_available_tool_calls,
    finalize_ai_content as finalize_ai_content,
    merge_usage_stats as merge_usage_stats,
    with_privacy_mode as with_privacy_mode,
)
from posthog.ai.openai.openai_converter import (
    accumulate_openai_tool_calls as accumulate_openai_tool_calls,
    extract_openai_content_from_chunk as extract_openai_content_from_chunk,
    extract_openai_tool_calls_from_chunk as extract_openai_tool_calls_from_chunk,
    extract_openai_usage_from_chunk as extract_openai_usage_from_chunk,
    format_openai_streaming_input as _format_openai_streaming_input,
    format_openai_streaming_output as _format_openai_streaming_output,
)
from posthog.client import Client as PostHogClient
from posthog import setup
from posthog.ai.openai._streaming import (
    _ChatCompletionsStreamState,
    _ResponsesStreamState,
    _build_streaming_event_data,
)
from ._embeddings import _capture_embedding_event
from .wrapper_utils import (
    _OpenAIWrapperResource,
    _wrap_openai_resources,
    merge_provider_override,
)


class OpenAI(openai.OpenAI):
    """
    A wrapper around the OpenAI SDK that automatically sends LLM usage events to PostHog.
    """

    _ph_client: PostHogClient

    if _TYPE_CHECKING:
        chat: "WrappedChat"
        embeddings: "WrappedEmbeddings"
        beta: "WrappedBeta"
        responses: "WrappedResponses"

    def __init__(self, posthog_client: Optional[PostHogClient] = None, **kwargs):
        """
        Args:
            posthog_client: If provided, events will be captured via this client
                instead of the global ``posthog`` client.
            **kwargs: Arguments passed to ``openai.OpenAI`` such as ``api_key``
                or ``organization``.
        """

        super().__init__(**kwargs)
        self._ph_client = posthog_client or setup()

        _wrap_openai_resources(self, _SYNC_RESOURCE_WRAPPERS)


def _parse_and_track(
    wrapper,
    posthog_distinct_id: Optional[str],
    posthog_trace_id: Optional[str],
    posthog_properties: Optional[Dict[str, Any]],
    posthog_privacy_mode: bool,
    posthog_groups: Optional[Dict[str, Any]],
    posthog_provider_override: Optional[str] = None,
    **kwargs: Any,
):
    return call_llm_and_track_usage(
        posthog_distinct_id,
        wrapper._client._ph_client,
        "openai",
        posthog_trace_id,
        merge_provider_override(posthog_properties, posthog_provider_override),
        posthog_privacy_mode,
        posthog_groups,
        wrapper._client.base_url,
        wrapper._original.parse,
        **kwargs,
    )


class WrappedResponses(_OpenAIWrapperResource):
    """Wrapper for OpenAI responses that tracks usage in PostHog."""

    def create(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        posthog_provider_override: Optional[str] = None,
        **kwargs: Any,
    ):
        """
        Create an OpenAI Responses API response while tracking usage in PostHog.

        Args:
            posthog_distinct_id: Optional distinct ID to associate with the usage event.
            posthog_trace_id: Optional trace ID. Generated automatically when omitted.
            posthog_properties: Additional properties to include with the usage event.
            posthog_privacy_mode: Whether to redact captured input and output.
            posthog_groups: Optional PostHog groups to associate with the event.
            posthog_provider_override: Optional override for the ``$ai_provider``
                reported on the usage event. Useful when this client is pointed at
                an OpenAI-compatible endpoint (e.g. DeepSeek, Groq) via a custom
                ``base_url``, so cost attribution matches the real provider.
                Defaults to ``"openai"`` when omitted.
            **kwargs: Arguments passed to OpenAI's ``responses.create`` API.

        Returns:
            The OpenAI response, or a streaming iterator when ``stream=True``.
        """
        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        posthog_properties = merge_provider_override(
            posthog_properties, posthog_provider_override
        )

        if kwargs.get("stream", False):
            return self._create_streaming(
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
                **kwargs,
            )

        return call_llm_and_track_usage(
            posthog_distinct_id,
            self._client._ph_client,
            "openai",
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            self._client.base_url,
            self._original.create,
            **kwargs,
        )

    def _create_streaming(
        self,
        posthog_distinct_id: Optional[str],
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool,
        posthog_groups: Optional[Dict[str, Any]],
        **kwargs: Any,
    ):
        start_time = time.time()
        state = _ResponsesStreamState()
        response = self._original.create(**kwargs)

        def generator():
            try:
                for chunk in response:
                    state.process_chunk(chunk)
                    yield chunk
            finally:
                self._capture_streaming_event(
                    posthog_distinct_id,
                    posthog_trace_id,
                    posthog_properties,
                    posthog_privacy_mode,
                    posthog_groups,
                    kwargs,
                    state,
                    time.time() - start_time,
                )

        return generator()

    def _capture_streaming_event(
        self,
        posthog_distinct_id: Optional[str],
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool,
        posthog_groups: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
        state: _ResponsesStreamState,
        latency: float,
    ):
        from posthog.ai.utils import capture_streaming_event

        event_data = _build_streaming_event_data(
            base_url=self._client.base_url,
            kwargs=kwargs,
            formatted_input=_format_openai_streaming_input(kwargs, "responses"),
            formatted_output=_format_openai_streaming_output(state.output, "responses"),
            usage_stats=state.usage_stats,
            latency=latency,
            distinct_id=posthog_distinct_id,
            trace_id=posthog_trace_id,
            properties=posthog_properties,
            privacy_mode=posthog_privacy_mode,
            groups=posthog_groups,
            model_from_response=state.model,
            stop_reason=state.stop_reason,
        )
        capture_streaming_event(self._client._ph_client, event_data)

    def parse(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        posthog_provider_override: Optional[str] = None,
        **kwargs: Any,
    ):
        """
        Parse structured output using OpenAI's 'responses.parse' method, but also track usage in PostHog.

        Args:
            posthog_distinct_id: Optional ID to associate with the usage event.
            posthog_trace_id: Optional trace UUID for linking events.
            posthog_properties: Optional dictionary of extra properties to include in the event.
            posthog_privacy_mode: Whether to anonymize the input and output.
            posthog_groups: Optional dictionary of groups to associate with the event.
            posthog_provider_override: Optional override for the ``$ai_provider``
                reported on the usage event. Useful when this client is pointed at
                an OpenAI-compatible endpoint (e.g. DeepSeek, Groq) via a custom
                ``base_url``, so cost attribution matches the real provider.
                Defaults to ``"openai"`` when omitted.
            **kwargs: Any additional parameters for the OpenAI Responses Parse API.

        Returns:
            The response from OpenAI's responses.parse call.
        """
        return _parse_and_track(
            self,
            posthog_distinct_id,
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            posthog_provider_override,
            **kwargs,
        )


class WrappedChat(_OpenAIWrapperResource):
    """Wrapper for OpenAI chat that tracks usage in PostHog."""

    @property
    def completions(self):
        """Access chat completions with PostHog usage tracking."""
        return WrappedCompletions(self._client, self._original.completions)


class WrappedCompletions(_OpenAIWrapperResource):
    """Wrapper for OpenAI chat completions that tracks usage in PostHog."""

    def parse(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        posthog_provider_override: Optional[str] = None,
        **kwargs: Any,
    ):
        """
        Parse an OpenAI chat completion while tracking usage in PostHog.

        Args:
            posthog_distinct_id: Optional distinct ID to associate with the usage event.
            posthog_trace_id: Optional trace ID. Generated automatically when omitted.
            posthog_properties: Additional properties to include with the usage event.
            posthog_privacy_mode: Whether to redact captured input and output.
            posthog_groups: Optional PostHog groups to associate with the event.
            posthog_provider_override: Optional override for the ``$ai_provider``
                reported on the usage event. Useful when this client is pointed at
                an OpenAI-compatible endpoint (e.g. DeepSeek, Groq) via a custom
                ``base_url``, so cost attribution matches the real provider.
                Defaults to ``"openai"`` when omitted.
            **kwargs: Arguments passed to OpenAI's ``chat.completions.parse`` API.

        Returns:
            The parsed response from OpenAI.
        """
        return _parse_and_track(
            self,
            posthog_distinct_id,
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            posthog_provider_override,
            **kwargs,
        )

    def create(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        posthog_provider_override: Optional[str] = None,
        **kwargs: Any,
    ):
        """
        Create an OpenAI chat completion while tracking usage in PostHog.

        Args:
            posthog_distinct_id: Optional distinct ID to associate with the usage event.
            posthog_trace_id: Optional trace ID. Generated automatically when omitted.
            posthog_properties: Additional properties to include with the usage event.
            posthog_privacy_mode: Whether to redact captured input and output.
            posthog_groups: Optional PostHog groups to associate with the event.
            posthog_provider_override: Optional override for the ``$ai_provider``
                reported on the usage event. Useful when this client is pointed at
                an OpenAI-compatible endpoint (e.g. DeepSeek, Groq) via a custom
                ``base_url``, so cost attribution matches the real provider.
                Defaults to ``"openai"`` when omitted.
            **kwargs: Arguments passed to OpenAI's ``chat.completions.create`` API.

        Returns:
            The OpenAI chat completion, or a streaming iterator when ``stream=True``.
        """
        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        posthog_properties = merge_provider_override(
            posthog_properties, posthog_provider_override
        )

        if kwargs.get("stream", False):
            return self._create_streaming(
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
                **kwargs,
            )

        return call_llm_and_track_usage(
            posthog_distinct_id,
            self._client._ph_client,
            "openai",
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            self._client.base_url,
            self._original.create,
            **kwargs,
        )

    def _create_streaming(
        self,
        posthog_distinct_id: Optional[str],
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool,
        posthog_groups: Optional[Dict[str, Any]],
        **kwargs: Any,
    ):
        start_time = time.time()
        state = _ChatCompletionsStreamState()
        if "stream_options" not in kwargs:
            kwargs["stream_options"] = {}
        kwargs["stream_options"]["include_usage"] = True
        response = self._original.create(**kwargs)

        def generator():
            try:
                for chunk in response:
                    state.process_chunk(chunk)
                    yield chunk
            finally:
                self._capture_streaming_event(
                    posthog_distinct_id,
                    posthog_trace_id,
                    posthog_properties,
                    posthog_privacy_mode,
                    posthog_groups,
                    kwargs,
                    state,
                    time.time() - start_time,
                )

        return generator()

    def _capture_streaming_event(
        self,
        posthog_distinct_id: Optional[str],
        posthog_trace_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool,
        posthog_groups: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
        state: _ChatCompletionsStreamState,
        latency: float,
    ):
        from posthog.ai.utils import capture_streaming_event

        event_data = _build_streaming_event_data(
            base_url=self._client.base_url,
            kwargs=kwargs,
            formatted_input=_format_openai_streaming_input(kwargs, "chat"),
            formatted_output=_format_openai_streaming_output(
                state.output, "chat", state.tool_calls
            ),
            usage_stats=state.usage_stats,
            latency=latency,
            distinct_id=posthog_distinct_id,
            trace_id=posthog_trace_id,
            properties=posthog_properties,
            privacy_mode=posthog_privacy_mode,
            groups=posthog_groups,
            model_from_response=state.model,
            stop_reason=state.stop_reason,
        )
        capture_streaming_event(self._client._ph_client, event_data)


class WrappedEmbeddings(_OpenAIWrapperResource):
    """Wrapper for OpenAI embeddings that tracks usage in PostHog."""

    def create(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        posthog_provider_override: Optional[str] = None,
        **kwargs: Any,
    ):
        """
        Create an embedding using OpenAI's 'embeddings.create' method, but also track usage in PostHog.

        Args:
            posthog_distinct_id: Optional ID to associate with the usage event.
            posthog_trace_id: Optional trace UUID for linking events.
            posthog_properties: Optional dictionary of extra properties to include in the event.
            posthog_privacy_mode: Whether to anonymize the input and output.
            posthog_groups: Optional dictionary of groups to associate with the event.
            posthog_provider_override: Optional override for the ``$ai_provider``
                reported on the usage event. Useful when this client is pointed at
                an OpenAI-compatible endpoint (e.g. DeepSeek, Groq) via a custom
                ``base_url``, so cost attribution matches the real provider.
                Defaults to ``"openai"`` when omitted.
            **kwargs: Any additional parameters for the OpenAI Embeddings API.

        Returns:
            The response from OpenAI's embeddings.create call.
        """

        if posthog_trace_id is None:
            posthog_trace_id = str(uuid.uuid4())

        posthog_properties = merge_provider_override(
            posthog_properties, posthog_provider_override
        )

        start_time = time.time()
        response = self._original.create(**kwargs)
        end_time = time.time()

        _capture_embedding_event(
            posthog_client=self._client._ph_client,
            base_url=self._client.base_url,
            response=response,
            request_kwargs=kwargs,
            latency=end_time - start_time,
            distinct_id=posthog_distinct_id,
            trace_id=posthog_trace_id,
            properties=posthog_properties,
            privacy_mode=posthog_privacy_mode,
            groups=posthog_groups,
        )
        return response


class WrappedBeta(_OpenAIWrapperResource):
    """Wrapper for OpenAI beta features that tracks usage in PostHog."""

    @property
    def chat(self):
        """Access beta chat APIs with PostHog usage tracking."""
        return WrappedBetaChat(self._client, self._original.chat)


class WrappedBetaChat(_OpenAIWrapperResource):
    """Wrapper for OpenAI beta chat that tracks usage in PostHog."""

    @property
    def completions(self):
        """Access beta chat completions with PostHog usage tracking."""
        return WrappedBetaCompletions(self._client, self._original.completions)


class WrappedBetaCompletions(_OpenAIWrapperResource):
    """Wrapper for OpenAI beta chat completions that tracks usage in PostHog."""

    def parse(
        self,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        posthog_provider_override: Optional[str] = None,
        **kwargs: Any,
    ):
        """
        Parse an OpenAI beta chat completion while tracking usage in PostHog.

        Args:
            posthog_distinct_id: Optional distinct ID to associate with the usage event.
            posthog_trace_id: Optional trace ID. Generated automatically when omitted.
            posthog_properties: Additional properties to include with the usage event.
            posthog_privacy_mode: Whether to redact captured input and output.
            posthog_groups: Optional PostHog groups to associate with the event.
            posthog_provider_override: Optional override for the ``$ai_provider``
                reported on the usage event. Useful when this client is pointed at
                an OpenAI-compatible endpoint (e.g. DeepSeek, Groq) via a custom
                ``base_url``, so cost attribution matches the real provider.
                Defaults to ``"openai"`` when omitted.
            **kwargs: Arguments passed to OpenAI's beta ``chat.completions.parse`` API.

        Returns:
            The parsed response from OpenAI.
        """
        return _parse_and_track(
            self,
            posthog_distinct_id,
            posthog_trace_id,
            posthog_properties,
            posthog_privacy_mode,
            posthog_groups,
            posthog_provider_override,
            **kwargs,
        )


_SYNC_RESOURCE_WRAPPERS = {
    "chat": WrappedChat,
    "embeddings": WrappedEmbeddings,
    "beta": WrappedBeta,
    "responses": WrappedResponses,
}
