---
pypi/posthog: major
---

`capture_exception` takes a `level=` argument on `Client`, `AsyncPosthog` and the module, which sets `$exception_level` (default `"error"`). Reserved exception properties passed in `properties`, such as `$exception_list` and `$exception_level`, are now ignored. In posthog-python 7.x.x they overrode the SDK's values with a `DeprecationWarning`. `AsyncPosthog.capture_exception` now sends `$exception_level`.
