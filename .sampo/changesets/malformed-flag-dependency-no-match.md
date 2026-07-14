---
pypi/posthog: patch
---

Malformed flag-dependency conditions (missing key, null value, or wrong operator) now evaluate locally as no-match (false), matching the server, instead of falling back to the `/flags` endpoint on every evaluation. 7.22.1 made these conditions fall back to the server, which could massively increase billable `/flags` request volume for flag definitions containing legacy/malformed dependency conditions.
