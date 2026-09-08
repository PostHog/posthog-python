---
pypi/posthog: minor
---

OpenAI and LangChain generations now also emit the served service tier as the explicit `$ai_service_tier` event property, next to the copy inside `$ai_model_parameters`. Cost processing prices tiered calls only from the explicit property, whose writers assert response-derived values.
