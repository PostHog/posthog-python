---
pypi/posthog: minor
---

feat(ai): `Prompts.get(..., with_metadata=True)` results now include `config`, the JSON object of model parameters or agent configuration stored with the prompt version in PostHog prompt management (`None` when the version has none). Config is carried through the client-side cache and the stale-cache fallback. The hardcoded `fallback` string has no config, so use defensive access like `(result.config or {}).get("temperature", 0)`.
