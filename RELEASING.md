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

## Failed or cancelled release

The independent [release failure notification workflow](.github/workflows/release-failure-notification.yml) checks every completed `main` release attempt and alerts if a job failed, timed out, or was cancelled. This includes runner startup failures and cancellations with job records. It also alerts when the run did not succeed, no job failed, and a publish job (`Publish posthog`, `Publish posthoganalytics`, or `Publish openfeature-provider-posthog`) was never created, for example when GitHub Actions stops a run before the publish jobs start. Once every publish job succeeds, alerts describe follow-up problems rather than claiming the release failed, and the workflow alerts if the released Slack notification was skipped.

Cancelled runs with no jobs are ignored, including superseded queued runs and manual cancellations before any job is created. A version-bump failure before any steps ran is also ignored when the rejection-reply step confirms an explicit rejection in the same attempt and no other job failed.

Job inspection retries transient GitHub API failures up to three times. If inspection still fails, it logs the HTTP status or error type and message, then posts a fallback alert with the overall run status and logs link so the release can be checked manually. Error diagnostics stay in the run log, not in the Slack message.

Alerts reply to the approval thread in the release approvals Slack channel. The release workflow preserves the thread timestamp in a run artifact for 30 days, independently of release approval. Failed-job reruns reuse the latest available thread from the same or an earlier attempt. If the timestamp is missing, invalid, or unavailable, the alert falls back to a new channel message, so it does not depend on the approval notification succeeding. The failure message surfaces Slack API errors rather than silently ignoring them. The notification workflow must be present on the default branch to run; local tests do not send Slack messages.
