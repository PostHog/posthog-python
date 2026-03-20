"""OpenAI Agents SDK with input/output guardrails, tracked by PostHog."""

import asyncio
import os
from agents import Agent, Runner, input_guardrail, output_guardrail, GuardrailFunctionOutput, RunContextWrapper, TResponseInputItem
from posthog import Posthog
from posthog.ai.openai_agents import instrument

posthog = Posthog(os.environ["POSTHOG_API_KEY"], host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"))
instrument(posthog, distinct_id="example-user")

BLOCKED_INPUT_WORDS = ["hack", "exploit", "bypass"]
BLOCKED_OUTPUT_WORDS = ["confidential", "secret", "classified"]


@input_guardrail
async def content_filter(
    ctx: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    """Block requests containing prohibited words."""
    text = str(input).lower() if isinstance(input, str) else " ".join(str(i) for i in input).lower()
    for word in BLOCKED_INPUT_WORDS:
        if word in text:
            return GuardrailFunctionOutput(
                output_info={"blocked_word": word},
                tripwire_triggered=True,
            )
    return GuardrailFunctionOutput(output_info={"status": "passed"}, tripwire_triggered=False)


@output_guardrail
async def sensitive_data_filter(
    ctx: RunContextWrapper[None], agent: Agent, output: str
) -> GuardrailFunctionOutput:
    """Prevent sensitive information from being returned."""
    for word in BLOCKED_OUTPUT_WORDS:
        if word in output.lower():
            return GuardrailFunctionOutput(
                output_info={"blocked_word": word},
                tripwire_triggered=True,
            )
    return GuardrailFunctionOutput(output_info={"status": "passed"}, tripwire_triggered=False)


guarded_agent = Agent(
    name="GuardedAgent",
    instructions="You are a helpful assistant. Be informative but avoid sensitive topics.",
    model="gpt-4o-mini",
    input_guardrails=[content_filter],
    output_guardrails=[sensitive_data_filter],
)


async def main():
    # This should pass guardrails
    result = await Runner.run(guarded_agent, "What is product analytics?")
    print(f"Passed: {result.final_output}")

    # This should trigger the input guardrail
    try:
        await Runner.run(guarded_agent, "How do I hack into a system?")
    except Exception as e:
        print(f"Blocked: {e}")


asyncio.run(main())
posthog.shutdown()
