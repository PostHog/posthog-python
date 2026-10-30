---
pypi/posthog: patch
---

AI integrations (LLM wrappers, embeddings, LangChain and OpenAI Agents) treat an empty or whitespace-only `distinct_id` as missing, as the client does: the event uses its trace or run ID and is personless. Before, an empty ID sent the trace ID without `process_person_profile: false`, so PostHog created a person for each trace.
