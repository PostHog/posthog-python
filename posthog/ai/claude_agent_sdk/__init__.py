from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Union

if TYPE_CHECKING:
    from claude_agent_sdk.types import ClaudeAgentOptions, ResultMessage

    from posthog.client import Client

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    raise ModuleNotFoundError(
        "Please install the Claude Agent SDK to use this feature: 'pip install claude-agent-sdk'"
    )

from posthog.ai.claude_agent_sdk.client import PostHogClaudeSDKClient
from posthog.ai.claude_agent_sdk.processor import PostHogClaudeAgentProcessor

__all__ = [
    "PostHogClaudeAgentProcessor",
    "PostHogClaudeSDKClient",
    "instrument",
    "query",
]


def instrument(
    client: Optional[Client] = None,
    distinct_id: Optional[Union[str, Callable[[ResultMessage], Optional[str]]]] = None,
    privacy_mode: bool = False,
    groups: Optional[Dict[str, Any]] = None,
    properties: Optional[Dict[str, Any]] = None,
) -> PostHogClaudeAgentProcessor:
    """
    Create a PostHog-instrumented query wrapper for the Claude Agent SDK.

    Returns a PostHogClaudeAgentProcessor whose .query() method is a drop-in
    replacement for claude_agent_sdk.query() that automatically emits
    $ai_generation, $ai_span, and $ai_trace events.

    Args:
        client: Optional PostHog client instance. If not provided, uses the default client.
        distinct_id: Optional distinct ID to associate with all events.
            Can also be a callable that takes a ResultMessage and returns a distinct ID.
        privacy_mode: If True, redacts sensitive information in tracking.
        groups: Optional PostHog groups to associate with events.
        properties: Optional additional properties to include with all events.

    Returns:
        PostHogClaudeAgentProcessor: A processor whose .query() method wraps claude_agent_sdk.query().

    Example:
        ```python
        from posthog.ai.claude_agent_sdk import instrument

        ph = instrument(distinct_id="my-app", properties={"env": "prod"})

        async for message in ph.query(prompt="Hello", options=options):
            print(message)
        ```
    """
    return PostHogClaudeAgentProcessor(
        client=client,
        distinct_id=distinct_id,
        privacy_mode=privacy_mode,
        groups=groups,
        properties=properties,
    )


async def query(
    *,
    prompt: Any,
    options: Optional[ClaudeAgentOptions] = None,
    transport: Any = None,
    posthog_client: Optional[Client] = None,
    posthog_distinct_id: Optional[
        Union[str, Callable[[ResultMessage], Optional[str]]]
    ] = None,
    posthog_trace_id: Optional[str] = None,
    posthog_properties: Optional[Dict[str, Any]] = None,
    posthog_privacy_mode: bool = False,
    posthog_groups: Optional[Dict[str, Any]] = None,
):
    """
    Drop-in replacement for claude_agent_sdk.query() with PostHog instrumentation.

    All original messages are yielded unchanged. PostHog events ($ai_generation,
    $ai_span, $ai_trace) are emitted automatically.

    Args:
        prompt: The prompt (same as claude_agent_sdk.query)
        options: ClaudeAgentOptions (same as claude_agent_sdk.query)
        transport: Optional transport (same as claude_agent_sdk.query)
        posthog_client: Optional PostHog client instance.
        posthog_distinct_id: Optional distinct ID for this query.
        posthog_trace_id: Optional trace ID (auto-generated if not provided).
        posthog_properties: Extra properties to include with all events.
        posthog_privacy_mode: If True, redacts sensitive content.
        posthog_groups: Optional PostHog groups.

    Example:
        ```python
        from posthog.ai.claude_agent_sdk import query

        async for message in query(
            prompt="Hello",
            options=options,
            posthog_distinct_id="my-app",
            posthog_properties={"pr_number": 123},
        ):
            print(message)
        ```
    """
    processor = PostHogClaudeAgentProcessor(
        client=posthog_client,
        distinct_id=posthog_distinct_id,
        privacy_mode=posthog_privacy_mode,
        groups=posthog_groups,
        properties={},
    )

    async for message in processor.query(
        prompt=prompt,
        options=options,
        transport=transport,
        posthog_trace_id=posthog_trace_id,
        posthog_properties=posthog_properties,
        posthog_privacy_mode=posthog_privacy_mode,
        posthog_groups=posthog_groups,
    ):
        yield message
