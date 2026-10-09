---
pypi/posthog: minor
---

The Django middleware can read the session ID, and the distinct ID of an identified user, from the posthog-js cookie when a request has no tracing headers, so backend events link to browser sessions on same-site requests without frontend configuration. Turn it on with `POSTHOG_MW_READ_POSTHOG_COOKIE = True`.
