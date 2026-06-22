# Releasing

This repository uses [Sampo](https://github.com/bruits/sampo) for versioning and changelog generation, with GitHub Actions publishing packages to PyPI.

1. When making changes, include a changeset: `sampo add`
2. Create a PR with your changes and the changeset file
3. Merge to `main` (no release label required)
4. Approve the release in Slack when prompted — this triggers the version bump, publishes both `posthog` and the `posthoganalytics` mirror package to PyPI, creates a git tag, and creates a GitHub Release

You can also trigger a release manually via the workflow's `workflow_dispatch` trigger (still requires pending changesets).

> [!IMPORTANT]
> Changesets must live in **`.sampo/changesets/`** (this is where `sampo add` puts them). Do **not** create them in the legacy `.changeset/` directory — Sampo and the `Release` workflow only read `.sampo/changesets/*.md`, so a changeset placed anywhere else is silently ignored and **no release is triggered**.
