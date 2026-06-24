# AGENTS.md

Guidance for coding agents working in `posthog-python`.

## Repo context

- This repository contains the PostHog Python SDK, published as `posthog`.
- The main runtime package is `posthog/`; tests live under `posthog/test/`.
- The project uses `uv` for local development. See `CONTRIBUTING.md` for setup.
- Keep edits targeted and follow existing patterns. Prefer adding or updating tests near the behavior you change.

## Validation

Useful checks:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy --no-site-packages --config-file mypy.ini . | uv run mypy-baseline filter
uv run pytest --verbose --timeout=30
uv run python -W error -c "import posthog"
```

For focused changes, run the smallest relevant `uv run pytest ...` command first.

If public API surface changes, update/check `references/public_api_snapshot.txt` with:

```bash
make public_api_snapshot
make public_api_check
```

## `posthoganalytics` mirror package

This repo also publishes `posthoganalytics`, a generated mirror of `posthog` used by the PostHog app. The mirror is created by copying `posthog/` to `posthoganalytics/` and rewriting absolute imports such as `from posthog.foo import ...` to `from posthoganalytics.foo import ...`.

Important when editing SDK-internal code:

- Prefer relative imports for imports within the SDK package, especially in runtime modules under `posthog/`.
  - Good: `from .client import Client`, `from .exception_utils import extract_exception_properties`
  - Risky: `from posthog.client import Client`
- Absolute `posthog...` imports inside SDK modules can break the `posthoganalytics` mirror when it is imported inside an application that also has its own `posthog` package/module on `sys.path`.
- Test mirror-sensitive changes by running the normal focused tests and, when relevant, `make prep_local` to generate a local `posthoganalytics` copy for testing in the PostHog app.
- Do not commit generated `posthoganalytics/` directories; they are build/local artifacts.

## Release/build notes

- `make build_release` builds the `posthog` distribution.
- `make build_release_analytics` builds the `posthoganalytics` distribution and temporarily rewrites/copies package files; ensure the working tree is clean before and after running it.
- Release flow publishes both packages; see `RELEASING.md`.
