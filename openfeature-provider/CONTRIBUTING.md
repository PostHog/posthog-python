# Contributing to openfeature-provider-posthog

This package is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/)
member of the [posthog-python](https://github.com/PostHog/posthog-python) repo
(declared in the root `pyproject.toml`'s `[tool.uv.workspace]`), so it is always
developed and tested against the in-repo `posthog`.

## Local development

From this directory (`openfeature-provider/`):

```bash
uv sync --package openfeature-provider-posthog --extra dev
uv run --package openfeature-provider-posthog pytest
uv run --package openfeature-provider-posthog ruff format --check .
uv run --package openfeature-provider-posthog ruff check .
uv run --package openfeature-provider-posthog mypy .
```

## Build

Build the distribution into this package's own `dist/` (kept separate from the
`posthog` dist):

```bash
uv build --package openfeature-provider-posthog --out-dir dist
```

## Releasing

Versioning and publishing are handled by the repo's Sampo-based release flow.
Add a changeset targeting this package and the release workflow builds, publishes,
and tags it:

```bash
sampo add -p pypi/openfeature-provider-posthog -b patch -m "your change"
```
