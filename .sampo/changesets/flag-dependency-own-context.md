---
pypi/posthog: patch
---

Local evaluation now evaluates a flag dependency as if the referenced flag were evaluated directly with the same inputs. It uses the referenced flag's own aggregation, properties and bucketing value, as the flags service does. Before, a person flag that depended on a group flag evaluated the group flag with the person's properties and distinct ID, and could return a different value than `/flags`. A group dependency whose group isn't passed now resolves as `false` locally instead of falling back to `/flags`.
