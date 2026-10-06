"""Run a synthetic offline evaluation, with explicit scorer setup and durable replay."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from posthog.ai.evaluations import (
    BooleanScorerConfig,
    BulkUploadError,
    EvaluationAPIError,
    EvaluationItem,
    EvaluationResult,
    OfflineEvaluations,
)


def resolve_version(evaluations: OfflineEvaluations) -> str:
    version_id = os.environ.get("POSTHOG_SCORER_VERSION_ID")
    if version_id:
        return str(UUID(version_id))
    scorer_id = os.environ.get("POSTHOG_SCORER_ID")
    if not scorer_id:
        raise ValueError(
            "Set POSTHOG_SCORER_ID or POSTHOG_SCORER_VERSION_ID, or run --create-scorer once."
        )
    scorer = evaluations.scorers.get(scorer_id)
    if scorer.kind != "boolean":
        raise ValueError("This example requires a boolean scorer.")
    return scorer.current_version_id


def prepare_state(
    evaluations: OfflineEvaluations, project_id: int, host: str
) -> dict[str, Any]:
    version_id = resolve_version(evaluations)
    cases = [
        {
            "key": "france",
            "input": "Capital of France?",
            "output": "Paris",
            "expected": "Paris",
        },
        {
            "key": "italy",
            "input": "Capital of Italy?",
            "output": "Milan",
            "expected": "Rome",
        },
    ]
    items = [
        EvaluationItem(
            case_key=case["key"],
            input=case["input"],
            output=case["output"],
            expected_output=case["expected"],
        )
        for case in cases
    ]
    results = [
        EvaluationResult(
            item=item,
            scorer_version_id=version_id,
            value=case["output"] == case["expected"],
            reasoning="Exact string comparison",
        )
        for item, case in zip(items, cases)
    ]
    return {
        "project_id": project_id,
        "host": host,
        "experiment": {
            "id": str(uuid4()),
            "name": "Capital city example",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "run_source": "local",
            "expected_item_count": len(items),
            "expected_result_count": len(results),
        },
        "items": [item.to_dict() for item in items],
        "results": [result.to_dict() for result in results],
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    # Persist identities and content before the first experiment write. Never
    # overwrite an existing run or record credentials in its state file.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, allow_nan=False, indent=2)
        file.flush()
        os.fsync(file.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--create-scorer",
        action="store_true",
        help="Create a boolean scorer once and print its IDs, then exit.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("offline-evaluation-state.json"),
        help="Create a new prepared run here, or replay the existing run exactly.",
    )
    args = parser.parse_args()
    project_id = int(os.environ["POSTHOG_PROJECT_ID"])
    host = os.environ.get("POSTHOG_HOST", "https://us.posthog.com")
    with OfflineEvaluations(
        project_id=project_id, secret_key=os.environ["POSTHOG_SECRET_KEY"], host=host
    ) as evaluations:
        if args.create_scorer:
            scorer = evaluations.scorers.create(
                name="Capital exact match",
                kind="boolean",
                config=BooleanScorerConfig(
                    true_is_failure=False, true_label="Correct", false_label="Incorrect"
                ),
            )
            print(f"Scorer ID: {scorer.id}")
            print(f"Version ID: {scorer.current_version_id}")
            return

        if args.state.exists():
            state = json.loads(args.state.read_text(encoding="utf-8"))
            if state["project_id"] != project_id or state["host"] != host:
                raise ValueError(
                    "The saved run belongs to a different project or host."
                )
        else:
            state = prepare_state(evaluations, project_id, host)
            save_state(args.state, state)

        items = {item["id"]: EvaluationItem.from_dict(item) for item in state["items"]}
        results = [
            EvaluationResult.from_dict(result, item=items[result["item_id"]])
            for result in state["results"]
        ]
        try:
            experiment = evaluations.create_experiment(**state["experiment"])
            receipt = experiment.upload_results(results)
            completed = experiment.complete()
        except BulkUploadError as error:
            print(
                f"Accepted {len(error.completed.chunks)} chunks; stopped at result {error.failed_index} ({error.persistence})."
            )
            print(
                f"Prepared run retained in {args.state}. Inspect the error before replaying the same file."
            )
            raise
        except EvaluationAPIError:
            print(
                f"Prepared run retained in {args.state}; the SDK has not automatically closed the experiment."
            )
            raise
        print(f"Experiment {experiment.id}: {completed.status}")
        print(
            f"Acknowledged {sum(len(chunk.results) for chunk in receipt.chunks)} results across {len(receipt.chunks)} chunks."
        )
        print(
            f"Keep {args.state} for exact replay; choose a different --state path for a new experiment."
        )


if __name__ == "__main__":
    main()
