---
pypi/posthog: minor
---

The Celery integration propagates context options from the publisher to the worker, like tags. Options set with `set_context_option` before a task is published apply to every event captured while the task runs. `propagate_context=False` sends none of them.
