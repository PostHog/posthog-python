---
pypi/posthog: patch
---

The `conversation_id` tool argument's description no longer tells an agent to wait for an id before issuing parallel tool calls. Some requests never receive an id, such as calls that already carry a transport session, so an obedient agent serialized every call. The description now asks for the first call to run on its own, then to reuse any id it returned.
