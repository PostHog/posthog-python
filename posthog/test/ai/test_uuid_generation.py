from unittest.mock import MagicMock
from uuid import UUID

from posthog.ai.utils import capture_streaming_event


def test_capture_streaming_event_generates_uuid_v7_trace_id_when_missing():
    ph_client = MagicMock()

    capture_streaming_event(
        ph_client,
        {
            "provider": "openai",
            "model": "gpt-test",
            "base_url": "https://api.openai.com/v1",
            "kwargs": {},
            "formatted_input": [],
            "formatted_output": [],
            "usage_stats": {},
            "latency": 0.1,
            "distinct_id": None,
            "trace_id": None,
            "properties": None,
            "privacy_mode": False,
            "groups": None,
            "stop_reason": None,
        },
    )

    capture_kwargs = ph_client.capture.call_args.kwargs
    trace_id = capture_kwargs["properties"]["$ai_trace_id"]
    assert UUID(trace_id).version == 7
    assert capture_kwargs["distinct_id"] == trace_id
    assert capture_kwargs["properties"]["$process_person_profile"] is False
