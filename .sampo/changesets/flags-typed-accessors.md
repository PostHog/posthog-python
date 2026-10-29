---
pypi/posthog: minor
---

Add `get_boolean_value`, `get_string_value`, `get_number_value`, `get_integer_value`, `get_float_value` and `get_object_value` to the `evaluate_flags()` result, each with a `_details` form that returns a `FlagEvaluationDetails` with an OpenFeature `reason`, `error_code` and `flag_metadata`. These accessors never coerce: they return your default when the flag is missing, failed, has no value, or has a value of another type. Number and object values need a server that sends typed values; the SDK logs once per client when it reads a response without them.
