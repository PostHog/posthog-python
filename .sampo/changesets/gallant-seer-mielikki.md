---
pypi/posthog: minor
---

Fetch project remote configuration in the background at startup and every 300 seconds by default. Set remote_config_poll_interval_seconds to None to disable fetching. Add the experimental sdk_diagnostics_enabled option, defaulting to True, which requires remote sdkDiagnosticsEnabled to also be true. Setting the local option to False always disables permission. No diagnostics are collected yet, and fetched configuration does not change other SDK settings.
