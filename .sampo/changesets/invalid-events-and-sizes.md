---
pypi/posthog: major
---

An invalid event no longer costs the rest of its batch. An event with a missing, empty or non-string name is dropped locally, an event that cannot be serialized (for example with `NaN`) is dropped alone, and a property key JSON cannot represent is dropped from its event. The analytics per-event limit is now capture's 10 MiB request limit instead of 900 KiB. A personal API key (`phx_...`) disables the client like an empty key. Logs no longer include event content or invalid timestamp values. `AsyncPosthog` follows the same rules.
