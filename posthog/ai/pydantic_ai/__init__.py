"""
Pydantic AI integration for PostHog AI observability.

This module provides a simple interface to instrument Pydantic AI agents
with PostHog tracing.
"""

from posthog.ai.pydantic_ai.instrument import instrument_pydantic_ai

__all__ = ["instrument_pydantic_ai"]
