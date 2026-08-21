import os
import uuid
from typing import Any, Dict, Optional

try:
    from google import genai
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Google Gemini SDK to use this feature: 'pip install google-genai'"
    )

from ... import setup
from ...client import Client as PostHogClient
from ..types import StreamingEventData, TokenUsage
from ..utils import (
    _capture_ai_event,
    capture_streaming_event,
    finalize_ai_content,
    merge_system_prompt,
    with_privacy_mode,
)
from .gemini_converter import (
    extract_gemini_embedding_token_count,
    format_gemini_streaming_output,
)

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"


def _resolve_posthog_client(
    posthog_client: Optional[PostHogClient],
) -> PostHogClient:
    client = posthog_client or setup()
    if client is None:
        raise ValueError("posthog_client is required for PostHog tracking")
    return client


def _build_gemini_client_args(
    *,
    api_key: Optional[str],
    vertexai: Optional[bool],
    credentials: Optional[Any],
    project: Optional[str],
    location: Optional[str],
    debug_config: Optional[Any],
    http_options: Optional[Any],
) -> Dict[str, Any]:
    """Build provider client arguments while preserving Gemini auth precedence."""
    client_args: Dict[str, Any] = {}

    optional_args = {
        "vertexai": vertexai,
        "credentials": credentials,
        "project": project,
        "location": location,
        "debug_config": debug_config,
        "http_options": http_options,
    }
    client_args.update(
        {name: value for name, value in optional_args.items() if value is not None}
    )

    if vertexai:
        if api_key is not None:
            client_args["api_key"] = api_key
        return client_args

    resolved_api_key = api_key
    if resolved_api_key is None:
        resolved_api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("API_KEY")
    if resolved_api_key is None:
        raise ValueError(
            "API key must be provided either as parameter or via GOOGLE_API_KEY/API_KEY environment variable"
        )

    client_args["api_key"] = resolved_api_key
    return client_args


class _GeminiModelsPolicy:
    """Shared telemetry policy for the explicit sync and async Gemini adapters."""

    _ph_client: PostHogClient

    def _initialize_policy(
        self,
        *,
        api_key: Optional[str],
        vertexai: Optional[bool],
        credentials: Optional[Any],
        project: Optional[str],
        location: Optional[str],
        debug_config: Optional[Any],
        http_options: Optional[Any],
        posthog_client: Optional[PostHogClient],
        posthog_distinct_id: Optional[str],
        posthog_properties: Optional[Dict[str, Any]],
        posthog_privacy_mode: bool,
        posthog_groups: Optional[Dict[str, Any]],
    ) -> None:
        self._ph_client = _resolve_posthog_client(posthog_client)
        self._default_distinct_id = posthog_distinct_id
        self._default_properties = posthog_properties or {}
        self._default_privacy_mode = posthog_privacy_mode
        self._default_groups = posthog_groups

        client_args = _build_gemini_client_args(
            api_key=api_key,
            vertexai=vertexai,
            credentials=credentials,
            project=project,
            location=location,
            debug_config=debug_config,
            http_options=http_options,
        )
        self._client = genai.Client(**client_args)
        self._base_url = _GEMINI_BASE_URL

    def _merge_posthog_params(
        self,
        call_distinct_id: Optional[str],
        call_trace_id: Optional[str],
        call_properties: Optional[Dict[str, Any]],
        call_privacy_mode: Optional[bool],
        call_groups: Optional[Dict[str, Any]],
    ):
        """Merge call-level PostHog parameters with client defaults."""
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

        properties = dict(self._default_properties)
        if call_properties:
            properties.update(call_properties)

        trace_id = call_trace_id if call_trace_id is not None else str(uuid.uuid4())
        return distinct_id, trace_id, properties, privacy_mode, groups

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
        usage_stats: TokenUsage,
        latency: float,
        output: Any,
        stop_reason: Optional[str] = None,
    ) -> None:
        formatted_input = merge_system_prompt(
            {"contents": contents, **kwargs}, "gemini"
        )
        event_data = StreamingEventData(
            provider="gemini",
            model=model,
            base_url=self._base_url,
            kwargs=kwargs,
            formatted_input=formatted_input,
            formatted_output=format_gemini_streaming_output(output),
            usage_stats=usage_stats,
            latency=latency,
            distinct_id=distinct_id,
            trace_id=trace_id,
            properties=properties,
            privacy_mode=privacy_mode,
            groups=groups,
            stop_reason=stop_reason,
        )
        capture_streaming_event(self._ph_client, event_data)

    def _capture_embedding_outcome(
        self,
        *,
        model: str,
        contents: Any,
        distinct_id: Optional[str],
        trace_id: str,
        properties: Optional[Dict[str, Any]],
        privacy_mode: bool,
        groups: Optional[Dict[str, Any]],
        response: Any,
        error: Optional[Exception],
        latency: float,
    ) -> None:
        input_tokens = extract_gemini_embedding_token_count(response) if response else 0
        event_properties = {
            "$ai_provider": "gemini",
            "$ai_model": model,
            "$ai_input": with_privacy_mode(
                self._ph_client,
                privacy_mode,
                finalize_ai_content(contents, self._ph_client),
            ),
            "$ai_http_status": (
                getattr(error, "status_code", 0) if error is not None else 200
            ),
            "$ai_input_tokens": input_tokens,
            "$ai_latency": latency,
            "$ai_trace_id": trace_id,
            "$ai_base_url": self._base_url,
            **(properties or {}),
        }

        if error:
            event_properties["$ai_is_error"] = True
            event_properties["$ai_error"] = str(error)

        if distinct_id is None:
            event_properties["$process_person_profile"] = False

        _capture_ai_event(
            self._ph_client,
            "$ai_embedding",
            distinct_id=distinct_id or trace_id,
            properties=event_properties,
            groups=groups,
        )
