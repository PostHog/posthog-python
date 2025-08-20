import os
import time
import uuid
from typing import Any, Dict, Optional

try:
    from google import genai
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Google Gemini SDK to use this feature: 'pip install google-genai'"
    )

from posthog import setup
from posthog.ai.utils import (
    call_llm_and_track_usage,
    get_model_params,
    with_privacy_mode,
)
from posthog.client import Client as PostHogClient


class Client:
    """
    A drop-in replacement for genai.Client that automatically sends LLM usage events to PostHog.

    Usage:
        client = Client(
            api_key="your_api_key",
            posthog_client=posthog_client,
            posthog_distinct_id="default_user",  # Optional defaults
            posthog_properties={"team": "ai"}    # Optional defaults
        )
        response = client.models.generate_content(
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
        self._ph_client = posthog_client or setup()

        if self._ph_client is None:
            raise ValueError("posthog_client is required for PostHog tracking")

        self.models = Models(
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


class Models:
    """
    Models interface that mimics genai.Client().models with PostHog tracking.
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
        self._ph_client = posthog_client or setup()

        if self._ph_client is None:
            raise ValueError("posthog_client is required for PostHog tracking")

        # Store default PostHog settings
        self._default_distinct_id = posthog_distinct_id
        self._default_properties = posthog_properties or {}
        self._default_privacy_mode = posthog_privacy_mode
        self._default_groups = posthog_groups

        # Build genai.Client arguments
        client_args: Dict[str, Any] = {}

        # Add Vertex AI parameters if provided
        if vertexai is not None:
            client_args["vertexai"] = vertexai
        if credentials is not None:
            client_args["credentials"] = credentials
        if project is not None:
            client_args["project"] = project
        if location is not None:
            client_args["location"] = location
        if debug_config is not None:
            client_args["debug_config"] = debug_config
        if http_options is not None:
            client_args["http_options"] = http_options

        # Handle API key authentication
        if vertexai:
            # For Vertex AI, api_key is optional
            if api_key is not None:
                client_args["api_key"] = api_key
        else:
            # For non-Vertex AI mode, api_key is required (backwards compatibility)
            if api_key is None:
                api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("API_KEY")

            if api_key is None:
                raise ValueError(
                    "API key must be provided either as parameter or via GOOGLE_API_KEY/API_KEY environment variable"
                )
            client_args["api_key"] = api_key

        self._client = genai.Client(**client_args)
        self._base_url = "https://generativelanguage.googleapis.com"

    def _merge_posthog_params(
        self,
        call_distinct_id: Optional[str],
        call_trace_id: Optional[str],
        call_properties: Optional[Dict[str, Any]],
        call_privacy_mode: Optional[bool],
        call_groups: Optional[Dict[str, Any]],
    ):
        """Merge call-level PostHog parameters with client defaults."""
        # Use call-level values if provided, otherwise fall back to defaults
        distinct_id = (
            call_distinct_id
            if call_distinct_id is not None
            else self._default_distinct_id
        )
        privacy_mode = (
            call_privacy_mode
            if call_privacy_mode is not None
            else self._default_privacy_mode
        )
        groups = call_groups if call_groups is not None else self._default_groups

        # Merge properties: default properties + call properties (call properties override)
        properties = dict(self._default_properties)
        if call_properties:
            properties.update(call_properties)

        if call_trace_id is None:
            call_trace_id = str(uuid.uuid4())

        return distinct_id, call_trace_id, properties, privacy_mode, groups

    def generate_content(
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

        This method signature exactly matches genai.Client().models.generate_content()
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

        return call_llm_and_track_usage(
            distinct_id,
            self._ph_client,
            "gemini",
            trace_id,
            properties,
            privacy_mode,
            groups,
            self._base_url,
            self._client.models.generate_content,
            **kwargs_with_contents,
        )

    def _generate_content_streaming(
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
        usage_stats: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        accumulated_content = []

        kwargs_without_stream = {"model": model, "contents": contents, **kwargs}
        response = self._client.models.generate_content_stream(**kwargs_without_stream)

        def generator():
            nonlocal usage_stats
            nonlocal accumulated_content  # noqa: F824
            try:
                for chunk in response:
                    if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                        usage_stats = {
                            "input_tokens": getattr(
                                chunk.usage_metadata, "prompt_token_count", 0
                            ),
                            "output_tokens": getattr(
                                chunk.usage_metadata, "candidates_token_count", 0
                            ),
                        }

                    if hasattr(chunk, "text") and chunk.text:
                        accumulated_content.append(chunk.text)

                    yield chunk

            finally:
                end_time = time.time()
                latency = end_time - start_time
                output = "".join(accumulated_content)

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
                    output,
                )

        return generator()

    def _capture_streaming_event(
        self,
        model: str,
        contents,
        distinct_id: Optional[str],
        trace_id: Optional[str],
        properties: Optional[Dict[str, Any]],
        privacy_mode: bool,
        groups: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
        usage_stats: Dict[str, int],
        latency: float,
        output: str,
    ):
        if trace_id is None:
            trace_id = str(uuid.uuid4())

        event_properties = {
            "$ai_provider": "gemini",
            "$ai_model": model,
            "$ai_model_parameters": get_model_params(kwargs),
            "$ai_input": with_privacy_mode(
                self._ph_client,
                privacy_mode,
                self._format_input(contents),
            ),
            "$ai_output_choices": with_privacy_mode(
                self._ph_client,
                privacy_mode,
                [{"content": output, "role": "assistant"}],
            ),
            "$ai_http_status": 200,
            "$ai_input_tokens": usage_stats.get("input_tokens", 0),
            "$ai_output_tokens": usage_stats.get("output_tokens", 0),
            "$ai_latency": latency,
            "$ai_trace_id": trace_id,
            "$ai_base_url": self._base_url,
            **(properties or {}),
        }

        if distinct_id is None:
            event_properties["$process_person_profile"] = False

        if hasattr(self._ph_client, "capture"):
            self._ph_client.capture(
                distinct_id=distinct_id,
                event="$ai_generation",
                properties=event_properties,
                groups=groups,
            )

    def _format_input(self, contents):
        """Format input contents for PostHog tracking"""
        if isinstance(contents, str):
            return [{"role": "user", "content": contents}]
        elif isinstance(contents, list):
            formatted = []
            for item in contents:
                if isinstance(item, str):
                    formatted.append({"role": "user", "content": item})
                elif hasattr(item, "text"):
                    formatted.append({"role": "user", "content": item.text})
                else:
                    formatted.append({"role": "user", "content": str(item)})
            return formatted
        else:
            return [{"role": "user", "content": str(contents)}]

    def generate_content_stream(
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

        return self._generate_content_streaming(
            model,
            contents,
            distinct_id,
            trace_id,
            properties,
            privacy_mode,
            groups,
            **kwargs,
        )
