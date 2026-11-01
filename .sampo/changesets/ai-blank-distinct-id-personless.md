---
pypi/posthog: patch
---

AI integrations (LLM wrappers, embeddings, LangChain and OpenAI Agents) treat an empty or whitespace-only `distinct_id` as missing, as the client does: the event uses its trace or run ID and is personless. Before, an empty ID gave streaming calls, embeddings and LangChain the trace or run ID with person processing on, so PostHog created a person for each trace or run. Non-streaming wrapper calls used a random personless ID, and OpenAI Agents already treated an empty ID as missing. A whitespace-only ID was sent as given.
