---
pypi/posthog: patch
---

Non-finite float property values (`inf`, `-inf`, `nan`, including `Decimal("Infinity")`) are now sent as `null` instead of the invalid JSON tokens `Infinity`/`NaN`, so strict-parsing ingestion no longer rejects the whole batch. This matches posthog-js.
