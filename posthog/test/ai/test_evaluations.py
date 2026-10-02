"""Offline evaluation contracts exercised through the public sync and async API."""

import asyncio
import importlib.util
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4, uuid5

import pytest

from posthog.ai.evaluations import (
    AsyncOfflineEvaluations,
    BulkUploadError,
    EvaluationAPIError,
    EvaluationItem,
    EvaluationResult,
    OfflineEvaluations,
)
from posthog.ai.evaluations import _client
from posthog.ai.evaluations._transport import AsyncTransport, SyncTransport


EXPERIMENT_ID = "8cba6d9e-d2ee-4b23-a545-a61057da5870"
VERSION_ID = "f72481ec-1310-41de-b5bb-d20e1c59f121"
OTHER_VERSION_ID = "f72481ec-1310-41de-b5bb-d20e1c59f122"
ACCEPTED_AT = "2026-10-02T12:00:00Z"
BASE = "/api/projects/123/ai_observability/offline_experiments/"


def experiment_response(id=EXPERIMENT_ID, **overrides):
    return {
        "id": id,
        "status": "uploading",
        "created": True,
        "started_at": ACCEPTED_AT,
        "created_at": ACCEPTED_AT,
        "finished_at": None,
        "expected_item_count": None,
        "expected_result_count": None,
        "accepted_item_count": 0,
        "accepted_result_count": 0,
        **overrides,
    }


def upload_response(body, *, created=True):
    data = json.loads(body)
    return {
        "items": [
            {"id": id, "created": created, "accepted_at": ACCEPTED_AT}
            for id in dict.fromkeys(result["item_id"] for result in data["results"])
        ],
        "results": [
            {
                "id": str(uuid5(UUID(result["item_id"]), result["scorer_version_id"])),
                "item_id": result["item_id"],
                "scorer_version_id": result["scorer_version_id"],
                "created": created,
                "accepted_at": ACCEPTED_AT,
            }
            for result in data["results"]
        ],
    }


def respond(method, path, *, body=None, **kwargs):
    assert method == "POST"
    if path.endswith("/upload/"):
        return upload_response(body)
    if path == BASE:
        data = json.loads(body)
        return experiment_response(
            id=data["id"],
            started_at=data["started_at"],
            expected_item_count=data.get("expected_item_count"),
            expected_result_count=data.get("expected_result_count"),
        )
    action = path.rstrip("/").rsplit("/", 1)[1]
    return experiment_response(
        id=path.split("/")[-3],
        created=False,
        status={"complete": "completed", "fail": "failed"}[action],
        finished_at=ACCEPTED_AT,
    )


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def client(request, monkeypatch):
    transport = Mock(spec=SyncTransport)
    if request.param:
        transport = AsyncMock(spec=AsyncTransport)
        monkeypatch.setattr(_client, "AsyncTransport", lambda *args: transport)
        client = AsyncOfflineEvaluations(project_id=123, secret_key="test-secret")
    else:
        monkeypatch.setattr(_client, "SyncTransport", lambda *args: transport)
        client = OfflineEvaluations(project_id=123, secret_key="test-secret")
    transport.request.side_effect = respond
    return client, transport


def invoke(method, *args, **kwargs):
    result = method(*args, **kwargs)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def result(item=None, **kwargs):
    return EvaluationResult(
        item=item or EvaluationItem(input="question", output="answer"),
        scorer_version_id=VERSION_ID,
        **({"value": 1} | kwargs),
    )


def test_create_upload_and_explicit_completion(client):
    evaluations, transport = client
    experiment = invoke(
        evaluations.create_experiment,
        name="Quality benchmark",
        run_source="ci",
        expected_item_count=1,
        expected_result_count=1,
        application_version="commit-123",
    )
    creation_body = json.loads(transport.request.call_args.kwargs["body"])
    assert str(UUID(experiment.id)) == creation_body["id"]
    assert datetime.fromisoformat(creation_body["started_at"]).tzinfo is not None
    assert creation_body["run_source"] == "ci"
    assert creation_body["application_version"] == "commit-123"
    assert experiment.receipt.expected_result_count == 1
    assert experiment.submission == creation_body
    changed_copy = experiment.submission
    changed_copy["name"] = "Changed locally"
    assert experiment.submission == creation_body

    item = EvaluationItem(input="Question", output="Answer")
    receipt = invoke(
        experiment.upload_result, item=item, scorer_version_id=VERSION_ID, value=False
    )
    assert receipt.items[0].id == item.id
    assert receipt.results[0].created is True
    assert receipt.results[0].accepted_at.tzinfo == timezone.utc
    assert receipt.results[0].scorer_version_id == VERSION_ID
    assert (
        json.loads(transport.request.call_args.kwargs["body"])["results"][0]["value"]
        is False
    )
    assert transport.request.call_args.args == (
        "POST",
        BASE + experiment.id + "/upload/",
    )
    completion = invoke(experiment.complete)
    assert completion.status == "completed"
    assert experiment.receipt == completion
    assert transport.request.call_args.kwargs["body"] == b"{}"


def test_resume_is_local_and_supports_references_without_scorer_discovery(client):
    evaluations, transport = client
    experiment = evaluations.resume_experiment(UUID(EXPERIMENT_ID))
    assert experiment.id == EXPERIMENT_ID
    assert experiment.submission is None
    assert experiment.receipt is None
    transport.request.assert_not_called()
    item_id = str(uuid4())
    receipt = invoke(
        experiment.upload_result,
        item=item_id,
        scorer_version_id=UUID(VERSION_ID),
        value=0,
    )
    body = json.loads(transport.request.call_args.kwargs["body"])
    assert body["items"] == []
    assert body["results"][0]["item_id"] == item_id
    assert body["results"][0]["value"] == 0
    assert receipt.items[0].id == item_id
    transport.request.assert_called_once()


def test_same_item_is_declared_once_for_multiple_scorers(client):
    evaluations, transport = client
    item = EvaluationItem(input={"question": "Paris?"}, output="France")
    results = [
        result(item),
        EvaluationResult(item=item, scorer_version_id=OTHER_VERSION_ID, value=True),
    ]
    receipt = invoke(
        evaluations.resume_experiment(EXPERIMENT_ID).upload_results, results
    )
    body = json.loads(transport.request.call_args.kwargs["body"])
    assert body["items"] == [item.to_dict()]
    assert len(receipt.chunks) == 1
    assert len(receipt.chunks[0].results) == 2
    assert len(receipt.chunks[0].items) == 1


def test_real_result_count_boundary_splits_without_dropping_declarations(client):
    evaluations, transport = client
    results = [result() for _ in range(1001)]
    receipt = invoke(
        evaluations.resume_experiment(EXPERIMENT_ID).upload_results, results
    )
    assert [len(chunk.results) for chunk in receipt.chunks] == [1000, 1]
    bodies = [
        json.loads(call.kwargs["body"]) for call in transport.request.call_args_list
    ]
    assert [len(body["items"]) for body in bodies] == [1000, 1]
    assert [row["item_id"] for body in bodies for row in body["results"]] == [
        row.item_id for row in results
    ]


def test_utf8_byte_boundary_splits_below_request_limit(client):
    evaluations, transport = client
    results = [result(EvaluationItem(input="🙂" * (190 * 1024))) for _ in range(7)]
    receipt = invoke(
        evaluations.resume_experiment(EXPERIMENT_ID).upload_results, results
    )
    assert len(receipt.chunks) == 2
    assert sum(len(chunk.results) for chunk in receipt.chunks) == 7
    assert all(
        len(call.kwargs["body"]) <= 5 * 1024 * 1024
        for call in transport.request.call_args_list
    )


def test_shared_item_is_redeclared_in_each_independently_replayable_chunk(
    client, monkeypatch
):
    evaluations, transport = client
    monkeypatch.setattr(_client, "_MAX_RESULTS", 1)
    item = EvaluationItem(input="Same execution")
    results = [
        EvaluationResult(item=item.id, scorer_version_id=VERSION_ID, value=1),
        EvaluationResult(item=item, scorer_version_id=OTHER_VERSION_ID, value=True),
    ]
    invoke(evaluations.resume_experiment(EXPERIMENT_ID).upload_results, results)
    assert len(transport.request.call_args_list) == 2
    for call in transport.request.call_args_list:
        assert json.loads(call.kwargs["body"])["items"] == [item.to_dict()]


@pytest.mark.parametrize("persistence", ["unknown", "rejected"])
def test_partial_bulk_error_retains_acknowledged_chunks_and_exact_pending_work(
    client, monkeypatch, persistence
):
    evaluations, transport = client
    monkeypatch.setattr(_client, "_MAX_RESULTS", 1)
    results = [result() for _ in range(3)]
    calls = 0

    def fail_second(method, path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise EvaluationAPIError(
                status=503 if persistence == "unknown" else 409, persistence=persistence
            )
        return respond(method, path, **kwargs)

    transport.request.side_effect = fail_second
    experiment = evaluations.resume_experiment(EXPERIMENT_ID)
    with pytest.raises(BulkUploadError) as caught:
        invoke(experiment.upload_results, results)
    error = caught.value
    assert len(error.completed.chunks) == 1
    assert error.failed_index == 1
    assert error.pending_results == tuple(results[1:])
    assert error.persistence == persistence
    failed_body = transport.request.call_args.kwargs["body"]
    assert error.failed_chunk == json.loads(failed_body)
    error.failed_chunk["results"].clear()
    assert error.failed_chunk["results"]
    assert transport.request.call_count == 2

    transport.request.side_effect = respond
    receipt = invoke(experiment.upload_results, error.pending_results)
    assert len(receipt.chunks) == 2
    assert transport.request.call_args_list[2].kwargs["body"] == failed_body
    assert not any(
        call.args[1].endswith(("/fail/", "/complete/"))
        for call in transport.request.call_args_list
    )


def test_creation_error_exposes_generated_identity_for_exact_retry(client):
    evaluations, transport = client
    transport.request.side_effect = EvaluationAPIError(persistence="unknown")
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(
            evaluations.create_experiment,
            name="Recoverable",
            run_source=None,
            expected_result_count=0,
        )
    original = transport.request.call_args.kwargs["body"]
    saved = caught.value.submission
    assert saved == json.loads(original)
    assert saved["run_source"] is None
    transport.request.side_effect = respond
    experiment = invoke(evaluations.create_experiment, **saved)
    assert experiment.id == saved["id"]
    assert transport.request.call_args.kwargs["body"] == original


def test_malformed_creation_receipt_is_uncertain_and_recoverable(client):
    evaluations, transport = client
    transport.request.side_effect = None
    transport.request.return_value = experiment_response(id=str(uuid4()))
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(evaluations.create_experiment, name="Run", id=EXPERIMENT_ID)
    assert caught.value.code == "invalid_response"
    assert caught.value.persistence == "unknown"
    assert caught.value.submission["id"] == EXPERIMENT_ID


@pytest.mark.parametrize(
    "fault",
    [
        "missing_items",
        "missing_results",
        "wrong_version",
        "wrong_result_id",
        "bad_created",
    ],
)
def test_malformed_upload_receipts_cannot_report_delivery(client, fault):
    evaluations, transport = client

    def malformed(method, path, *, body):
        response = upload_response(body)
        if fault == "missing_items":
            response["items"] = []
        elif fault == "missing_results":
            response["results"] = []
        elif fault == "wrong_version":
            response["results"][0]["scorer_version_id"] = OTHER_VERSION_ID
        elif fault == "wrong_result_id":
            response["results"][0]["id"] = "invalid"
        else:
            response["results"][0]["created"] = "true"
        return response

    transport.request.side_effect = malformed
    with pytest.raises(BulkUploadError) as caught:
        invoke(evaluations.resume_experiment(EXPERIMENT_ID).upload_results, [result()])
    assert caught.value.persistence == "unknown"
    assert caught.value.completed.chunks == ()
    assert caught.value.failed_index == 0


def test_duplicate_acknowledgments_and_server_count_conflicts_are_observable(client):
    evaluations, transport = client
    transport.request.side_effect = lambda method, path, *, body: upload_response(
        body, created=False
    )
    experiment = evaluations.resume_experiment(EXPERIMENT_ID)
    receipt = invoke(experiment.upload_results, [result()])
    assert receipt.chunks[0].items[0].created is False
    assert receipt.chunks[0].results[0].created is False

    error = EvaluationAPIError(
        status=409,
        code="expected_counts_mismatch",
        response={"accepted_result_count": 1, "expected_result_count": 2},
        persistence="rejected",
    )
    transport.request.side_effect = error
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(experiment.complete)
    assert caught.value is error
    assert caught.value.response["expected_result_count"] == 2
    assert experiment.receipt is None
    assert transport.request.call_count == 2


def test_fail_is_explicit_and_uses_the_same_idempotent_lifecycle_contract(client):
    evaluations, transport = client
    receipt = invoke(evaluations.resume_experiment(EXPERIMENT_ID).fail)
    assert receipt.status == "failed"
    assert transport.request.call_args.args == ("POST", BASE + EXPERIMENT_ID + "/fail/")
    assert transport.request.call_args.kwargs["body"] == b"{}"


def test_empty_bulk_and_local_validation_make_no_network_requests(client):
    evaluations, transport = client
    experiment = evaluations.resume_experiment(EXPERIMENT_ID)
    assert invoke(experiment.upload_results, []).chunks == ()
    original = result()
    with pytest.raises(ValueError, match="pair"):
        invoke(experiment.upload_results, [original, original])
    changed = EvaluationItem(id=original.item_id, input="Changed execution")
    conflict = EvaluationResult(
        item=changed, scorer_version_id=OTHER_VERSION_ID, value=True
    )
    with pytest.raises(ValueError, match="Conflicting"):
        invoke(experiment.upload_results, [original, conflict])
    transport.request.assert_not_called()


def test_items_and_results_survive_json_restoration_without_mutable_aliases():
    content = {"messages": [{"text": "Original"}]}
    item = EvaluationItem(input=content, output=None, metadata={"dataset": "v1"})
    categories = ["safe", "helpful"]
    original = EvaluationResult(
        item=item,
        scorer_version_id=VERSION_ID,
        value=categories,
        reasoning="Two categories",
    )
    saved = json.loads(
        json.dumps({"item": item.to_dict(), "result": original.to_dict()})
    )
    content["messages"][0]["text"] = "Changed"
    categories.append("other")
    restored_item = EvaluationItem.from_dict(saved["item"])
    restored = EvaluationResult.from_dict(saved["result"], item=restored_item)
    assert restored_item.to_dict() == item.to_dict()
    assert restored.to_dict() == original.to_dict()
    assert restored.item_id == item.id
    assert restored.item is restored_item
    item.to_dict()["payload"]["input"]["messages"].clear()
    assert item.to_dict() == saved["item"]
    with pytest.raises(ValueError, match="original id"):
        EvaluationItem.from_dict({"payload": {}})
    with pytest.raises(ValueError, match="do not match"):
        EvaluationResult.from_dict(saved["result"], item=EvaluationItem())


def test_missing_empty_and_null_payloads_remain_distinct():
    omitted = EvaluationItem()
    empty = EvaluationItem(payload={})
    null_fields = EvaluationItem(input=None, metadata=None)
    assert "payload" not in omitted.to_dict()
    assert empty.to_dict()["payload"] == {}
    assert null_fields.to_dict()["payload"] == {"input": None, "metadata": None}
    assert "payload" not in result().to_dict()
    assert result(payload={}).to_dict()["payload"] == {}
    assert result(reasoning=None).to_dict()["payload"] == {"reasoning": None}
    with pytest.raises(ValueError, match="not both"):
        EvaluationItem(input=None, payload={})


@pytest.mark.parametrize("value", [False, True, 0, -1.25, ["safe", "helpful"]])
def test_accepted_scores_preserve_type_and_polarity(value):
    actual = result(value=value).to_dict()["value"]
    if isinstance(value, bool):
        assert actual is value
    else:
        assert actual == (sorted(value) if isinstance(value, list) else value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "pass",
        [],
        ["safe", "safe"],
        [""],
        ["a\x00b"],
        float("nan"),
        float("inf"),
        10**400,
    ],
)
def test_invalid_scores_fail_before_upload(value):
    with pytest.raises(ValueError):
        result(value=value)


@pytest.mark.parametrize("status", ["error", "skipped", "not_applicable"])
def test_non_ok_outcomes_have_no_score(status):
    kwargs = {"status": status}
    if status == "error":
        kwargs |= {"error_code": "timeout", "error_message": "Judge timed out"}
    outcome = EvaluationResult(
        item=EvaluationItem(), scorer_version_id=VERSION_ID, **kwargs
    )
    assert "value" not in outcome.to_dict()
    with pytest.raises(ValueError, match="Only an ok"):
        result(status=status, value=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"error_code": "timeout"},
        {"error_message": "Timed out"},
        {"payload": {"error_message": "Timed out"}},
    ],
)
def test_error_fields_are_restricted_to_error_status(kwargs):
    with pytest.raises(ValueError, match="Only an error"):
        result(**kwargs)


def test_payload_byte_and_json_shape_validation_matches_backend_limits():
    with pytest.raises(ValueError, match="bytes"):
        EvaluationItem(input="é" * (512 * 1024))
    with pytest.raises(ValueError, match="bytes"):
        result(reasoning="x" * (256 * 1024))
    nested = {}
    for _ in range(33):
        nested = {"child": nested}
    with pytest.raises(ValueError, match="nesting"):
        EvaluationItem(input=nested)
    for value in (
        {1: "not string"},
        {"a": object()},
        {"a": float("nan")},
        {"a": "\ud800"},
    ):
        with pytest.raises(ValueError):
            EvaluationItem(input=value)


def test_client_contexts_close_connections_without_closing_experiments(monkeypatch):
    transport = Mock(spec=SyncTransport)
    monkeypatch.setattr(_client, "SyncTransport", lambda *args: transport)
    with OfflineEvaluations(project_id=123, secret_key="test") as evaluations:
        evaluations.resume_experiment(EXPERIMENT_ID)
    transport.close.assert_called_once()
    transport.request.assert_not_called()

    async def run():
        transport = AsyncMock(spec=AsyncTransport)
        monkeypatch.setattr(_client, "AsyncTransport", lambda *args: transport)
        async with AsyncOfflineEvaluations(
            project_id=123, secret_key="test"
        ) as evaluations:
            evaluations.resume_experiment(EXPERIMENT_ID)
        transport.aclose.assert_awaited_once()
        transport.request.assert_not_called()

    asyncio.run(run())


def test_async_cancellation_never_completes_or_fails_experiment(monkeypatch):
    async def run():
        transport = AsyncMock(spec=AsyncTransport)
        transport.request.side_effect = asyncio.CancelledError
        monkeypatch.setattr(_client, "AsyncTransport", lambda *args: transport)
        async with AsyncOfflineEvaluations(
            project_id=123, secret_key="test"
        ) as evaluations:
            experiment = evaluations.resume_experiment(EXPERIMENT_ID)
            with pytest.raises(asyncio.CancelledError):
                await experiment.upload_results([result()])
            assert experiment.receipt is None
        transport.request.assert_awaited_once()
        assert transport.request.call_args.args[1].endswith("/upload/")
        transport.aclose.assert_awaited_once()

    asyncio.run(run())


def test_example_persists_before_writes_and_replays_the_same_run(monkeypatch, tmp_path):
    example = Path(__file__).parents[3] / "examples/example-offline-evaluations/main.py"
    spec = importlib.util.spec_from_file_location("offline_evaluation_example", example)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state_path = tmp_path / "prepared-run.json"
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "123")
    monkeypatch.setenv("POSTHOG_SECRET_KEY", "never-persist-this-secret")
    monkeypatch.setenv("POSTHOG_HOST", "https://us.posthog.com")
    monkeypatch.setenv("POSTHOG_SCORER_VERSION_ID", VERSION_ID)
    monkeypatch.setattr("sys.argv", [str(example), "--state", str(state_path)])
    transport = Mock(spec=SyncTransport)

    def check_state_before_write(method, path, **kwargs):
        assert state_path.exists()
        assert "never-persist-this-secret" not in state_path.read_text()
        return respond(method, path, **kwargs)

    transport.request.side_effect = check_state_before_write
    monkeypatch.setattr(_client, "SyncTransport", lambda *args: transport)
    module.main()
    prepared_state = state_path.read_bytes()
    module.main()
    assert state_path.read_bytes() == prepared_state
    requests = transport.request.call_args_list
    assert len(requests) == 6
    assert requests[:3] == requests[3:]
    assert transport.close.call_count == 2
