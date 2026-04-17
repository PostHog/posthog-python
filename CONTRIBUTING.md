# Contributing

Thanks for your interest in improving the PostHog Python SDK.

## Commit signing

This repo requires all commits to be signed. To configure commit signing, see the [PostHog handbook](https://posthog.com/handbook/engineering/security#commit-signing).

## Setup

We recommend using [uv](https://docs.astral.sh/uv/).

```bash
uv venv
source .venv/bin/activate
uv sync --extra dev --extra test
```

## CI-aligned checks

Run the same core checks CI uses before opening a PR:

```bash
ruff format --check .
ruff check .
mypy --no-site-packages --config-file mypy.ini . | mypy-baseline filter
pytest --verbose --timeout=30
python -W error -c "import posthog"
```

## Running locally

Assuming you have a [local version of PostHog](https://posthog.com/docs/developing-locally) running, you can run `python3 example.py` to see the library in action.

## Testing changes locally with the PostHog app

Run `make prep_local` to create a sibling folder named `posthog-python-local`. You can then import it into the PostHog app by changing `pyproject.toml` like this:

```toml
dependencies = [
    ...
    "posthoganalytics" #NOTE: no version number
    ...
]
...
[tools.uv.sources]
posthoganalytics = { path = "../posthog-python-local" }
```

This lets you test SDK changes fully locally inside the PostHog app stack. It mainly takes care of the `posthog -> posthoganalytics` module renaming. Re-run `make prep_local` each time you make a change, and then run `uv sync --active` in the PostHog app project.
