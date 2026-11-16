---
pypi/posthog: major
---

`group_identify` falls back to the context distinct ID and then to `$<group_type>_<group_key>` instead of a random ID, and no longer adds the context session ID. `alias()` no longer takes its target from the context distinct ID. Context tags and options now apply to `alias` and `group_identify`, but context tags, context options, `super_options` and `super_properties` do not set `process_person_profile` on identify-style events (`$identify`, `$create_alias`, `$merge_dangerously`, `$groupidentify`, `$set` and `$set_once`) whose distinct ID the SDK did not generate. Those events default to `True` unless the call sets it. An empty or whitespace-only `distinct_id` counts as missing, and generated personless distinct IDs are UUIDv7. `AsyncPosthog` follows the same rules.
