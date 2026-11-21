---
posthog: minor
---

Add `ai_trace_sample_rate` option and `POSTHOG_AI_TRACE_SAMPLE_RATE` environment variable to sample AI observability events (`$ai_*`) by trace ID across `Client` and `AsyncClient` / `AsyncPosthog` using 32-bit FNV-1a hashing with Murmur3 fmix32 finalization.
