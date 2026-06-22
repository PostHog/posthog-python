---
'pypi/posthog': patch
---

Mask sensitive data held inside objects and in URL/DSN credentials when capturing exception code variables. Custom objects are now traversed so fields like `password` are redacted by attribute name instead of leaking via `repr()`, and credentials embedded in connection strings are scrubbed. Adds the `code_variables_mask_url_credentials` option (default `True`).
