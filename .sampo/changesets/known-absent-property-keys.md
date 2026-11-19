---
posthog: minor
---

Add `person_property_keys_known_absent` and `group_property_keys_known_absent` to flag evaluation. Local evaluation treats a listed key as absent, so `is_not_set` and negated operators match without a `/flags` request, and a listed `$feature_enrollment/<flag key>` means not enrolled. Remote requests send the listed keys. Each list allows at most 1000 keys; a longer list raises `ValueError` and nothing is sent.
