---
pypi/posthog: minor
---

`posthog.mcp` is now stable. From this release, a minor or patch release of `posthog` no longer makes breaking changes to the `posthog.mcp` API, renames or removes a `$mcp_*` event or property, changes its type, or changes which requests send an event, so your saved insights keep working across upgrades. New events and properties can still arrive in minor releases. Upgrading needs no code change.
