---
pypi/posthog: patch
---

Events captured inside a Celery task now carry that task's own `celery_*` properties. Before, a task published from inside another task, such as a child task, a retry or a chord callback, reported the publishing task's `celery_task_name`, `celery_task_id` and `celery_retry_count`.
