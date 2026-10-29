---
pypi/posthog: minor
---

Request `/flags?v=4` and keep each flag's typed value, reason and metadata. Servers that still send the older response keep working, and a flag record with a field of the wrong JSON type fails only that flag. Existing getters, callbacks and `$feature_flag_called` properties return the same values as before: a number or object value reads as `True`, with the value as its payload. The SDK logs once per client when a server sends a response without typed values. `FeatureFlag` gains `value` (the typed value) and `failed` (the flag failed to evaluate or its record could not be read), and `FlagMetadata` gains `config_version`, `rule_type`, `rule_id`, `variant_key` and `forced_variant`. `FlagMetadata.id` and `FlagMetadata.version` are `None` when a record has none, instead of `0`.
