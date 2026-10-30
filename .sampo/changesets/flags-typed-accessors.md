---
pypi/posthog: minor
---

Add `get_boolean_value`, `get_string_value`, `get_number_value`, `get_integer_value`, `get_float_value` and `get_object_value` to the `evaluate_flags()` result, each with a `_details` form that returns a `FlagEvaluationDetails` with an OpenFeature `reason`, `error_code` and `flag_metadata`. These accessors never coerce and never raise: they return your default when the flag is missing, failed, unreadable, has no value, or has a value of another type, and an object value too deeply nested to copy reads as unreadable. `FeatureFlag` gains `value` (the typed value), `failed`, and `error_code` and `error_message`, which say why a flag failed or could not be read. `FlagMetadata` gains `config_version`, `rule_type`, `rule_id`, `variant_key` and `forced_variant`. Number and object values need a server that sends typed values; the SDK logs once per client when it reads a response without them.
