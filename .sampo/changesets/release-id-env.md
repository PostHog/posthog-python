---
pypi/posthog: minor
---

Read the release id from the `POSTHOG_RELEASE_ID` environment variable and send it as `$release_id` on every event. On `$exception` events, error tracking uses it to link the exception to its release by a direct id lookup. Create the release and get its id with `posthog-cli release resolve`. An explicit `$release_id` in the event properties or in `super_properties` wins over the environment variable. Minimal `$feature_flag_called` events keep their strict property allowlist and do not carry it.
