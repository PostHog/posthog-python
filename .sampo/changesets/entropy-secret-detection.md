---
'pypi/posthog': patch
---

Detect and redact high-entropy secrets (API keys, tokens, passwords) in exception code variables. Adds the `code_variables_detect_secrets` option (default `True`).
