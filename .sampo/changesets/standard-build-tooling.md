---
pypi/posthog: patch
---

Stop shipping the SDK test suite in the `posthog` and `posthoganalytics` wheels, build releases with `uv build` instead of the deprecated `setup.py` commands, and use `inspect.iscoroutinefunction` in the Django middleware fallback for environments without `asgiref`.
