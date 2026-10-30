---
pypi/posthog: patch
---

Keep the choices of a streamed OpenAI chat completion with `n > 1` apart in `$ai_output_choices`, as a non-streamed one already does. Their text and tool calls were interleaved into one choice.
