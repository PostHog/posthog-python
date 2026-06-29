# PostHog provider for OpenFeature (Python)

Official [PostHog](https://posthog.com) provider for the
[OpenFeature](https://openfeature.dev) Python SDK. It resolves OpenFeature flag
evaluations through a configured `posthog.Posthog` client.

- **Distribution:** `openfeature-provider-posthog`
- **Import path:** `openfeature.contrib.provider.posthog`
- **License:** MIT

## Documentation

PostHog's feature-flag docs are the single source of truth for flag setup,
concepts, and usage: **https://posthog.com/docs/feature-flags**

## Install

```bash
pip install openfeature-provider-posthog
```

## Quickstart

```python
import posthog
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.contrib.provider.posthog import PostHogProvider

client = posthog.Posthog("phc_project_api_key", host="https://us.i.posthog.com")
api.set_provider(PostHogProvider(client, default_distinct_id="anonymous"))

of_client = api.get_client()
ctx = EvaluationContext(targeting_key="user-123", attributes={"plan": "pro"})
enabled = of_client.get_boolean_value("my-flag", False, ctx)
```

The OpenFeature `targeting_key` maps to PostHog's `distinct_id`; other context
attributes become person properties, with reserved keys `groups` and
`group_properties` mapping to PostHog groups. You own the `Posthog` client
lifecycle — call `client.shutdown()` when done.

## Development

```bash
cd openfeature-provider
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy .
```
