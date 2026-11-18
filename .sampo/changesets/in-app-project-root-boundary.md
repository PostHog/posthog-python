---
pypi/posthog: patch
---

`posthog.exception_utils`: only mark frames as `in_app` when their path is inside the project root directory, so sibling directories sharing the root's name as a prefix (e.g. `/work/app-shared` for root `/work/app`) are no longer treated as application code.
