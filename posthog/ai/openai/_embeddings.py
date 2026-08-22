from typing import Any, Dict, Optional

from ..utils import _capture_ai_event, finalize_ai_content, with_privacy_mode


def _capture_embedding_event(
    *,
    posthog_client: Any,
    base_url: Any,
    response: Any,
    request_kwargs: Dict[str, Any],
    latency: float,
    distinct_id: Optional[str],
    trace_id: str,
    properties: Optional[Dict[str, Any]],
    privacy_mode: bool,
    groups: Optional[Dict[str, Any]],
) -> None:
    """Build and capture telemetry shared by sync and async embedding wrappers."""
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0

    event_properties = {
        "$ai_provider": "openai",
        "$ai_model": request_kwargs.get("model"),
        "$ai_input": with_privacy_mode(
            posthog_client,
            privacy_mode,
            finalize_ai_content(request_kwargs.get("input"), posthog_client),
        ),
        "$ai_http_status": 200,
        "$ai_input_tokens": input_tokens,
        "$ai_latency": latency,
        "$ai_trace_id": trace_id,
        "$ai_base_url": str(base_url),
        **(properties or {}),
    }

    if distinct_id is None:
        event_properties["$process_person_profile"] = False

    if hasattr(posthog_client, "capture"):
        _capture_ai_event(
            posthog_client,
            "$ai_embedding",
            distinct_id=distinct_id or trace_id,
            properties=event_properties,
            groups=groups,
        )
