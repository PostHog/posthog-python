---
pypi/posthog: minor
---

`prompts.get_all()` now works without a label. It fetches the latest version of every prompt in one request and warms the cache for plain `prompts.get(name)` calls. Previously the label was required, and passing `label=None` sent the literal string "None" as the label filter, returning an empty result.
