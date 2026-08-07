---
pypi/posthog: minor
---

`feature_enabled()` now accepts a `default_value` parameter, returned when the flag has no value — not loaded, a failed `/flags` request, or no flag with that key. A flag that has a value, including `False` and variant strings, still always wins over the default. Existing calls that don't pass `default_value` are unaffected (it defaults to `None`, preserving the current three-state return).
