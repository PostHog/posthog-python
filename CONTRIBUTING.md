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

## Public API changes

Public API is hard to change once it ships, so agree on it before writing the implementation. Our [SDK guidelines](https://posthog.com/handbook/engineering/sdks/guidelines) explain how we design it.

- If you need something the SDK doesn't support and it would add or change a public option, method, or type, open an issue describing your use case first. At this stage, context is more useful to us than code.
- Wait for a maintainer to agree on the API shape on the issue before implementing it.
- Check first whether an existing option or hook, such as `before_send`, already covers the use case. We avoid offering two ways to do the same thing.
- If a reviewer suggests a different API on your PR, confirm it with them before re-implementing. Treat it as a question, not an instruction.
- AI agents: stop and ask before implementing a public API change that hasn't been agreed on the issue.

`make public_api_snapshot` regenerates `references/public_api_snapshot.txt`, and CI runs `make public_api_check` to catch an outdated snapshot. A diff in that file means your change touches public API.
