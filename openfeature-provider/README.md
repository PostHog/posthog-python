# PostHog provider for OpenFeature (Python)

Official [PostHog](https://posthog.com) provider for the
[OpenFeature](https://openfeature.dev) Python SDK. It wraps a configured
`posthog.Posthog` client and resolves flags through OpenFeature's standard
evaluation API.

Distribution: `openfeature-provider-posthog` · Import path:
`openfeature.contrib.provider.posthog` · License: MIT.

## Install

```bash
pip install openfeature-provider-posthog
```

This pulls in `posthog` and `openfeature-sdk`.

## Usage

Construct and configure a `Posthog` client yourself (you own its lifecycle),
then register the provider:

```python
import posthog
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.contrib.provider.posthog import PostHogProvider

client = posthog.Posthog(
    "phc_project_api_key",
    host="https://us.i.posthog.com",
    # personal_api_key="phx_...",  # enables local evaluation
)

api.set_provider(PostHogProvider(client, default_distinct_id="anonymous"))
of_client = api.get_client()

ctx = EvaluationContext(
    targeting_key="user-123",
    attributes={
        "plan": "pro",                              # -> person property
        "groups": {"organization": "acme"},          # -> PostHog groups
        "group_properties": {"organization": {"tier": "enterprise"}},
    },
)

enabled = of_client.get_boolean_value("my-flag", False, ctx)
variant = of_client.get_string_value("my-experiment", "control", ctx)
config = of_client.get_object_value("my-config", {}, ctx)
```

When you're done, shut down your own client (the provider does not own it):

```python
client.shutdown()
```

## Evaluation context

| OpenFeature context | PostHog input |
| --- | --- |
| `targeting_key` | `distinct_id` (required unless `default_distinct_id` is set) |
| attribute `groups` | `groups` |
| attribute `group_properties` | `group_properties` |
| any other attribute | `person_properties` |

If no `targeting_key` is present and no `default_distinct_id` is configured, the
provider raises `TargetingKeyMissingError` and the OpenFeature client returns
your default value.

## Supported flag types

| Method | Resolves to |
| --- | --- |
| `get_boolean_value` | whether the flag is enabled |
| `get_string_value` | the multivariate variant key |
| `get_integer_value` / `get_float_value` | the variant key parsed as a number |
| `get_object_value` | the flag's JSON payload (a dict or list) |

Calling a typed getter on an incompatible flag (e.g. `get_string_value` on a
plain boolean flag, or `get_integer_value` on a non-numeric variant) returns
your default value with `error_code = TYPE_MISMATCH`, per the OpenFeature spec.

## Options

`PostHogProvider(client, *, default_distinct_id=None, send_feature_flag_events=True)`

- `default_distinct_id` — distinct ID used when the context has no
  `targeting_key`. Defaults to `None` (raise `TargetingKeyMissingError`).
- `send_feature_flag_events` — whether to emit `$feature_flag_called` events on
  each evaluation. Defaults to `True` so flag analytics and experiments keep
  working; set `False` for high-volume evaluation where you don't need them.

## Development

```bash
cd openfeature-provider
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy .
```
