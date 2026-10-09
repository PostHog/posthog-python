---
pypi/posthog: minor
---

The Django middleware now reads the distinct ID and the live session ID from the posthog-js cookie when the tracing headers are not present, so backend events link to browser sessions on same-site requests without frontend configuration. Set `POSTHOG_MW_READ_POSTHOG_COOKIE = False` to turn this off.
