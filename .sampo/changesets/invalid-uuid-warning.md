---
pypi/posthog: major
---

An invalid event `uuid` is still replaced with a generated one, but the SDK now logs one warning instead of an error, and the warning no longer includes the value. An empty string `uuid` counts as unset, so the SDK generates one without logging. `AsyncPosthog` follows the same rules.
