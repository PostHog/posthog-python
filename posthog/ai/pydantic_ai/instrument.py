"""
Pydantic AI instrumentation for PostHog.

Provides a simple one-liner to instrument all Pydantic AI agents with PostHog tracing.
"""

from typing import Any, Dict, Optional

from posthog.client import Client as PostHogClient


def instrument_pydantic_ai(
    client: PostHogClient,
    distinct_id: Optional[str] = None,
    privacy_mode: Optional[bool] = None,
    properties: Optional[Dict[str, Any]] = None,
    groups: Optional[Dict[str, Any]] = None,
    debug: bool = False,
) -> None:
    """
    Instrument all Pydantic AI agents with PostHog tracing.

    This function sets up OpenTelemetry tracing for Pydantic AI and routes
    all spans to PostHog as AI events ($ai_generation, $ai_trace, $ai_span).

    Usage:
        from posthog import Posthog
        from posthog.ai.pydantic_ai import instrument_pydantic_ai
        from pydantic_ai import Agent

        posthog = Posthog(api_key="...", host="...")
        instrument_pydantic_ai(posthog, distinct_id="user_123")

        # Now use Pydantic AI normally - all traces go to PostHog
        agent = Agent('openai:gpt-4')
        result = await agent.run('Hello!')

    Args:
        client: PostHog client instance for sending events
        distinct_id: Default distinct ID for all events. If not provided,
            events will use the trace ID as distinct_id.
        privacy_mode: If True, message content will be redacted from events.
            If not specified, inherits from client.privacy_mode.
        properties: Additional properties to include in all events
        groups: PostHog groups to associate with all events
        debug: Enable debug logging for troubleshooting

    Raises:
        ImportError: If pydantic-ai or opentelemetry-sdk is not installed
    """
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings
    except ImportError as e:
        raise ImportError(
            "pydantic-ai is required for Pydantic AI instrumentation. "
            "Install it with: pip install pydantic-ai"
        ) from e

    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        raise ImportError(
            "opentelemetry-sdk is required for Pydantic AI instrumentation. "
            "Install it with: pip install opentelemetry-sdk"
        ) from e

    from posthog.ai.pydantic_ai.exporter import PydanticAISpanExporter

    # Resolve privacy_mode from client if not explicitly set
    if privacy_mode is None:
        privacy_mode = getattr(client, "privacy_mode", False)

    # Create the Pydantic AI-specific exporter (handles message format normalization)
    exporter = PydanticAISpanExporter(
        client=client,
        distinct_id=distinct_id,
        privacy_mode=privacy_mode,
        properties=properties,
        groups=groups,
        debug=debug,
    )

    # Create a TracerProvider with our exporter
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Configure Pydantic AI instrumentation settings
    settings = InstrumentationSettings(
        tracer_provider=provider,
        include_content=not privacy_mode,
    )

    # Apply instrumentation globally to all agents
    Agent.instrument_all(settings)
