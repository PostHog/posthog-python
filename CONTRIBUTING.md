# Contributing

Thanks for your interest in improving the PostHog Python SDK.

## Commit signing

This repo requires all commits to be signed. To configure commit signing, see the [PostHog handbook](https://posthog.com/handbook/engineering/security#commit-signing).

## Testing locally

We recommend using [uv](https://docs.astral.sh/uv/).

1. Create a virtual environment:
   - `uv venv env`
   - or `python3 -m venv env`
2. Activate it:
   - `source env/bin/activate`
3. Install the package in editable mode with development and test dependencies:
   - `uv sync --extra dev --extra test`
   - or `pip install -e ".[dev,test]"`
4. Install pre-commit hooks:
   - `pre-commit install`
5. Run the test suite:
   - `make test`
6. Run a specific test if needed:
   - `pytest -k test_no_api_key`

## Recommended `uv` workflow

```bash
uv python install 3.12
uv python pin 3.12
uv venv
source env/bin/activate
uv sync --extra dev --extra test
pre-commit install
make test
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
