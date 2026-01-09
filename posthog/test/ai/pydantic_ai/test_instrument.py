"""
Tests for instrument_pydantic_ai function.
"""

from unittest.mock import MagicMock, patch

import pytest

try:
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False
    Agent = None
    InstrumentationSettings = None

pytestmark = pytest.mark.skipif(
    not DEPS_AVAILABLE, reason="pydantic-ai and opentelemetry-sdk are required"
)


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.privacy_mode = False
    return client


class TestInstrumentPydanticAI:
    """Tests for the instrument_pydantic_ai function."""

    def test_basic_instrumentation(self, mock_client):
        from posthog.ai.pydantic_ai import instrument_pydantic_ai

        with patch.object(Agent, "instrument_all") as mock_instrument_all:
            instrument_pydantic_ai(mock_client, distinct_id="user_123")

            mock_instrument_all.assert_called_once()
            settings = mock_instrument_all.call_args[0][0]
            assert isinstance(settings, InstrumentationSettings)

    def test_privacy_mode_disables_content(self):
        from posthog.ai.pydantic_ai import instrument_pydantic_ai

        client = MagicMock()
        client.privacy_mode = True

        with patch.object(Agent, "instrument_all") as mock_instrument_all:
            instrument_pydantic_ai(client)

            settings = mock_instrument_all.call_args[0][0]
            assert settings.include_content is False

    def test_privacy_mode_false_includes_content(self, mock_client):
        from posthog.ai.pydantic_ai import instrument_pydantic_ai

        with patch.object(Agent, "instrument_all") as mock_instrument_all:
            instrument_pydantic_ai(mock_client)

            settings = mock_instrument_all.call_args[0][0]
            assert settings.include_content is True

    def test_tracer_configured_via_settings(self, mock_client):
        from posthog.ai.pydantic_ai import instrument_pydantic_ai

        with patch.object(Agent, "instrument_all") as mock_instrument_all:
            instrument_pydantic_ai(mock_client, distinct_id="user_123")

            settings = mock_instrument_all.call_args[0][0]
            # InstrumentationSettings creates a tracer internally from the provider
            # We verify it's properly configured by checking it has a tracer attribute
            assert hasattr(settings, "tracer")

    def test_accepts_properties(self, mock_client):
        from posthog.ai.pydantic_ai import instrument_pydantic_ai

        with patch.object(Agent, "instrument_all") as mock_instrument_all:
            properties = {"$ai_session_id": "session_123", "custom": "value"}
            instrument_pydantic_ai(mock_client, properties=properties)

            mock_instrument_all.assert_called_once()

    def test_accepts_groups(self, mock_client):
        from posthog.ai.pydantic_ai import instrument_pydantic_ai

        with patch.object(Agent, "instrument_all") as mock_instrument_all:
            groups = {"company": "posthog", "team": "product"}
            instrument_pydantic_ai(mock_client, groups=groups)

            mock_instrument_all.assert_called_once()

    def test_debug_mode(self, mock_client):
        from posthog.ai.pydantic_ai import instrument_pydantic_ai

        with patch.object(Agent, "instrument_all") as mock_instrument_all:
            instrument_pydantic_ai(mock_client, debug=True)

            mock_instrument_all.assert_called_once()
