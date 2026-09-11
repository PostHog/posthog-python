---
pypi/posthog: patch
---

`Prompts.get_all` now fails loudly in two cases it previously papered over: a server that ignores the label filter but happens to have some labels on latest versions no longer produces a silently incomplete result, and a malformed row in the list response now raises the invalid-response error instead of being skipped. A rejected batch also no longer leaves partially cached prompts.
