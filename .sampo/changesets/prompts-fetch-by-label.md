---
pypi/posthog: minor
---

Add a `label` option to `Prompts.get()` to fetch the prompt version a label (e.g. `production`) currently points to. Labeled fetches are cached separately, and `PromptResult` carries the resolved `label`. Requires a PostHog version with prompt labels; older servers ignore the parameter and return the latest version.
