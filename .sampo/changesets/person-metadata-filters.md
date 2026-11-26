---
pypi/posthog: patch
---

Local flag evaluation treats a filter type it cannot evaluate, such as a cohort's `person_metadata` filter on first seen, as inconclusive instead of matching it against a person property with the same key. A later condition can still decide the flag, and otherwise it falls back to `/flags`.
