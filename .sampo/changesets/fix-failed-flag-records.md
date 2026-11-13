---
pypi/posthog: patch
---

Treat server-marked failed feature flag records as unevaluated: direct result, value, enabled and payload getters return None rather than a disabled False result or placeholder payload. Snapshot value and payload reads return None, enablement honors the caller's default, and failed keys are omitted from enumeration and capture enrichment. Bulk value and payload maps omit failed remote records while preserving successful locally computed values and payloads for those keys. Preserve selected or accessed failure diagnostics through snapshot filters and report local_evaluation_inconclusive alongside evaluation_failed after an unresolved failed fallback; healthy False and malformed-record compatibility are unchanged.
