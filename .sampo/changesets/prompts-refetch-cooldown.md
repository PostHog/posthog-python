---
pypi/posthog: minor
---

After a failed prompt refetch, the SDK now serves the stale cached prompt for a cooldown period (60 seconds by default) instead of retrying the network on every `prompts.get()` call. When the failure is a 429, the cooldown follows the `Retry-After` the server sends, capped at one hour. This keeps a rate-limited client from holding itself against the limit. Matches the behavior the JavaScript SDK already has.
