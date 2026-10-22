---
pypi/posthog: patch
---

A non-streamed OpenAI chat completion that the model refused is no longer captured with an empty `$ai_output_choices`: the refusal text is recorded as a `refusal` content block, as it already is for streamed responses.
