# Releasing

This repository uses [Sampo](https://github.com/bruits/sampo) for versioning and changelog generation, with GitHub Actions publishing packages to PyPI.

Published packages:

- `posthog` — root package, published to PyPI and mirrored as `posthoganalytics`
- `posthoganalytics` — generated build-time mirror of `posthog`, published to PyPI without its own tag or GitHub Release
- `openfeature-provider-posthog` — OpenFeature provider package under `openfeature-provider/`, published to PyPI

Package changelogs live with each package and are linked from the root `CHANGELOG.md`:

- `posthog/CHANGELOG.md`
- `openfeature-provider/CHANGELOG.md`

Release tags use package-prefixed names:

- `posthog-v{version}`
- `openfeature-provider-posthog-v{version}`

1. When making changes, include a changeset: `sampo add`
2. Create a PR with your changes and the changeset file
3. Merge to `main` (no release label required)
4. Approve the release in Slack when prompted — this triggers the version bump, publishes packages with changed versions to PyPI, creates package tags, and creates GitHub Releases for tagged packages

You can also trigger a release manually via the workflow's `workflow_dispatch` trigger (still requires pending changesets).

> [!IMPORTANT]
> Changesets must live in **`.sampo/changesets/`** (this is where `sampo add` puts them). Do **not** create them in the legacy `.changeset/` directory — Sampo and the `Release` workflow only read `.sampo/changesets/*.md`, so a changeset placed anywhere else is silently ignored and **no release is triggered**.
