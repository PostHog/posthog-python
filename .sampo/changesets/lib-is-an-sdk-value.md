---
pypi/posthog: major
---

Events send `$lib` and `$lib_version` as they are at send time, whoever set them. If no `$lib` is set in the call's `properties`, a context tag or `super_properties`, the SDK sets both to `posthog-python` and the installed version. If `$lib` is `posthog-python` and `$lib_version` is missing, the SDK fills in the version. A `$lib` that names another library is sent with whatever `$lib_version` it has, so set both when you override them. Only a non-empty string counts as set. In 7.x the SDK overwrote a `$lib` or `$lib_version` set in a call or a context tag. `before_send` can still change or remove both, and `AsyncPosthog` follows the same rules.
