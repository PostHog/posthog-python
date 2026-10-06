# Offline evaluations

Upload evaluations you have already run, manage reusable scorers, and explicitly finish an experiment. The SDK returns server acknowledgments for every operation. It does not execute your application or judges, create traces, or upload in the background.

## Credentials and setup

Offline evaluations must be enabled for your PostHog project. Use the project's numeric ID and the app host (`https://us.posthog.com`, `https://eu.posthog.com`, or your self-hosted URL).

| Operation | Credential and scopes |
| --- | --- |
| Create experiments, upload results, complete/fail | Personal API key or project secret key with `offline_evaluation_ingestion:write` |
| Fetch scorers and versions | Personal API key with `llm_analytics:read` |
| Create/update scorers and create versions | Personal API key with `llm_analytics:write` |

A personal key can cover both scorer management and uploads. Project secret keys can upload using a known scorer-version UUID without scorer read access. All operations use the explicit project/environment; scorer names are not unique.

From this repository's root:

```bash
export POSTHOG_PROJECT_ID=123
export POSTHOG_SECRET_KEY='your-secret-key'
export POSTHOG_HOST='https://us.posthog.com'

# Optional one-time provisioning; requires a personal key.
uv run python examples/example-offline-evaluations/main.py --create-scorer

# Use the stable scorer ID printed above; its current version is fetched once.
export POSTHOG_SCORER_ID='your-scorer-uuid'
uv run python examples/example-offline-evaluations/main.py --state capital-run.json
```

Alternatively, set `POSTHOG_SCORER_VERSION_ID` to an existing **boolean** version UUID. This takes precedence over `POSTHOG_SCORER_ID` and avoids discovery. The synthetic example stores its experiment, items, and results in the state file before uploading. Rerunning with that file replays the same experiment; a new file starts a new experiment. The state includes evaluation data and never includes your API key. No LLM provider or API key is needed for this example.

## Upload a run

```python
from posthog.ai.evaluations import (
    EvaluationItem,
    EvaluationResult,
    OfflineEvaluations,
)

with OfflineEvaluations(
    project_id=123,
    secret_key=secret_key,
    host="https://us.posthog.com",
) as evaluations:
    scorer = evaluations.scorers.get(scorer_id)
    version_id = scorer.current_version_id  # Pin once for this run.
    experiment = evaluations.create_experiment(
        name="support-agent",
        run_source="ci",  # Optional: ci, local, or scheduled.
        expected_item_count=1,
        expected_result_count=1,
        application_version="your-commit-sha",
    )
    item = EvaluationItem(
        input="What is the capital of France?",
        output="Paris",
        expected_output="Paris",
        case_key="capital-france",
    )
    receipt = experiment.upload_result(
        item=item,
        scorer_version_id=version_id,
        value=True,
    )
    print(receipt.results[0].id, receipt.results[0].created)
    experiment.complete()
```

Use one `EvaluationItem` for the application's execution and reuse it across scorer results. `upload_results([EvaluationResult(...), ...])` uploads a finite sequence, eagerly and sequentially. It splits at 1,000 items/results or 5 MiB of UTF-8 JSON per request, whichever comes first. Item payloads are limited to 1 MiB; result payloads to 256 KiB. Payload JSON supports at most 32 nested containers. The SDK raises for invalid values and oversized payloads without truncating them. Memory use scales with the sequence you supply, so submit caller-sized batches for large datasets.

`status="ok"` means the evaluator ran successfully. Its boolean, number, or category-list `value` is uploaded with its original polarity. Numeric scores use finite double-precision (binary64) values; integer inputs are rejected if conversion would change their value. Use `error`, `skipped`, or `not_applicable` without a value for other outcomes. `error_code` and a non-null `error_message` are valid only with `status="error"`.

Omitted payload fields, explicit `None`, and empty payloads are distinct:

```python
EvaluationItem()                 # No payload declaration.
EvaluationItem(payload={})       # Explicitly empty payload.
EvaluationItem(input=None)       # Payload contains a null input.
```

The client context manager releases connections. Call `complete()` explicitly after uploads succeed, or `fail()` when you deliberately want to close the experiment as failed. Neither exceptions nor async cancellation automatically close an experiment. Expected counts include every outcome status; the server checks them on completion.

## Typed scorer configuration and polarity

Configurations are public `TypedDict` types: editors suggest fields and type checkers connect `create(kind=...)` to the correct configuration. They are ordinary dictionaries at runtime; server validation remains authoritative.

```python
from posthog.ai.evaluations import (
    BooleanScorerConfig,
    CategoricalPassingRule,
    CategoricalScorerConfig,
    CategoricalScorerOption,
    NumericPassingRule,
    NumericScorerConfig,
)

boolean = BooleanScorerConfig(
    true_is_failure=True,
    true_label="Unsafe",
    false_label="Safe",
)
numeric = NumericScorerConfig(
    min=0,
    max=1,
    step=0.01,
    passing_rule=NumericPassingRule(operator="gte", threshold=0.8),
)
categorical = CategoricalScorerConfig(
    options=[
        CategoricalScorerOption(key="safe", label="Safe"),
        CategoricalScorerOption(key="helpful", label="Helpful"),
    ],
    selection_mode="multiple",
    min_selections=1,
    max_selections=2,
    passing_rule=CategoricalPassingRule(categories=["safe", "helpful"]),
)
scorer = evaluations.scorers.create(
    name="Answer relevance",
    kind="numeric",
    config=numeric,
)
version_id = scorer.current_version_id
```

Boolean `true_is_failure=True` makes false pass; false, null, or omission makes true pass. Numeric passing rules support `gte` and `lte`; the finite threshold must be within any configured bounds. Numeric bounds, step, and passing rule can be omitted or null. For categorical scores, **every** returned category must be in the passing list. Selection defaults to `single`; minimum and maximum selections apply only to `multiple`. Numeric/categorical scores are neutral when their passing rule is omitted or null. Polarity is pinned to each immutable version.

Manage metadata independently from versioned configuration:

```python
scorer = evaluations.scorers.get(scorer_id)
evaluations.scorers.update(scorer.id, description="Updated description", archived=False)
updated = evaluations.scorers.create_version(
    scorer.id,
    config=scorer.config,
    base_version=scorer.current_version,
)
version = evaluations.scorers.get_version(scorer.id, updated.current_version_id)
```

`create_version()` returns the updated scorer. Identical configuration is allowed when, for example, your judging prompt changes. Supply `base_version` for optimistic concurrency; a stale version raises a 409 error without silently refreshing and retrying. Omit metadata fields to preserve them; `description=None` clears the description. Scorer kind cannot change.

`scorers.list(limit=100, offset=0, search=..., kind=..., archived=..., order_by=...)` returns a page with `results`, `count`, and `next_offset`; preserve the filters when continuing. Omitted `archived` lists active scorers. `list_versions(scorer_id, limit=50, cursor=...)` returns newest versions first and a `next_cursor`; its maximum limit is 100. Stop when the corresponding continuation is `None`.

Scorer creation, version creation, and metadata updates are never automatically retried. If a response is lost, inspect server state before repeating a mutation. Version creation is not made idempotent by `base_version`.

## Receipts, errors, and recovery

Upload receipts contain persisted item/result UUIDs, original acceptance times, and `created` flags. `created=False` acknowledges an exact duplicate. A bulk upload returns `chunks`; each chunk commits independently. A malformed or incomplete acknowledgment raises rather than reporting success.

`EvaluationAPIError` exposes `status`, `code`, `detail`, `attr`, `errors`, `response`, `retry_after`, and `persistence`:

- `rejected`: the server rejected the request.
- `unknown`: it may have committed, for example if its response was lost.
- `not_sent`: it was not sent.

Errors do not include evaluation content or credentials in their message; inspect their structured attributes explicitly when needed. `BulkUploadError` also exposes `completed.chunks`, `failed_index`, the exact `failed_chunk`, and `pending_results`. After resolving the cause, retry the pending results against the same experiment. An uncertain chunk can safely be replayed with identical identities and content. Do not generate new item IDs during recovery.

```python
from posthog.ai.evaluations import BulkUploadError

try:
    receipts = experiment.upload_results(results)
except BulkUploadError as error:
    acknowledged = error.completed
    pending = error.pending_results
    # Preserve these and inspect the cause before retrying:
    # experiment.upload_results(pending)
```

Creation generates an experiment UUID and start time once. A successful handle exposes its full `submission`; creation errors expose `error.submission`. Retry `evaluations.create_experiment(**submission)` with all original fields to recover a failed acknowledgment. `resume_experiment(id)` creates a local handle without a read request; it does not create missing experiments.

For recovery after process termination, persist explicit experiment `id`, `started_at`, and all creation fields **before** the creation call. Also save every `item.to_dict()` and `result.to_dict()` before upload. Restore items with `EvaluationItem.from_dict(...)`; attach the matching restored item with `EvaluationResult.from_dict(saved_result, item=item)`. This preserves payloads and re-declares items safely if their prior acknowledgment was lost. The example's state file implements this pattern; the SDK itself does not write to disk.

Default request timeout is 15 seconds with at most three retries. Idempotent offline operations preserve request bytes across retries and honor rate-limit delays. Authentication, validation, and conflicts surface immediately. There is no automatic completion after a failed upload.

## Async

Install `posthog[async]` for native async support. Methods and receipts match the synchronous client:

```python
from posthog.ai.evaluations import AsyncOfflineEvaluations

async with AsyncOfflineEvaluations(project_id=123, secret_key=secret_key) as evaluations:
    scorer = await evaluations.scorers.get(scorer_id)
    experiment = await evaluations.create_experiment(name="async-run")
    await experiment.upload_result(
        item=item, scorer_version_id=scorer.current_version_id, value=True,
    )
    await experiment.complete()
```

`resume_experiment(id)` is local in both clients and does not require `await`. Use each async client on one event loop. Cancellation propagates; replay the persisted submission when an in-flight upload's outcome is uncertain.
