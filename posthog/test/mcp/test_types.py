from unittest.mock import Mock

import pytest

from posthog.mcp.types import MCPAnalyticsOptions, PreparedToolCall, UserIdentity


@pytest.mark.parametrize("capture_model", [False, True])
def test_analytics_options_preserve_existing_positional_arguments(capture_model):
    logger = Mock()
    identity = UserIdentity("user-123")
    intent_fallback = Mock(return_value="searching")
    before_send = Mock(return_value=None)
    event_properties = Mock(return_value={"team": "support"})

    options = MCPAnalyticsOptions(
        logger,
        True,
        "report_missing",
        True,
        False,
        False,
        identity,
        intent_fallback,
        before_send,
        event_properties,
        capture_model=capture_model,
    )

    assert options == MCPAnalyticsOptions(
        logger=logger,
        report_missing=True,
        missing_capability_tool_name="report_missing",
        enable_conversation_id=True,
        enable_exception_autocapture=False,
        context=False,
        identify=identity,
        intent_fallback=intent_fallback,
        before_send=before_send,
        event_properties=event_properties,
        capture_model=capture_model,
    )


@pytest.mark.parametrize("is_missing_capability", [False, True])
def test_prepared_tool_call_preserves_existing_positional_arguments(
    is_missing_capability,
):
    call = PreparedToolCall(
        {"query": "docs"}, "searching", "context_parameter", is_missing_capability
    )

    assert call == PreparedToolCall(
        args={"query": "docs"},
        intent="searching",
        intent_source="context_parameter",
        is_missing_capability=is_missing_capability,
    )
    assert call.llm_model is None
    assert call.llm_model_source is None
