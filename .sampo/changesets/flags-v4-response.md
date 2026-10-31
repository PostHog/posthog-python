---
pypi/posthog: minor
---

Request `/flags?v=4` and read each flag's typed value, reason and metadata. This release adds no new public API for them yet. Servers that still send the older response keep working, and a flag record with a field of the wrong JSON type fails only that flag. For valid flag records, existing getters, callbacks and `$feature_flag_called` properties return the same values as before: a number or object value reads as `True`, with the value as its payload. Two things can differ: the human-readable reason text of a config version 2 flag is the one the server sends at v4, and a record with a known field of the wrong JSON type (for example a string `reason`) now reads as a failed flag instead of being read anyway or failing the whole response. The SDK logs once per client when a server sends a response without typed values. `FlagMetadata.id` and `FlagMetadata.version` are `None` when a record has none, instead of `0`, so their type is now `Optional[int]`.
