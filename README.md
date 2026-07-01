# PostHog Python

<p align="center">
  <img alt="posthoglogo" src="https://user-images.githubusercontent.com/65415371/205059737-c8a4f836-4889-4654-902e-f302b187b6a0.png">
</p>
<p align="center">
   <a href="https://pypi.org/project/posthog/"><img alt="pypi installs" src="https://img.shields.io/pypi/v/posthog"/></a>
   <img alt="GitHub contributors" src="https://img.shields.io/github/contributors/posthog/posthog-python">
  <img alt="GitHub commit activity" src="https://img.shields.io/github/commit-activity/m/posthog/posthog-python"/>
  <img alt="GitHub closed issues" src="https://img.shields.io/github/issues-closed/posthog/posthog-python"/>
</p>

Please see the main [PostHog docs](https://posthog.com/docs).

SDK usage examples and code snippets live in the official documentation so they stay up to date.

## Python Version Support

| SDK Version   | Python Versions Supported    | Notes                      |
| ------------- | ---------------------------- | -------------------------- |
| 7.3.1+        | 3.10, 3.11, 3.12, 3.13, 3.14 | Added Python 3.14 support  |
| 7.0.0 - 7.0.1 | 3.10, 3.11, 3.12, 3.13       | Dropped Python 3.9 support |
| 4.0.1 - 6.x   | 3.9, 3.10, 3.11, 3.12, 3.13  | Python 3.9+ required       |

## Documentation

- [Python library docs](https://posthog.com/docs/libraries/python)
- [Django framework docs](https://posthog.com/docs/libraries/django)
- [Flask framework docs](https://posthog.com/docs/libraries/flask)
- [OpenFeature provider docs](https://posthog.com/docs/feature-flags/installation/openfeature) — use PostHog flags through the [OpenFeature](https://openfeature.dev) Python SDK

## Async usage

Install the async extra to use the asyncio-native client:

```bash
pip install 'posthog[async]'
```

Use `AsyncPosthog` in async applications and close it with `async with` or `await shutdown()`:

```python
from posthog import AsyncPosthog

async with AsyncPosthog("<ph_project_api_key>", host="<ph_client_api_host>") as posthog:
    await posthog.capture("page_viewed", distinct_id="user_123")
    await posthog.flush()
```

Async feature flag evaluation uses non-blocking HTTP requests:

```python
async with AsyncPosthog("<ph_project_api_key>") as posthog:
    flags = await posthog.evaluate_flags("user_123")
    if flags.is_enabled("new-dashboard"):
        ...
    await posthog.capture("page_viewed", distinct_id="user_123", flags=flags)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup and test instructions.

## Releasing

See [RELEASING.md](RELEASING.md).
