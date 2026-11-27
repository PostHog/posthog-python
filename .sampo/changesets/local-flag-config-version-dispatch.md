---
pypi/posthog: patch
---

Local evaluation now chooses the evaluator of each flag definition by its configuration version. Definitions in the current format are evaluated as before, and the definitions request is unchanged.
