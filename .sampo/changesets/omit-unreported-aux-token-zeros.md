---
pypi/posthog: patch
---

Streaming generations interrupted before the provider reported any usage no longer send zero `$ai_cache_read_input_tokens`, `$ai_cache_creation_input_tokens`, or `$ai_reasoning_tokens`. A fabricated 0 reads as a report of nothing, so cost processing priced an unknown generation as a known $0.00 instead of leaving it unknown. Streams whose usage was reported keep the historical zero defaults.
