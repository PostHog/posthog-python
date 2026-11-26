---
pypi/posthog: patch
---

Local evaluation of a group flag without a usable group key now skips only its group conditions, as the flags service does. Its conditions aggregated on the person still run and can enable the flag, directly or as a dependency. A group flag with a holdout above 0% and no usable group key is inconclusive (`missing_context`) and falls back to `/flags`.
