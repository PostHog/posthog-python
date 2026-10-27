---
pypi/posthog: minor
---

Report flags that local evaluation could not resolve on `evaluate_flags()` snapshots. `FeatureFlagEvaluations.unresolved_flags` maps each such flag to an `UnresolvedFlagReason` (`experience_continuity`, `unsupported_definition`, `unresolved_dependency` or `missing_context`), and reading one reports the `local_evaluation_inconclusive` error instead of `flag_missing`. When definitions load, log one warning per flag that local evaluation can never resolve (experience continuity, a static cohort, an unknown operator, a malformed value, or a missing or circular flag dependency), and again only when its definition changes. `AsyncClient` evaluates only remotely, so its snapshots have no unresolved flags.

Behavior changes:

- A condition with a filter that local evaluation can't check and a filter that does not match now resolves locally as no match. Before, such a flag fell back to a remote request, or stayed unresolved with `only_evaluate_locally=True`.
- An inactive flag with experience continuity enabled now resolves to `false` locally instead of falling back to a remote request.
- A flag that depends on a flag with experience continuity enabled is no longer evaluated locally. It falls back to a remote request, or stays unresolved with `only_evaluate_locally=True`.
