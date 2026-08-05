---
pypi/posthog: patch
---

The `$feature_flag_called` dedupe tracker now evicts its oldest entry when it reaches capacity instead of clearing every entry. Previously, each time a client accumulated 50,000 distinct IDs the whole tracker was wiped, so the next flag read for every previously seen distinct ID re-emitted a `$feature_flag_called` event it had already deduped.
