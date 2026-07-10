---
pypi/posthog: patch
---

Fix local evaluation of flag dependencies with a `flag_evaluates_to: false` condition: such conditions never matched, forcing the dependent flag to `false` for every locally-evaluated user.
