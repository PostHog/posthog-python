---
pypi/posthog: patch
---

`$ai_tools` masks the credentials in Gemini tool definitions with `[REDACTED]`, with or without privacy mode: the `api_auth` and `auth_config` of `retrieval.external_api`, the `auth_config` of `google_maps`, the `api_key` of `parallel_ai_search`, and the `headers` of `mcp_servers`. The tools you pass in are not modified.
