---
pypi/posthog: patch
---

Code variable masking now searches strings of up to 2,048 characters for known credential formats, not only strings of up to 200 characters. A key inside a longer string, such as a SQL query that inlines an access key, is now redacted. Dict keys are now masked too. A non-string key whose text matches a mask pattern, a key that looks like a secret, and a key too long to scan are each replaced with a `$$_posthog_redacted_key_<n>_$$` placeholder. URL credentials are removed from string keys.
