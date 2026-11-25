---
pypi/posthog: patch
---

`$ai_tools` masks the credentials in Gemini tool definitions with `[REDACTED]`, with or without privacy mode: every `api_key`, `api_auth`, `auth_config`, and `headers` inside a tool's config, such as `retrieval.external_api`, `google_maps`, `exa_ai_search`, `parallel_ai_search`, and `mcp_servers`. Function declarations are not masked. The tools you pass in are not modified.
