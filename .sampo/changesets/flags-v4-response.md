---
pypi/posthog: minor
---

Request `/flags?v=4` and keep each flag's typed value, reason and metadata internally. Servers that still send the older response keep working. Existing getters, callbacks and `$feature_flag_called` properties return the same values as before: a number or object value reads as `True`, with the value as its payload. The SDK logs once per client when a server sends a response without typed values. No public API is added.
