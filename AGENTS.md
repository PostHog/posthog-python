# AGENTS.md

Guidance for coding agents working on the PostHog Python SDK (`posthog`). Runtime: `posthog/`; main tests: `posthog/test/`.

## Start here

- Use `uv`; follow [CONTRIBUTING.md](./CONTRIBUTING.md) for setup and [checks](./CONTRIBUTING.md#ci-aligned-checks). Run the smallest relevant tests first.
- For `openfeature-provider/` changes, also follow its [contributor guide](./openfeature-provider/CONTRIBUTING.md); root pytest collection does not cover it.
- Read [RELEASING.md](./RELEASING.md) when adding changesets or changing builds/publishing. Releases publish both `posthog` and its generated `posthoganalytics` mirror.
- For public API changes, update/review `references/public_api_snapshot.txt` and run its check as described in the contributor guide.

## Public API and specifications

Follow [Public API changes](./CONTRIBUTING.md#public-api-changes). As an agent, also:

- When reviewing or fixing someone else's PR, don't ask for or open an issue. Note an external contributor's public API change when it has neither an agreed issue nor an API-defining published spec.
- The author is a PostHog maintainer when the PR's `author_association` is `MEMBER` or `OWNER` (`gh api repos/PostHog/posthog-python/pulls/<number> --jq .author_association`) or, before a PR exists, when `gh api orgs/PostHog/members/$(gh api user --jq .login)` succeeds. If the check fails or can't run, treat the author as an external contributor.
- A published [sdk-spec](https://github.com/PostHog/sdk-specs) that defines the API counts as the agreement, so no issue is needed.
- For an external contributor with no agreed issue and no API-defining spec, stop before implementing and draft the issue body for the user to post. Open it only if they ask. The review/fix exception above still applies.
- Before implementing or reviewing SDK behavior, check [PostHog/sdk-specs](https://github.com/PostHog/sdk-specs) for a covering spec (its README lists every capability). Use it as the cross-SDK contract for changed behavior and call out divergence in the PR description. Don't fix or flag discrepancies in code the PR doesn't touch. If no spec covers it, carry on.

## Capture changes

Before changing capture configuration, serialization, routing, or retries, read the relevant implementation and tests.

Capture v1 is the only capture protocol (`capture` posts to `/i/v1/analytics/events`, `capture_ai` to `/i/v1/ai/events`); per-event `options` sent as given, layered over `super_options` and context options, with legacy `$` option properties hoisted once after `before_send`; `$set`/`$set_once` relocation; compression (gzip, zlib-wrapped deflate, optional zstd, default none), set per lane by `capture_compression` and `capture_ai_compression`; partial-only per-event retries with stable identity; accumulated drop reporting even on 2xx; terminal v1 `429`; `Retry-After` as a minimum bounded by the shared 30s ceiling; and inline blocking retries with `sync_mode=True`.

## Mirror and build safety

- Prefer relative SDK-internal imports (e.g. `from .client import Client`). Absolute `posthog...` imports can collide with the PostHog app's own package after mirror generation rewrites imports to `posthoganalytics`.
- Run focused tests for mirror-sensitive changes; when app testing is relevant, follow the contributor guide's mirror workflow. **`make prep_local` deletes and recreates `../posthog-python-local`; verify no work there needs preserving before every use.** Do not commit generated `posthoganalytics/` directories.
- `make build_release_analytics` temporarily rewrites/copies source and package files and clears `dist/`. Publish or preserve the `posthog` build artifacts before running it. Require a clean working tree before running it and verify the tree is clean afterward.
