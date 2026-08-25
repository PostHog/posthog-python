import time
from typing import Any, Dict, Optional

from ... import setup as setup
from ...client import Client as PostHogClient
from ..stream import AsyncStreamWrapper
from ..types import StreamingEventData as StreamingEventData
from ..types import TokenUsage
from ..utils import (
    call_llm_and_track_usage_async,
    capture_streaming_event as capture_streaming_event,
    finalize_ai_content as finalize_ai_content,
    merge_system_prompt as merge_system_prompt,
    merge_usage_stats,
    with_privacy_mode as with_privacy_mode,
)
from ._shared import _GeminiModelsPolicy, _resolve_posthog_client
from .gemini_converter import (
    extract_gemini_content_from_chunk,
    extract_gemini_embedding_token_count as extract_gemini_embedding_token_count,
    extract_gemini_stop_reason_from_chunk,
    extract_gemini_usage_from_chunk,
    format_gemini_streaming_output as format_gemini_streaming_output,
)


class AsyncClient:
    """
    An async drop-in replacement for genai.Client that automatically sends LLM usage events to PostHog.

    Usage:
        client = AsyncClient(
            api_key="your_api_key",
            posthog_client=posthog_client,
            posthog_distinct_id="default_user",  # Optional defaults
            posthog_properties={"team": "ai"}    # Optional defaults
        )
        response = await client.models.generate_content(
            model="gemini-2.0-flash",
            contents=["Hello world"],
            posthog_distinct_id="specific_user"  # Override default
        )
    """

    _ph_client: PostHogClient

    def __init__(
        self,
        api_key: Optional[str] = None,
        vertexai: Optional[bool] = None,
        credentials: Optional[Any] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        debug_config: Optional[Any] = None,
        http_options: Optional[Any] = None,
        posthog_client: Optional[PostHogClient] = None,
        posthog_distinct_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        """
        Args:
            api_key: Google AI API key. If not provided, will use GOOGLE_API_KEY or API_KEY environment variable (not required for Vertex AI)
            vertexai: Whether to use Vertex AI authentication
            credentials: Vertex AI credentials object
            project: GCP project ID for Vertex AI
            location: GCP location for Vertex AI
            debug_config: Debug configuration for the client
            http_options: HTTP options for the client
            posthog_client: PostHog client for tracking usage
            posthog_distinct_id: Default distinct ID for all calls (can be overridden per call)
            posthog_properties: Default properties for all calls (can be overridden per call)
            posthog_privacy_mode: Default privacy mode for all calls (can be overridden per call)
            posthog_groups: Default groups for all calls (can be overridden per call)
            **kwargs: Additional arguments (for future compatibility)
        """

        self._ph_client = _resolve_posthog_client(posthog_client)

        self.models = AsyncModels(
            api_key=api_key,
            vertexai=vertexai,
            credentials=credentials,
            project=project,
            location=location,
            debug_config=debug_config,
            http_options=http_options,
            posthog_client=self._ph_client,
            posthog_distinct_id=posthog_distinct_id,
            posthog_properties=posthog_properties,
            posthog_privacy_mode=posthog_privacy_mode,
            posthog_groups=posthog_groups,
            **kwargs,
        )


class AsyncModels(_GeminiModelsPolicy):
    """
    Async Models interface that mimics genai.Client().aio.models with PostHog tracking.
    """

    _ph_client: PostHogClient  # Not None after __init__ validation

    def __init__(
        self,
        api_key: Optional[str] = None,
        vertexai: Optional[bool] = None,
        credentials: Optional[Any] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        debug_config: Optional[Any] = None,
        http_options: Optional[Any] = None,
        posthog_client: Optional[PostHogClient] = None,
        posthog_distinct_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: bool = False,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        """
        Args:
            api_key: Google AI API key. If not provided, will use GOOGLE_API_KEY or API_KEY environment variable (not required for Vertex AI)
            vertexai: Whether to use Vertex AI authentication
            credentials: Vertex AI credentials object
            project: GCP project ID for Vertex AI
            location: GCP location for Vertex AI
            debug_config: Debug configuration for the client
            http_options: HTTP options for the client
            posthog_client: PostHog client for tracking usage
            posthog_distinct_id: Default distinct ID for all calls
            posthog_properties: Default properties for all calls
            posthog_privacy_mode: Default privacy mode for all calls
            posthog_groups: Default groups for all calls
            **kwargs: Additional arguments (for future compatibility)
        """

        self._initialize_policy(
            api_key=api_key,
            vertexai=vertexai,
            credentials=credentials,
            project=project,
            location=location,
            debug_config=debug_config,
            http_options=http_options,
            posthog_client=posthog_client,
            posthog_distinct_id=posthog_distinct_id,
            posthog_properties=posthog_properties,
            posthog_privacy_mode=posthog_privacy_mode,
            posthog_groups=posthog_groups,
        )

    async def generate_content(
        self,
        model: str,
        contents,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: Optional[bool] = None,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """
        Generate content using Gemini's API while tracking usage in PostHog.

        This method signature exactly matches genai.Client().aio.models.generate_content()
        with additional PostHog tracking parameters.

        Args:
            model: The model to use (e.g., 'gemini-2.0-flash')
            contents: The input content for generation
            posthog_distinct_id: ID to associate with the usage event (overrides client default)
            posthog_trace_id: Trace UUID for linking events (auto-generated if not provided)
            posthog_properties: Extra properties to include in the event (merged with client defaults)
            posthog_privacy_mode: Whether to redact sensitive information (overrides client default)
            posthog_groups: Group analytics properties (overrides client default)
            **kwargs: Arguments passed to Gemini's generate_content
        """

        # Merge PostHog parameters
        distinct_id, trace_id, properties, privacy_mode, groups = (
            self._merge_posthog_params(
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
            )
        )

        kwargs_with_contents = {"model": model, "contents": contents, **kwargs}

        return await call_llm_and_track_usage_async(
            distinct_id,
            self._ph_client,
            "gemini",
            trace_id,
            properties,
            privacy_mode,
            groups,
            self._base_url,
            self._client.aio.models.generate_content,
            **kwargs_with_contents,
        )

    async def _generate_content_streaming(
        self,
        model: str,
        contents,
        distinct_id: Optional[str],
        trace_id: Optional[str],
        properties: Optional[Dict[str, Any]],
        privacy_mode: bool,
        groups: Optional[Dict[str, Any]],
        **kwargs: Any,
    ):
        start_time = time.time()
        usage_stats: TokenUsage = TokenUsage(input_tokens=0, output_tokens=0)
        accumulated_content = []
        stop_reason: Optional[str] = None

        kwargs_without_stream = {"model": model, "contents": contents, **kwargs}
        response = await self._client.aio.models.generate_content_stream(
            **kwargs_without_stream
        )

        async def async_generator():
            nonlocal usage_stats
            nonlocal accumulated_content
            nonlocal stop_reason

            try:
                async for chunk in response:
                    # Extract usage stats from chunk
                    chunk_usage = extract_gemini_usage_from_chunk(chunk)

                    if chunk_usage:
                        # Gemini reports cumulative totals, not incremental values
                        merge_usage_stats(usage_stats, chunk_usage, mode="cumulative")

                    # Extract content from chunk (now returns content blocks)
                    content_blocks = extract_gemini_content_from_chunk(chunk)

                    if content_blocks is not None:
                        accumulated_content.extend(content_blocks)

                    # Extract stop reason from chunk
                    chunk_stop_reason = extract_gemini_stop_reason_from_chunk(chunk)
                    if chunk_stop_reason is not None:
                        stop_reason = chunk_stop_reason

                    yield chunk

            finally:
                end_time = time.time()
                latency = end_time - start_time

                self._capture_streaming_event(
                    model,
                    contents,
                    distinct_id,
                    trace_id,
                    properties,
                    privacy_mode,
                    groups,
                    kwargs,
                    usage_stats,
                    latency,
                    accumulated_content,
                    stop_reason=stop_reason,
                )

        return AsyncStreamWrapper(async_generator(), stream=response)

    async def generate_content_stream(
        self,
        model: str,
        contents,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: Optional[bool] = None,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """
        Stream content from Gemini asynchronously while tracking usage in PostHog.

        Args:
            model: The Gemini model to use.
            contents: Input content for generation.
            posthog_distinct_id: Optional distinct ID, overriding the client default.
            posthog_trace_id: Optional trace ID. Generated automatically when omitted.
            posthog_properties: Additional properties merged with client defaults.
            posthog_privacy_mode: Whether to redact captured input and output,
                overriding the client default.
            posthog_groups: Optional PostHog groups, overriding the client default.
            **kwargs: Arguments passed to Gemini's async ``generate_content_stream`` API.

        Returns:
            An async streaming iterator yielding Gemini chunks.
        """
        # Merge PostHog parameters
        distinct_id, trace_id, properties, privacy_mode, groups = (
            self._merge_posthog_params(
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
            )
        )

        return await self._generate_content_streaming(
            model,
            contents,
            distinct_id,
            trace_id,
            properties,
            privacy_mode,
            groups,
            **kwargs,
        )

    async def embed_content(
        self,
        model: str,
        contents,
        posthog_distinct_id: Optional[str] = None,
        posthog_trace_id: Optional[str] = None,
        posthog_properties: Optional[Dict[str, Any]] = None,
        posthog_privacy_mode: Optional[bool] = None,
        posthog_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """
        Create embeddings using Gemini's API while tracking usage in PostHog.

        Args:
            model: The model to use (e.g., 'gemini-embedding-001')
            contents: The input content for embedding
            posthog_distinct_id: ID to associate with the usage event (overrides client default)
            posthog_trace_id: Trace UUID for linking events (auto-generated if not provided)
            posthog_properties: Extra properties to include in the event (merged with client defaults)
            posthog_privacy_mode: Whether to redact sensitive information (overrides client default)
            posthog_groups: Group analytics properties (overrides client default)
            **kwargs: Arguments passed to Gemini's embed_content (e.g., config)
        """
        distinct_id, trace_id, properties, privacy_mode, groups = (
            self._merge_posthog_params(
                posthog_distinct_id,
                posthog_trace_id,
                posthog_properties,
                posthog_privacy_mode,
                posthog_groups,
            )
        )

        start_time = time.time()
        response = None
        error = None

        try:
            response = await self._client.aio.models.embed_content(
                model=model, contents=contents, **kwargs
            )
        except Exception as exc:
            error = exc
        finally:
            self._capture_embedding_outcome(
                model=model,
                contents=contents,
                distinct_id=distinct_id,
                trace_id=trace_id,
                properties=properties,
                privacy_mode=privacy_mode,
                groups=groups,
                response=response,
                error=error,
                latency=time.time() - start_time,
            )

        if error:
            raise error

        return response
