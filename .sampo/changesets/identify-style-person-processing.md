---
pypi/posthog: major
---

Identify-style events (`$identify`, `$create_alias`, `$merge_dangerously`, `$groupidentify`, `$set` and `$set_once`) default to `process_person_profile: True`, from `alias`, `group_identify`, `set`, `set_once`, `capture` and `capture_ai` alike. Only the call's own `process_person_profile` option or `$process_person_profile` property can turn it off, so a `False` from context options or tags, `super_options` or `super_properties` no longer applies to them. PostHog drops `$create_alias` and `$groupidentify` events with person processing off. A call's `$process_person_profile` property moves into the option before `before_send` runs. An event whose distinct ID the SDK generated keeps `process_person_profile: False`. `before_send` sees the value and can change it. `AsyncPosthog` follows the same rules.
