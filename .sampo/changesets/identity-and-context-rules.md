---
pypi/posthog: major
---

`group_identify` falls back to the context distinct ID and then to `$<group_type>_<group_key>` instead of a random ID, and no longer adds the context session ID. `alias()` no longer takes its target from the context distinct ID. Context tags and options now apply to `alias` and `group_identify`. An empty or whitespace-only `distinct_id` counts as missing, and generated personless distinct IDs are UUIDv7. `AsyncPosthog` follows the same rules.
