---
pypi/posthog: patch
---

Only terminal Responses API statuses become `$ai_stop_reason`: a queued or in-progress background run no longer records a lifecycle state as its stop reason, and an incomplete run is named by what cut it short (`incomplete_details.reason`, e.g. `max_output_tokens`). Streaming runs that end incomplete or failed now carry a stop reason too, and the LangChain callback reads stop reasons from `response_metadata` as well, covering Responses API and Anthropic runs that previously recorded none.
