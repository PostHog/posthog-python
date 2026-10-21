---
pypi/posthog: minor
---

Manage AI observability datasets and items through `OfflineEvaluations.datasets` and `AsyncOfflineEvaluations.datasets`, including history, tags, and archive/restore operations. Pin a dataset snapshot for lazy, consistent reads and use `EvaluationItem.from_dataset_item()` to preserve case provenance across offline experiments and scorers. Item creates with a caller-supplied `client_item_id` support safe retries; updates use explicit version checks.
