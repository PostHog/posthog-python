---
pypi/posthog: patch
---

A flag record that the server marks `failed` no longer returns a payload, even when the record still carries `metadata.payload`. A failed flag reads as disabled, so `get_feature_flag_payload()`, `get_feature_flag_result().payload` and a snapshot's `get_flag_payload()` now return `None` for it, and its `$feature_flag_called` events leave out `$feature_flag_payload`. `get_all_flags_and_payloads()` and `get_feature_payloads()` already left it out. This applies to responses from both `/flags?v=4` and older servers.
