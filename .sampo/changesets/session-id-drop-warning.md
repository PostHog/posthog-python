---
pypi/posthog: major
---

A `$session_id` or `$window_id` that is not a string is not sent, because capture v1 would reject the whole batch, and the SDK logs a warning for each one. The warning names the key and the value's type, not the value. `None` counts as unset and drops without a warning. An empty string is sent. In 7.x these values were sent as properties.
