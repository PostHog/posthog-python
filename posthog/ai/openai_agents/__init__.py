from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Union

if TYPE_CHECKING:
    from agents.tracing import Trace

    from posthog.client import Client

try:
    import agents  # noqa: F401
except ImportError:
    raise ModuleNotFoundError(
        "Please install the OpenAI Agents SDK to use this feature: 'pip install openai-agents'"
    )

from posthog.ai.openai_agents.processor import PostHogTracingProcessor

__all__ = ["PostHogTracingProcessor", "instrument"]


def instrument(
    client: Optional[Client] = None,
    distinct_id: Optional[Union[str, Callable[[Trace], Optional[str]]]] = None,
    privacy_mode: bool = False,
    groups: Optional[Dict[str, Any]] = None,
    properties: Optional[Dict[str, Any]] = None,
) -> PostHogTracingProcessor:
    """
    One-liner to instrument OpenAI Agents SDK with PostHog tracing.

    This registers a PostHogTracingProcessor with the OpenAI Agents SDK,
    automatically capturing traces, spans, and LLM generations.

    Args:
        client: Optional PostHog client instance. If not provided, uses the default client.
        distinct_id: Optional default distinct ID for all traces. Only suitable
            as a static value when one process serves one user (a CLI or
            worker); servers should pass identity per run via
            ``RunConfig(trace_metadata={"posthog_distinct_id": ...})``, which
            takes precedence. Can also be a callable that takes a trace and
            returns a distinct ID.
        privacy_mode: If True, redacts input/output content from events.
        groups: Optional PostHog groups to associate with events.
        properties: Optional additional properties to include with all events.
            Per-run properties can be passed via
            ``RunConfig(trace_metadata={"posthog_properties": {...}})`` and
            override these defaults.

    Returns:
        PostHogTracingProcessor: The registered processor instance.

    Example:
        ```python
        from posthog.ai.openai_agents import instrument

        # One-user process (CLI/worker): a static distinct ID is fine
        instrument(distinct_id="user@example.com")

        # Server: pass identity and session per run instead
        instrument()

        from agents import Agent, Runner, RunConfig
        agent = Agent(name="Assistant", instructions="You are helpful.")
        result = Runner.run_sync(
            agent,
            "Hello!",
            run_config=RunConfig(
                group_id=conversation_id,  # becomes $ai_session_id
                trace_metadata={
                    "posthog_distinct_id": user_id,
                    "posthog_properties": {"plan": "scale"},
                },
            ),
        )
        ```
    """
    from agents.tracing import add_trace_processor

    processor = PostHogTracingProcessor(
        client=client,
        distinct_id=distinct_id,
        privacy_mode=privacy_mode,
        groups=groups,
        properties=properties,
    )
    add_trace_processor(processor)
    return processor
