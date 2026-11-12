# Offline evaluations

Upload evaluations you have already run, manage reusable scorers and datasets, and explicitly finish an experiment. The SDK returns server acknowledgments for every operation. It does not execute your application or judges, create traces, or upload in the background.

## Credentials and setup

Offline evaluations must be enabled for your PostHog project. Use the project's numeric ID and the app host (`https://us.posthog.com`, `https://eu.posthog.com`, or your self-hosted URL).

| Operation | Credential and scopes |
| --- | --- |
| Create experiments, upload results, complete/fail | Personal API key or project secret key with `offline_evaluation_ingestion:write` |
| Fetch scorers and versions | Personal API key with `llm_analytics:read` |
| Validate a composite formula without saving | Personal API key with `llm_analytics:read` |
| Calculate composites using previously stored inputs | Personal API key with ingestion access, `evaluation:read`, and `llm_analytics:read` |
| Create/update scorers and create versions | Personal API key with `llm_analytics:write` |
| Fetch datasets, items, and history | Personal API key with `dataset:read` |
| Create/update/archive/restore datasets and items | Personal API key with `dataset:write` |
| Read experiment tags | Personal API key with `evaluation:read` |
| Replace experiment tags | Personal API key with `evaluation:write` |

A personal key can cover both scorer management and uploads. Project secret keys can upload using a known scorer-version UUID without scorer read access. All operations use the explicit project/environment; scorer names are not unique.

Dataset operations also require the datasets feature to be enabled. Dataset reads, scorer reads, and experiment uploads use their own scopes; an upload-only credential cannot fetch a dataset. Keep personal keys in server-side scripts or CI secrets.

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
        model_version="model-b",
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

Set `model_version` on each item to compare executions from different models in one experiment. It identifies the application's model and overrides the default supplied to `create_experiment(model_version=...)`. Omit it or pass `None` to inherit that default on the server; without either value, the model remains unknown. Explicit model identifiers must be nonempty strings of at most 255 characters and are preserved without trimming. The declared model is immutable item content: changing, adding, or removing it after acceptance causes a `content_conflict`, even if the effective model is unchanged. Create a new item for a different model execution, and preserve the original declaration for retries.

`status="ok"` means the evaluator ran successfully. Its boolean, number, or category-list `value` is uploaded with its original polarity. Numeric scores use finite double-precision (binary64) values; integer inputs are rejected if conversion would change their value. Use `error`, `skipped`, or `not_applicable` without a value for other outcomes. `error_code` and a non-null `error_message` are valid only with `status="error"`.

Omitted payload fields, explicit `None`, and empty payloads are distinct:

```python
EvaluationItem()                 # No payload declaration.
EvaluationItem(payload={})       # Explicitly empty payload.
EvaluationItem(input=None)       # Payload contains a null input.
```

The client context manager releases connections. Call `complete()` explicitly after uploads succeed, or `fail()` when you deliberately want to close the experiment as failed. Neither exceptions nor async cancellation automatically close an experiment. Expected counts include every outcome status; the server checks them on completion.

## Composite item scores

Composite scorers calculate one score per item on the PostHog server from exact reported scorer versions. They can return numeric, boolean, or categorical values. This requires a backend with composite scorer support; ordinary uploads remain compatible with older backends.

Provision a composite once, using a personal key with scorer write access. This example assumes `correctness_version_id` identifies a reported boolean scorer and `relevance_version_id` a reported numeric scorer:

```python
from posthog.ai.evaluations import ScorerComposition

composition: ScorerComposition = {
    "expression": "(number(correct) + relevance) / 2",
    "inputs": [
        {"alias": "correct", "scorer_version_id": correctness_version_id},
        {"alias": "relevance", "scorer_version_id": relevance_version_id},
    ],
}
# Optional; read scope is sufficient and no scorer/version is created.
evaluations.scorers.validate_composition(
    kind="numeric", composition=composition, config={"min": 0, "max": 1},
)
overall = evaluations.scorers.create(
    name="Overall",
    kind="numeric",
    calculation="composite",
    config={"min": 0, "max": 1, "passing_rule": {"operator": "gte", "threshold": 0.8}},
    composition=composition,
)
overall_version_id = overall.current_version_id
```

Save that version UUID for your runs. Creating scorers/versions is not idempotent, so a lost response requires inspecting server state before repeating creation. Update a formula, output configuration, or pinned input through `create_version(scorer_id, config=..., composition=..., base_version=...)`. Supply the complete composition each time. New source versions never update a composite automatically.

Upload the reported inputs and explicitly request the calculation:

```python
experiment = evaluations.create_experiment(
    name="Composite example", expected_item_count=1, expected_result_count=3,
)
item = EvaluationItem(input="Capital of France?", output="Paris")
receipt = experiment.upload_results([
    EvaluationResult(item=item, scorer_version_id=correctness_version_id, value=True),
    EvaluationResult(item=item, scorer_version_id=relevance_version_id, value=0.8),
    EvaluationResult(item=item, scorer_version_id=overall_version_id, calculate=True),
])
for chunk in receipt.chunks:
    for outcome in chunk.results:
        print(outcome.scorer_version_id, outcome.status, outcome.error_code)
experiment.complete()
```

Calculation declarations contain only the item reference, scorer version, and `calculate=True`. Do not supply a value, reasoning, metadata, evaluator trace, or evaluation time. An item may also be referenced by its already accepted UUID.

Project secret keys and ingestion-only personal keys must supply **every pinned input result in the same HTTP request**, including on retries. Previously uploaded inputs may be resubmitted with their original values and metadata. Reusing stored inputs without resubmitting them requires both `evaluation:read` and `llm_analytics:read`, in addition to ingestion access. Composite requests and retries also require current access to the composite and its inputs.

The SDK keeps all supplied results for a composite-bearing item together without fetching scorer definitions. It preserves order, so interleaved composite items can form a larger indivisible group. Group each item's results together for large batches. If a group exceeds 1,000 items/results or 5 MiB, the entire call fails locally before any upload. The SDK cannot check that your supplied inputs match the pinned formula without read access; the server validates this. Missing inputs reject the whole HTTP request. Inputs can appear before or after their composite in that request.

HTTP success acknowledges persistence, not successful calculation. Input errors become `composite_input_error`; formula errors or invalid output values become `composite_formula_error`. Input statuses propagate in the order `error`, `skipped`, `not_applicable`. Check each receipt's `status` and `error_code`; no result-read permission is needed. For ordinary results on an older backend, `status=None` means the backend omitted the outcome. Composite acknowledgments require an outcome.

All accepted composite outcomes count toward `expected_result_count`, including errors and skips. Derived results add no uploaded-result billing usage. Omitting a composite leaves it unrequested. Exact retries return the stored outcome, including after completion; formulas are not recalculated on retry or read. Persist declarations with `to_dict` and restore them with `from_dict` for exact recovery.

Formulas support 1–16 reported inputs and at most 1,024 characters; aliases start with a lowercase letter and contain lowercase letters, digits, or underscores, up to 32 characters. Nested composites are unsupported. Use `number(flag)` for explicit boolean conversion and `"good" in categories` for categorical membership. Boolean inputs retain their raw meaning regardless of passing rules. Numeric composites support bounds and passing thresholds, but no non-null `step`. Formula syntax, input access, and output types are validated by the server; runtime values and bounds are checked during ingestion.

Read `scorer.calculation`, `scorer.composition`, and `scorer.input_scorers` to inspect saved inputs and their pinned/latest versions. `dependent_composite_count` counts accessible active dependents; `scorers.list(calculation="composite", input_definition_id=source_id)` retrieves them. Historical versions retain their saved composition. All operations also exist on `AsyncOfflineEvaluations`.

The runnable capital-city example can include a composite consuming only its configured boolean scorer version. Provision one with expression `number(correct)`, pin that boolean version as `correct`, and pass its composite version UUID when creating a new state file:

```bash
uv run python examples/example-offline-evaluations/main.py \
  --state composite-run.json --composite-version-id 'your-composite-version-uuid'
```

The state file includes both reported and calculation declarations and the combined expected count. Reusing the file replays that saved run regardless of newly supplied scorer flags.

## Datasets and reproducible evaluations

Use `evaluations.datasets` to manage PostHog-hosted datasets with the same client configuration as scorers and experiments. Dataset names are unique within a project; keep the returned UUID for subsequent operations.

Provision a dataset once with a personal key that has `dataset:write`:

```python
dataset = evaluations.datasets.create(
    name="Capital city regression cases",
    description="Cases for the offline evaluation example",
    tags=["regression"],
)
case = evaluations.datasets.create_item(
    dataset.id,
    client_item_id="capital-france",
    input="Capital of France?",
    expected_output="Paris",
    metadata={"category": "geography"},
)
print(dataset.id)
```

An identical item create with the same `client_item_id` returns the existing active item. Reusing that key with different current content, or for an archived item, raises `EvaluationAPIError` with `code="client_item_id_conflict"`. Choose a stable key per case and retain it across process restarts. Creating the dataset itself is not idempotent; keep its ID instead of provisioning it on each evaluation run.

Pin both the dataset and scorer before running your application:

```python
from posthog.ai.evaluations import EvaluationItem, OfflineEvaluations

def capital_answer(question):
    return {"Capital of France?": "Paris", "Capital of Italy?": "Rome"}.get(
        question, "I don't know"
    )

with OfflineEvaluations(project_id=123, secret_key=personal_api_key) as evaluations:
    snapshot = evaluations.datasets.snapshot(dataset_id)  # Resolve latest once.
    scorer = evaluations.scorers.get(scorer_id)  # A boolean exact-match scorer.
    version_id = scorer.current_version_id
    experiment = evaluations.create_experiment(
        name="Capital city regression",
        dataset_revision_id=snapshot.revision_id,
        application_version="your-commit-sha",
        run_source="ci",
    )

    for case in snapshot.iter_items():
        output = capital_answer(case.input)
        item = EvaluationItem.from_dataset_item(case, output=output)
        if case.expected_output is None:
            experiment.upload_result(
                item=item, scorer_version_id=version_id, status="not_applicable",
            )
        else:
            experiment.upload_result(
                item=item, scorer_version_id=version_id,
                value=output == case.expected_output,
            )

    experiment.complete()
```

This example uses string inputs. Replace `capital_answer` and the equality check with your own application and judge. The SDK does not run scorer definitions. For multiple scorers, reuse the same `EvaluationItem` and upload one result per scorer-version UUID. For independent trials, call `from_dataset_item` again and pass a distinct `trial`, such as `trial="2"`; each call generates a new execution UUID. The helper defaults `case_key` to `client_item_id`, falling back to the stable dataset item UUID, and accepts an explicit override.

`from_dataset_item` copies input, expected output, metadata, and immutable item provenance. It never uses `source_output` as a new execution's output or expected answer, and never uses the original source trace as the new application's trace. Pass `application_trace_id` explicitly for the new execution. Omit `output` if execution failed before producing one, then upload an error outcome. Omitted output differs from `output=None`.

Pass `EvaluationItem.from_dataset_item(case, output=output, model_version="model-b")` to attribute a dataset execution to a model. Calling it again with another model creates a new execution UUID while preserving the same case key and dataset-item version. Omitting the model or passing `None` leaves experiment fallback to the server; dataset metadata and source traces never supply the execution model.

### Snapshot and pagination behavior

- `snapshot(dataset_id, revision=12)` selects a historical **number**. The returned `revision_id` is the **UUID** to pass to `create_experiment`. The backend derives the remaining hosted-dataset identifiers from that UUID.
- Every item request uses the pinned revision, even if another process edits or archives items during iteration. `DatasetItem.dataset_revision_id` identifies the revision that introduced that item version; it can differ from the snapshot's revision ID. Do not use it as the experiment's revision.
- `snapshot()` fetches metadata only. `snapshot.iter_items(page_size=25)` lazily fetches active items, one page at a time. Keep the client open. Repeating iteration starts again at the first page without caching; read errors propagate rather than silently returning a partial dataset.
- A new dataset has no revision until its first item is added, so `snapshot()` raises `ValueError`. An existing revision with zero active items yields an empty iterator. Archived datasets remain readable.
- `snapshot.dataset` contains current descriptive metadata observed when the snapshot was resolved. Names, descriptions, tags, and dataset metadata are not historical item content.

For explicit paging, each `list*` method returns typed `results`, `count`, and `next_offset`. Continue with the same filters until `next_offset is None`. Dataset/revision pages allow at most 100 entries, item pages 25, and item-history pages 100. `list_items` without a revision reads live state on each request; use a snapshot for an evaluation run. A historical snapshot may make multiple metadata requests to find its revision UUID.

| Method | Purpose |
| --- | --- |
| `list(search=..., tags=..., tag_match="any", archived=False, order_by="-created_at")` | Find datasets; `archived=True` selects archived datasets only |
| `get(dataset_id)` | Read current dataset metadata and revision references |
| `create(name=..., description=..., metadata=..., tags=...)` | Create an empty dataset |
| `update(dataset_id, name=..., description=..., metadata=..., tags=...)` | Replace supplied metadata fields without creating an item revision |
| `archive(dataset_id)` / `restore(dataset_id)` | Change dataset archive state; restore leaves individual item states unchanged |
| `list_revisions(dataset_id)` | Read revision history, newest first |
| `list_items(dataset_id, revision=..., archived=False)` | Read one page of current or historical items |
| `get_item(item_id, revision=...)` | Read one stable item at the current or selected dataset revision |
| `create_item(dataset_id, input=..., ...)` | Add a case and its first version |
| `update_item(item_id, base_version=..., input=..., expected_output=..., metadata=...)` | Update editable content with optimistic concurrency |
| `archive_item(item_id, base_version=...)` | Archive an active item with a new version |
| `restore_item(item_id, base_version=..., source_version=...)` | Restore an archived item, optionally copying a historical item version |
| `list_item_versions(item_id)` | Read item history, newest first |

Dataset and item metadata are JSON objects. Inputs accept any non-null JSON value; expected/source outputs can be null. Writes require finite numbers, JSON string keys, and at most 32 nested containers. The server enforces dataset, item, version, and payload size limits; failures retain their codes and details in `EvaluationAPIError`. JSON values returned by reads are local copies. Editing them does not write to PostHog.

To save source provenance on creation, supply `source_trace_id` together with a timezone-aware `source_timestamp`; optionally add `source_event_id`. Source fields and `client_item_id` cannot be edited later.

### Updates and recovery

```python
case = evaluations.datasets.get_item(item_id)
updated = evaluations.datasets.update_item(
    case.id,
    base_version=case.version,
    expected_output=None,  # Clear the answer; omitting it preserves it.
    metadata={},          # Clear metadata; omitting it preserves it.
)
```

Pass the **observed item version number** as `base_version` for updates, archive, and restore. A stale version raises a 409 `stale_version` conflict with details in `error.response`, including `current_version`. The SDK does not fetch a newer version and overwrite it automatically. Changed content creates a version and dataset revision; identical content is a no-op. Dataset descriptions clear with `""`, metadata with `{}`, and tags with `[]`; those fields do not accept `None`.

Reads retry within the configured transport limits. Item creates retry only when you supply a `client_item_id`. Other mutations never retry automatically. After an unknown write outcome, inspect the current state before issuing another mutation. Dataset errors use the same `EvaluationAPIError` as scorer and experiment operations, including `status`, `code`, `response`, and `persistence`.

Run the supplied example against the provisioned dataset:

```bash
uv run python examples/example-offline-evaluations/main.py \
  --dataset-id '<dataset-uuid>' --state dataset-run.json

# Optional: select an earlier numeric revision for a new run.
uv run python examples/example-offline-evaluations/main.py \
  --dataset-id '<dataset-uuid>' --dataset-revision 1 --state historical-run.json
```

The example prepares and saves the pinned revision, execution identities, scorer version, and results **before the first experiment write**. If the state file already exists, replay uses that file without fetching a newer dataset, selecting another scorer version, or running the application again. Dataset selection arguments apply only when preparing a new state file. Choose a new file for a new run. This small example materializes its results; for larger workloads, use caller-sized batches and persist each prepared batch before upload.

### Async datasets

Await management methods and snapshot creation. Use `async for` on `iter_items()`; iteration itself is an async iterator, not an awaitable list.

```python
from posthog.ai.evaluations import AsyncOfflineEvaluations, EvaluationItem

async with AsyncOfflineEvaluations(
    project_id=123, secret_key=personal_api_key,
) as evaluations:
    snapshot = await evaluations.datasets.snapshot(dataset_id)
    scorer = await evaluations.scorers.get(scorer_id)
    experiment = await evaluations.create_experiment(
        name="Async regression", dataset_revision_id=snapshot.revision_id,
    )
    async for case in snapshot.iter_items():
        output = capital_answer(case.input)
        item = EvaluationItem.from_dataset_item(case, output=output)
        if case.expected_output is None:
            await experiment.upload_result(
                item=item, scorer_version_id=scorer.current_version_id,
                status="not_applicable",
            )
        else:
            await experiment.upload_result(
                item=item, scorer_version_id=scorer.current_version_id,
                value=output == case.expected_output,
            )
    await experiment.complete()
```

Install `posthog[async]` for async support. Cancellation propagates and does not automatically complete or fail an experiment.

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

## Tags

Assign tags when creating a scorer, or replace them with a metadata update:

```python
scorer = evaluations.scorers.create(
    name="Answer relevance",
    kind="numeric",
    config=NumericScorerConfig(min=0, max=1),
    tags=["quality", "production"],
)
print(scorer.tags)  # ("production", "quality")
scorer = evaluations.scorers.update(scorer.id, tags=["quality"])
page = evaluations.scorers.list(tags=["quality", "production"], tag_match="all")
evaluations.scorers.update(scorer.id, tags=[])  # Remove all tags.
```

Omitting `tags` preserves existing tags on updates. `tags=[]` clears them; `None` is not a valid replacement. Lists and tuples are accepted. The server trims and lowercases names, removes duplicates, and sorts them. Returned tags are immutable tuples. Writes accept up to 100 names, each nonblank and at most 255 characters after trimming.

List filters accept up to 20 tags. `tag_match="any"` (the default) matches at least one selected tag; `"all"` requires every selected tag. An empty filter disables tag filtering. Preserve these filters when requesting subsequent pages.

Experiment tags are separate from the creation/upload declaration. Set them on a newly created or resumed handle, including after completion:

```python
experiment = evaluations.resume_experiment(experiment_id)
tags = experiment.set_tags(["quality", "baseline"])
print(tags)  # ("baseline", "quality")
print(experiment.get_tags())
experiment.set_tags([])  # Remove all tags.
```

Reading and writing experiment tags require the personal-key scopes listed above; an upload-only project secret key cannot manage tags. Tag updates do not change the saved `submission`, lifecycle `receipt`, or scorer versions. They are not retried automatically: after a lost acknowledgment, read the current tags before deciding whether to repeat the replacement, since another caller may have edited them.

The async client exposes the same API: await scorer methods and `experiment.get_tags()` / `experiment.set_tags(...)`.

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
