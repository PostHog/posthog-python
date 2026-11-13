---
pypi/openfeature-provider-posthog: patch
---

Document failed-record default handling with the updated PostHog SDK: a server-marked failed flag supplies no result, so the provider's existing FlagNotFoundError mapping lets OpenFeature return the caller's default instead of treating the failed record as a healthy disabled flag. This behavior requires upgrading the installed posthog dependency; the provider's compatible dependency range is unchanged and this patch intent does not force that upgrade.
