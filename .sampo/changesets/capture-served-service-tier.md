---
pypi/posthog: minor
---

OpenAI generations now record the service tier the provider served (`service_tier` inside `$ai_model_parameters`), on non-streaming, streaming, and LangChain capture paths. LLM analytics uses it to price flex and priority calls at their real rates instead of standard; a requested tier can be refused, so the value always comes from the response.
