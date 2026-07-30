---
pypi/posthog: patch
---

Reset PostHog context after fork. Forked children no longer retain the parent process's active lexical context; they start without inherited context and can establish a new child-local context.
