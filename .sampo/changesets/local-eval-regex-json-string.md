---
posthog: patch
---

Stringify non-string properties as compact JSON for local `regex` and `not_regex` matching, so booleans, lists, and objects match the same way they do on the flags service.
