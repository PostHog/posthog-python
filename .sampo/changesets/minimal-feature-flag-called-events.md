---
'pypi/posthog': minor
---

`$feature_flag_called` events are now minimized for non-experiment flags when the server enables it. When the `/flags` v2 response (`minimalFlagCalledEvents`) or the local-evaluation payload (`minimal_flag_called_events`) reports the gate as enabled and the evaluated flag has no linked experiment (`has_experiment` is `false`), the event's properties are reduced to a strict allowlist (`$feature_flag`, `$feature_flag_response`, `$feature_flag_has_experiment`, the `$feature_flag_*` debug scalars, `locally_evaluated`, `$groups`, `$process_person_profile`, `$session_id`, `$lib`, `$lib_version`, `$is_server`, `$geoip_disable`, `$os`, `$os_version`, `$os_distro`, `$python_runtime`, `$python_version`). Everything else — including super properties and custom event properties — is stripped from those events.

If the server does not report the gate, if the flag's `has_experiment` signal is missing, or if the flag is linked to an experiment, the full property set is sent unchanged. There is no SDK-side configuration; the gate is controlled per-team by the server. For `evaluate_flags()` snapshots, the gate is pinned when the snapshot is created, so deferred flag accesses are shaped by the evaluation that produced them.

Custom `flag_definition_cache` providers now receive an additional `minimal_flag_called_events` key in the definitions payload, so the gate survives external cache round-trips.

When the server reports `has_experiment` for a flag, every `$feature_flag_called` event also carries a `$feature_flag_has_experiment` boolean property.
