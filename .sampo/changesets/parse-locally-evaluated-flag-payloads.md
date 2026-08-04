---
pypi/posthog: patch
---

`evaluate_flags()` now JSON-decodes payloads for locally-evaluated flags, the same way it already did for flags resolved remotely. Previously `get_flag_payload()` returned a parsed value (`{"copy": "new"}`) when the flag came back from `/flags` but the raw JSON string (`'{"copy": "new"}'`) when the poller evaluated it locally, so the payload's type depended on where the flag happened to resolve. The `$feature_flag_payload` property on `$feature_flag_called` events is decoded for locally-evaluated flags too. Payload strings that aren't valid JSON are still passed through unchanged.
