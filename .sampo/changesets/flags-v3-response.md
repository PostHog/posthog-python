---
pypi/posthog: minor
---

Request `/flags?v=3` and keep each flag's typed value, reason and metadata. Servers that still send the older response keep working. Existing getters, callbacks and `$feature_flag_called` properties return the same values as before: a number or object value reads as `True`, with the value as its payload. Add `get_boolean_value`, `get_string_value`, `get_number_value` and `get_object_value` to the `evaluate_flags()` result, each with a `_details` form that also returns the reason and metadata. These accessors never coerce: they return your default when the flag is missing, failed, has no value, or has a value of another type.
