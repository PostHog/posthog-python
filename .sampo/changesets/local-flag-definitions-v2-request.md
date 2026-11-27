---
pypi/posthog: minor
---

Local evaluation now asks for flag definitions with `v=2`, on the first load and on every poll, so it receives flags in the newer configuration format and evaluates them locally. A flag it cannot evaluate locally falls back to a remote request in the default mode and has no value with `only_evaluate_locally`, as for any other flag it cannot resolve. For a project without such flags the definitions response does not change. With a `flag_definition_cache_provider`, a client that loads definitions from the cache that differ from the ones it fetched, such as the legacy definitions an older release wrote, drops its ETag, so its next request gets the whole feed instead of `304`.
