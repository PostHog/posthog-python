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

Run the smallest relevant tests first, for example `pytest posthog/test/test_capture_v1.py --timeout=30` for v1 transport changes. Then run these core CI-aligned checks from the repository root in the activated `.venv` populated by the setup commands above:

```bash
ruff format --check .
ruff check .
mypy --no-site-packages --config-file mypy.ini . | mypy-baseline filter
pytest --verbose --timeout=30
python -W error -c "import posthog"
```

Without activating `.venv`, prefix Python-tool commands with `uv run --no-sync` after the same setup/sync (including both sides of the mypy pipeline). This uses the populated environment without re-syncing away its selected extras.

For public API changes, regenerate and review `references/public_api_snapshot.txt`, then check it in that environment:

```bash
make public_api_snapshot
make public_api_check
```

These Make targets invoke `python`: keep `.venv` activated, or use `uv run --no-sync make <target>`.

For changes under `openfeature-provider/`, also follow its [contributor guide](./openfeature-provider/CONTRIBUTING.md#local-development). Root pytest collection targets `posthog/test`; it does not substitute for the provider's package-scoped checks.

## Running locally

Assuming you have a [local version of PostHog](https://posthog.com/docs/developing-locally) running, you can run `python3 example.py` to see the library in action.

## Testing changes locally with the PostHog app

**Warning:** `make prep_local` deletes and recreates `../posthog-python-local`. Before using it (including every re-run), verify that no work there needs preserving. It creates a renamed SDK copy for local testing; do not commit generated `posthoganalytics/` directories.

You can then import that copy into the PostHog app by changing the app's `pyproject.toml` like this:

```toml
dependencies = [
    ...
    "posthoganalytics" #NOTE: no version number
    ...
]
...
[tool.uv.sources]
posthoganalytics = { path = "../posthog-python-local" }
```

This lets you test SDK changes fully locally inside the PostHog app stack. It mainly takes care of the `posthog -> posthoganalytics` module renaming. Re-run `make prep_local` each time you make a change, and then run `uv sync --active` in the PostHog app project.

## Public API changes

Public API is hard to change once it ships, so agree on it before writing the implementation. Our [SDK guidelines](https://posthog.com/handbook/engineering/sdks/guidelines) explain how we design it.

This section is for external contributors. PostHog maintainers (members of the PostHog GitHub org) agree on API shape in the PR itself, so they don't need a separate issue.

- **Before you start:** if you need something the SDK doesn't support and it would add or change a public option, method, or type, open an issue describing your use case. Wait for a maintainer to agree on the API shape there before you implement it. Context is more useful to us than code at this stage.
- **Already specified?** If a published [sdk-spec](https://github.com/PostHog/sdk-specs) defines the API, that's the agreement, so you don't need an issue.
- **Already have a PR open?** Don't stop or rewrite it. Call out the public API change at the top of the PR description, and link or open an issue so we can discuss the shape there.
- Check first whether an existing option or hook, such as `before_send`, already covers the use case. We avoid offering two ways to do the same thing.
- If a reviewer suggests a different API on your PR, confirm it with them before re-implementing. Treat it as a question, not an instruction.

Follow the snapshot update/check commands in [CI-aligned checks](#ci-aligned-checks); CI checks for an outdated snapshot. A diff in `references/public_api_snapshot.txt` means your change touches public API.
