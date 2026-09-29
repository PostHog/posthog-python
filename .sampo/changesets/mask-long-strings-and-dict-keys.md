---
pypi/posthog: patch
---

Code variable masking now searches strings of up to 2,048 characters for known credential formats, not only strings of up to 200 characters. A key inside a longer string, such as a SQL query that inlines an access key, is now redacted. The OpenAI and Anthropic `sk-` formats no longer match inside words such as `disk-`. Dict keys are now masked too. A key is replaced with a `$$_posthog_redacted_key_<n>_$$` placeholder when it matches a mask pattern but is not a plain field name, when it looks like a secret, when a non-string key holds a part that masking redacts, or when it is too long to scan. URL credentials are removed from string keys, and keys that end up with the same text keep separate entries.
