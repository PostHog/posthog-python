import asyncio
import copy
import importlib.util
import inspect
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import httpx
import pytest
import requests

from posthog.ai.evaluations import (
    AsyncOfflineEvaluations,
    DatasetItemPage,
    EvaluationAPIError,
    EvaluationItem,
    EvaluationResult,
    OfflineEvaluations,
)
from posthog.ai.evaluations import _client
from posthog.ai.evaluations._transport import AsyncTransport, SyncTransport

from .test_evaluations import respond as respond_to_experiment
from .test_evaluations_scorers import scorer_response

DATASET_ID = "015e4ac4-3659-456b-998c-4345b4e4c846"
ITEM_ID = "7d1e9d28-9b64-4c95-b4ad-3e6fa6b975b1"
OTHER_ITEM_ID = "b492bf6b-bb45-4374-aefb-e40a8eecfc62"
VERSION_ID = "9e2bdd1a-ad9e-4a96-9bc0-77a7f4d8b35e"
REVISION_ID = "ba71a1bb-836f-4c19-9d06-1d36d44948a4"
OLD_REVISION_ID = "a4137aa9-4e27-4829-928b-568cc55a0082"
TIMESTAMP = "2026-10-20T10:00:00Z"
BASE = "/api/projects/123/datasets/"
ITEMS = "/api/projects/123/dataset_items/"


def dataset_response(**overrides):
    return {
        "id": DATASET_ID,
        "name": "Capital cities",
        "description": "Regression cases",
        "metadata": {"owner": "support"},
        "tags": ["regression"],
        "archived": False,
        "current_revision": 3,
        "current_revision_id": REVISION_ID,
        "created_at": TIMESTAMP,
        "updated_at": None,
        "created_by": None,
        "team_id": 123,
        **overrides,
    }


def revision_response(**overrides):
    return {
        "id": OLD_REVISION_ID,
        "dataset_id": DATASET_ID,
        "revision": 1,
        "created_at": TIMESTAMP,
        "created_by": None,
        "team_id": 123,
        **overrides,
    }


def item_response(**overrides):
    return {
        "id": ITEM_ID,
        "dataset": DATASET_ID,
        "client_item_id": "capital-france",
        "version": 2,
        "version_id": VERSION_ID,
        "dataset_revision": 2,
        "dataset_revision_id": OLD_REVISION_ID,
        "archived": False,
        "input": {"question": "Capital of France?"},
        "expected_output": "Paris",
        "source_output": "Lyon",
        "metadata": {"category": ["geography"]},
        "source_trace_id": "source-trace",
        "source_event_id": "source-event",
        "source_timestamp": TIMESTAMP,
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
        "created_by": None,
        "version_created_at": TIMESTAMP,
        "version_created_by": None,
        "team_id": 123,
        **overrides,
    }


def page_response(*rows, next_offset=None, count=None):
    return {
        "count": len(rows) if count is None else count,
        "results": list(rows),
        "next": None
        if next_offset is None
        else f"https://us.posthog.com{ITEMS}?offset={next_offset}",
        "previous": None,
    }


def invoke(method, *args, **kwargs):
    result = method(*args, **kwargs)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def collect(iterator):
    async def consume():
        return [item async for item in iterator]

    return asyncio.run(consume()) if hasattr(iterator, "__aiter__") else list(iterator)


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def client(request, monkeypatch):
    transport = Mock(spec=SyncTransport)
    if request.param:
        transport = AsyncMock(spec=AsyncTransport)
        monkeypatch.setattr(_client, "AsyncTransport", lambda *args: transport)
        evaluations = AsyncOfflineEvaluations(project_id=123, secret_key="phx_example")
    else:
        monkeypatch.setattr(_client, "SyncTransport", lambda *args: transport)
        evaluations = OfflineEvaluations(project_id=123, secret_key="phx_example")
    return evaluations, transport


def test_dataset_metadata_omission_clearing_and_empty_revision(client):
    evaluations, transport = client
    response = dataset_response(current_revision=None, current_revision_id=None)
    transport.request.return_value = response
    dataset = invoke(evaluations.datasets.create, name="Capital cities")
    assert dataset.current_revision is None
    assert dataset.current_revision_id is None
    assert dataset.created_at.tzinfo == timezone.utc
    assert json.loads(transport.request.call_args.kwargs["body"]) == {
        "name": "Capital cities"
    }
    assert transport.request.call_args.kwargs["retry_safe"] is False

    response["metadata"]["owner"] = "changed"
    assert dataset.metadata == {"owner": "support"}
    with pytest.raises(FrozenInstanceError):
        dataset.id = OTHER_ITEM_ID

    invoke(
        evaluations.datasets.update,
        UUID(DATASET_ID),
        description="",
        metadata={},
        tags=[],
    )
    assert json.loads(transport.request.call_args.kwargs["body"]) == {
        "description": "",
        "metadata": {},
        "tags": [],
    }
    invoke(evaluations.datasets.update, DATASET_ID, name="Renamed")
    assert json.loads(transport.request.call_args.kwargs["body"]) == {"name": "Renamed"}
    assert transport.request.call_args.kwargs["retry_safe"] is False


@pytest.mark.parametrize("input", [False, 0, "", [], {"messages": []}])
def test_item_creation_preserves_json_values_and_source_provenance(client, input):
    evaluations, transport = client
    transport.request.return_value = item_response(input=input)
    item = invoke(
        evaluations.datasets.create_item,
        DATASET_ID,
        input=input,
        expected_output=None,
        source_output="original",
        metadata={},
        client_item_id="capital-france",
        source_trace_id="trace",
        source_event_id="event",
        source_timestamp=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )
    body = json.loads(transport.request.call_args.kwargs["body"])
    assert item.input == input
    assert body == {
        "dataset": DATASET_ID,
        "input": input,
        "expected_output": None,
        "source_output": "original",
        "metadata": {},
        "client_item_id": "capital-france",
        "source_trace_id": "trace",
        "source_event_id": "event",
        "source_timestamp": "2026-10-20T00:00:00+00:00",
    }
    assert transport.request.call_args.kwargs["retry_safe"] is True


def test_item_updates_distinguish_omission_from_clear_and_protect_source_fields(client):
    evaluations, transport = client
    transport.request.return_value = item_response()
    invoke(
        evaluations.datasets.update_item,
        ITEM_ID,
        base_version=2,
        expected_output=None,
        metadata={},
    )
    assert json.loads(transport.request.call_args.kwargs["body"]) == {
        "base_version": 2,
        "expected_output": None,
        "metadata": {},
    }
    assert transport.request.call_args.kwargs["retry_safe"] is False
    invoke(evaluations.datasets.update_item, ITEM_ID, base_version=2, input="new")
    assert json.loads(transport.request.call_args.kwargs["body"]) == {
        "base_version": 2,
        "input": "new",
    }
    transport.request.reset_mock()
    with pytest.raises(TypeError):
        invoke(
            evaluations.datasets.update_item,
            ITEM_ID,
            base_version=2,
            source_output="overwrite",
        )
    transport.request.assert_not_called()


@pytest.mark.parametrize("scope", ["dataset", "item"])
def test_archive_and_restore_use_explicit_transitions(client, scope):
    evaluations, transport = client
    namespace = evaluations.datasets
    if scope == "dataset":
        transport.request.return_value = dataset_response(archived=True)
        archived = invoke(namespace.archive, DATASET_ID)
        assert transport.request.call_args.args == (
            "POST",
            BASE + DATASET_ID + "/archive/",
        )
        transport.request.return_value = dataset_response()
        restored = invoke(namespace.restore, DATASET_ID)
        assert transport.request.call_args.args == (
            "POST",
            BASE + DATASET_ID + "/restore/",
        )
    else:
        transport.request.return_value = item_response(archived=True, version=3)
        archived = invoke(namespace.archive_item, ITEM_ID, base_version=2)
        assert json.loads(transport.request.call_args.kwargs["body"]) == {
            "base_version": 2
        }
        transport.request.return_value = item_response(version=4)
        restored = invoke(
            namespace.restore_item, ITEM_ID, base_version=3, source_version=1
        )
        assert json.loads(transport.request.call_args.kwargs["body"]) == {
            "base_version": 3,
            "source_version": 1,
        }
        assert transport.request.call_args.args == (
            "POST",
            ITEMS + ITEM_ID + "/restore/",
        )
    assert archived.archived is True
    assert restored.archived is False
    assert all(
        call.kwargs["retry_safe"] is False for call in transport.request.call_args_list
    )


@pytest.mark.parametrize("tags", [None, [], [" Quality "]])
@pytest.mark.parametrize("tag_match", ["any", "all"])
def test_filtered_dataset_pages_keep_continuation_and_normalized_tags(
    client, tags, tag_match
):
    evaluations, transport = client
    transport.request.return_value = page_response(
        dataset_response(tags=["quality"]), next_offset=50, count=51
    )
    page = invoke(
        evaluations.datasets.list,
        search="capital",
        archived=True,
        order_by="updated_at",
        tags=tags,
        tag_match=tag_match,
    )
    assert page.next_offset == 50
    assert page.count == 51
    assert page.results[0].tags == ("quality",)
    assert transport.request.call_args.kwargs["params"] == {
        "limit": 50,
        "offset": 0,
        "search": "capital",
        "archived": "true",
        "order_by": "updated_at",
        **({"tags": json.dumps(tags)} if tags is not None else {}),
        **({"tag_match": tag_match} if tags is not None or tag_match != "any" else {}),
    }


@pytest.mark.parametrize(
    "field", ["input", "expected_output", "source_output", "metadata"]
)
def test_item_json_preserves_response_key_order_without_aliasing(client, field):
    evaluations, transport = client
    value = {"z": [{"y": 1, "b": 2}], "a": 3}
    transport.request.return_value = item_response(**{field: value})

    item = invoke(evaluations.datasets.get_item, ITEM_ID)
    copied = getattr(item, field)
    assert list(copied) == ["z", "a"]
    assert list(copied["z"][0]) == ["y", "b"]
    value["z"][0]["y"] = 99
    assert copied["z"][0]["y"] == 1


def test_item_history_and_historical_get_keep_stable_and_version_ids_separate(client):
    evaluations, transport = client
    transport.request.return_value = page_response(
        item_response(), item_response(version=1)
    )
    page = invoke(evaluations.datasets.list_item_versions, UUID(ITEM_ID), limit=100)
    assert isinstance(page, DatasetItemPage)
    assert [row.version for row in page.results] == [2, 1]
    assert transport.request.call_args.args == ("GET", ITEMS + ITEM_ID + "/versions/")
    transport.request.return_value = item_response()
    item = invoke(evaluations.datasets.get_item, UUID(ITEM_ID), revision=3)
    assert item.id == ITEM_ID and item.version_id == VERSION_ID
    assert item.dataset_revision == 2
    assert transport.request.call_args.kwargs["params"] == {"revision": 3}


def test_snapshot_pins_before_iteration_and_never_follows_remote_pagination_urls(
    client,
):
    evaluations, transport = client
    latest = dataset_response()
    transport.request.return_value = latest
    snapshot = invoke(evaluations.datasets.snapshot, DATASET_ID)
    assert transport.request.call_count == 1
    assert snapshot.revision == 3 and snapshot.revision_id == REVISION_ID
    latest.update(current_revision=4, current_revision_id=OLD_REVISION_ID)
    iterator = snapshot.iter_items(page_size=1)
    assert transport.request.call_count == 1
    first = page_response(item_response(), next_offset=1, count=2)
    first["next"] = "https://untrusted.example/api/?offset=1&revision=4"
    transport.request.side_effect = [
        first,
        page_response(item_response(id=OTHER_ITEM_ID), count=2),
    ]
    items = collect(iterator)
    assert [item.id for item in items] == [ITEM_ID, OTHER_ITEM_ID]
    for call in transport.request.call_args_list[1:]:
        assert call.args == ("GET", ITEMS)
        assert call.kwargs["params"]["revision"] == 3
        assert call.kwargs["params"]["archived"] == "false"
    assert transport.request.call_args.kwargs["params"]["offset"] == 1


def test_historical_snapshot_looks_up_expected_offset_without_scanning(client):
    evaluations, transport = client
    transport.request.side_effect = [
        dataset_response(current_revision=10_000),
        page_response(revision_response(), count=10_000),
    ]

    snapshot = invoke(evaluations.datasets.snapshot, DATASET_ID, revision=1)

    assert snapshot.revision == 1 and snapshot.revision_id == OLD_REVISION_ID
    assert transport.request.call_count == 2
    assert transport.request.call_args.args == (
        "GET",
        BASE + DATASET_ID + "/revisions/",
    )
    assert transport.request.call_args.kwargs["params"] == {"limit": 1, "offset": 9999}


def test_historical_snapshot_falls_back_when_concurrent_writes_shift_offset(client):
    evaluations, transport = client
    transport.request.side_effect = [
        dataset_response(),
        page_response(revision_response(revision=2), next_offset=3, count=4),
        page_response(
            revision_response(revision=4),
            revision_response(revision=3),
            revision_response(revision=2),
            revision_response(),
        ),
    ]

    snapshot = invoke(evaluations.datasets.snapshot, DATASET_ID, revision=1)

    assert snapshot.revision == 1 and snapshot.revision_id == OLD_REVISION_ID
    assert [call.kwargs["params"] for call in transport.request.call_args_list[1:]] == [
        {"limit": 1, "offset": 2},
        {"limit": 100, "offset": 0},
    ]


def test_historical_snapshot_resolves_revision_uuid_across_pages(client):
    evaluations, transport = client
    transport.request.side_effect = [
        dataset_response(),
        page_response(count=2),
        page_response(
            revision_response(id=REVISION_ID, revision=3), next_offset=1, count=2
        ),
        page_response(revision_response(), count=2),
        page_response(item_response(dataset_revision=1)),
    ]
    snapshot = invoke(evaluations.datasets.snapshot, DATASET_ID, revision=1)
    assert snapshot.revision_id == OLD_REVISION_ID
    assert transport.request.call_args.kwargs["params"]["offset"] == 1
    assert snapshot.dataset.current_revision == 3
    assert [
        call.kwargs["params"]["offset"] for call in transport.request.call_args_list[1:]
    ] == [2, 0, 1]
    assert len(collect(snapshot.iter_items())) == 1
    assert transport.request.call_args.kwargs["params"]["revision"] == 1


def test_never_populated_and_missing_revisions_are_distinct_from_empty_snapshot(client):
    evaluations, transport = client
    transport.request.return_value = dataset_response(
        current_revision=None, current_revision_id=None
    )
    with pytest.raises(ValueError, match="no revision"):
        invoke(evaluations.datasets.snapshot, DATASET_ID)
    transport.request.side_effect = [dataset_response(), page_response()]
    with pytest.raises(ValueError, match="does not exist"):
        invoke(evaluations.datasets.snapshot, DATASET_ID, revision=9)
    assert transport.request.call_args.args == ("GET", BASE + DATASET_ID + "/")
    transport.request.side_effect = [dataset_response(archived=True), page_response()]
    snapshot = invoke(evaluations.datasets.snapshot, DATASET_ID)
    assert collect(snapshot.iter_items()) == []


def test_missing_historical_revision_stops_once_history_passes_it(client):
    evaluations, transport = client
    transport.request.side_effect = [
        dataset_response(),
        page_response(revision_response(), count=2),
        page_response(revision_response(), next_offset=1, count=2),
    ]
    with pytest.raises(ValueError, match="revision 2 does not exist"):
        invoke(evaluations.datasets.snapshot, DATASET_ID, revision=2)
    assert transport.request.call_count == 3


def test_snapshot_propagates_partial_read_failure_and_can_be_iterated_again(client):
    evaluations, transport = client
    transport.request.return_value = dataset_response()
    snapshot = invoke(evaluations.datasets.snapshot, DATASET_ID)
    failure = EvaluationAPIError(status=503, persistence="unknown")
    transport.request.side_effect = [
        page_response(item_response(), next_offset=1, count=2),
        failure,
    ]
    with pytest.raises(EvaluationAPIError) as caught:
        collect(snapshot.iter_items(page_size=1))
    assert caught.value is failure
    transport.request.side_effect = [page_response(item_response())]
    assert len(collect(snapshot.iter_items())) == 1
    assert transport.request.call_args.kwargs["params"]["offset"] == 0


@pytest.mark.parametrize(
    "next_url,rows",
    [
        ("?offset=0", [item_response()]),
        ("?offset=-1", [item_response()]),
        ("?offset=one", [item_response()]),
        ("?offset=1&offset=2", [item_response()]),
        ("?cursor=next", [item_response()]),
        ("?offset=1", []),
    ],
)
def test_invalid_continuations_cannot_loop_or_silently_truncate(client, next_url, rows):
    evaluations, transport = client
    transport.request.return_value = {"count": 2, "results": rows, "next": next_url}
    with pytest.raises(EvaluationAPIError, match="no acknowledgment") as caught:
        invoke(evaluations.datasets.list_items, DATASET_ID)
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    "overrides",
    [
        {"dataset": OTHER_ITEM_ID},
        {"team_id": 999},
        {"dataset_revision": 4},
        {"archived": True},
        {"version_id": "invalid"},
        {"metadata": []},
        {"input": None},
        {"version": True},
        {"created_at": "2026-10-20"},
    ],
)
def test_invalid_item_responses_cannot_enter_a_pinned_run(client, overrides):
    evaluations, transport = client
    transport.request.return_value = page_response(item_response(**overrides))
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(evaluations.datasets.list_items, DATASET_ID, revision=3)
    assert caught.value.code == "invalid_response"
    assert caught.value.persistence == "unknown"
    assert caught.value.detail == "The dataset API returned an invalid response."
    assert caught.value.response == transport.request.return_value


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": OTHER_ITEM_ID},
        {"team_id": 999},
        {"current_revision": None},
        {"current_revision_id": None},
        {"current_revision": True},
    ],
)
def test_invalid_dataset_metadata_cannot_pin_a_snapshot(client, overrides):
    evaluations, transport = client
    transport.request.return_value = dataset_response(**overrides)
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(evaluations.datasets.snapshot, DATASET_ID)
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("create", (), {"name": " "}),
        ("update", (DATASET_ID,), {"description": None}),
        ("update", (DATASET_ID,), {"metadata": None}),
        ("update", (DATASET_ID,), {"tags": "tag"}),
        ("get", ("../other-project",), {}),
        ("list", (), {"limit": 101}),
        ("list", (), {"offset": -1}),
        ("list", (), {"archived": "false"}),
        ("list", (), {"tag_match": "invalid"}),
        ("list_items", (DATASET_ID,), {"limit": 26}),
        ("list_items", (DATASET_ID,), {"revision": 0}),
        ("snapshot", (DATASET_ID,), {"revision": True}),
        ("create_item", (DATASET_ID,), {"input": None}),
        ("create_item", (DATASET_ID,), {"input": float("nan")}),
        ("create_item", (DATASET_ID,), {"input": {1: "invalid key"}}),
        ("create_item", (DATASET_ID,), {"input": "q", "client_item_id": ""}),
        ("create_item", (DATASET_ID,), {"input": "q", "source_trace_id": "trace"}),
        ("create_item", (DATASET_ID,), {"input": "q", "source_event_id": "event"}),
        ("create_item", (DATASET_ID,), {"input": "q", "source_timestamp": TIMESTAMP}),
        (
            "create_item",
            (DATASET_ID,),
            {
                "input": "q",
                "source_trace_id": "trace",
                "source_timestamp": "2026-10-20",
            },
        ),
        ("update_item", (ITEM_ID,), {"base_version": 0}),
        ("update_item", (ITEM_ID,), {"base_version": 2, "input": None}),
        ("restore_item", (ITEM_ID,), {"base_version": 2, "source_version": False}),
    ],
)
def test_invalid_requests_fail_before_network_io(client, method, args, kwargs):
    evaluations, transport = client
    with pytest.raises(ValueError):
        invoke(getattr(evaluations.datasets, method), *args, **kwargs)
    transport.request.assert_not_called()


def test_conflicts_and_scope_errors_preserve_actionable_server_details(client):
    evaluations, transport = client
    conflict = EvaluationAPIError(
        status=409,
        code="stale_version",
        response={"current_version": 3, "current_item_id": ITEM_ID},
        persistence="rejected",
    )
    transport.request.side_effect = conflict
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(
            evaluations.datasets.update_item,
            ITEM_ID,
            base_version=2,
            expected_output="new",
        )
    assert caught.value is conflict
    assert caught.value.response["current_version"] == 3
    transport.request.assert_called_once()
    transport.request.side_effect = EvaluationAPIError(
        status=403, persistence="rejected"
    )
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(evaluations.datasets.get, DATASET_ID)
    assert caught.value.status == 403


def test_dataset_to_execution_copies_payload_and_keeps_trials_independent(client):
    evaluations, transport = client
    response = item_response()
    transport.request.return_value = response
    case = invoke(evaluations.datasets.get_item, ITEM_ID)
    original = copy.deepcopy(response)
    response["metadata"]["category"].append("changed")
    output = {"answer": "Paris"}
    first = EvaluationItem.from_dataset_item(
        case, output=output, trial="1", application_trace_id="new-trace"
    )
    second = EvaluationItem.from_dataset_item(case, output="Paris", trial="2")
    assert first.id != second.id and first.id != case.id
    data = first.to_dict()
    assert data["dataset_item_version_id"] == VERSION_ID
    assert data["dataset_item_identifier"] == ITEM_ID
    assert data["dataset_item_version_identifier"] == VERSION_ID
    assert data["case_key"] == "capital-france"
    assert data["application_trace_id"] == "new-trace"
    assert data["payload"] == {
        "input": original["input"],
        "output": output,
        "expected_output": "Paris",
        "metadata": original["metadata"],
    }
    output["answer"] = "changed"
    case.metadata["category"].append("changed locally")
    assert first.to_dict() == data
    assert "application_trace_id" not in second.to_dict()
    assert EvaluationItem.from_dict(data).to_dict() == data
    assert (
        EvaluationItem.from_dataset_item(replace(case, client_item_id=None)).to_dict()[
            "case_key"
        ]
        == ITEM_ID
    )
    assert "output" not in EvaluationItem.from_dataset_item(case).to_dict()["payload"]
    explicit = EvaluationItem.from_dataset_item(
        case, id=first.id, case_key="override", output=None
    )
    assert explicit.id == first.id
    assert explicit.to_dict()["case_key"] == "override"
    assert explicit.to_dict()["payload"]["output"] is None
    with pytest.raises(ValueError, match="Archived"):
        EvaluationItem.from_dataset_item(replace(case, archived=True))


def test_dataset_executions_keep_explicit_models_and_never_infer_them(client):
    evaluations, transport = client
    transport.request.return_value = item_response(
        metadata={"model_version": "source-model"}
    )
    case = invoke(evaluations.datasets.get_item, ITEM_ID)
    transport.request.reset_mock()
    items = [
        EvaluationItem.from_dataset_item(case, output="Paris", model_version="model-b"),
        EvaluationItem.from_dataset_item(case, output="Lyon", model_version="model-c"),
        EvaluationItem.from_dataset_item(case),
        EvaluationItem.from_dataset_item(case, model_version=None),
    ]
    assert len({item.id for item in items}) == 4
    for item in items:
        data = item.to_dict()
        assert data["case_key"] == "capital-france"
        assert data["dataset_item_version_id"] == VERSION_ID
        assert data["payload"]["metadata"] == {"model_version": "source-model"}
        assert EvaluationItem.from_dict(data).to_dict() == data
    for invalid in ["", 1, True, "x" * 256]:
        with pytest.raises(ValueError, match="model_version"):
            EvaluationItem.from_dataset_item(case, model_version=invalid)
    transport.request.assert_not_called()
    transport.request.side_effect = respond_to_experiment
    experiment = invoke(
        evaluations.create_experiment, name="Models", model_version="model-a"
    )
    invoke(
        experiment.upload_results,
        [
            EvaluationResult(item=item, scorer_version_id=OTHER_ITEM_ID, value=True)
            for item in items
        ],
    )
    body = json.loads(transport.request.call_args.kwargs["body"])
    assert body["items"] == [item.to_dict() for item in items]
    assert [item.get("model_version") for item in body["items"]] == [
        "model-b",
        "model-c",
        None,
        None,
    ]
    assert [item["payload"].get("output") for item in body["items"]] == [
        "Paris",
        "Lyon",
        None,
        None,
    ]


def test_dataset_scorer_experiment_flow_pins_both_versions_and_reuses_execution(client):
    evaluations, transport = client
    bodies = []

    def respond(method, path, *, body=None, **kwargs):
        if path == BASE + DATASET_ID + "/":
            return dataset_response()
        if path == ITEMS:
            return page_response(item_response())
        if "score_definitions" in path:
            return scorer_response()
        if path.endswith("/upload/"):
            bodies.append(json.loads(body))
        return respond_to_experiment(method, path, body=body, **kwargs)

    transport.request.side_effect = respond
    snapshot = invoke(evaluations.datasets.snapshot, DATASET_ID)
    scorer = invoke(evaluations.scorers.get, scorer_response()["id"])
    experiment = invoke(
        evaluations.create_experiment,
        name="Capital cities",
        dataset_revision_id=snapshot.revision_id,
    )
    case = collect(snapshot.iter_items())[0]
    item = EvaluationItem.from_dataset_item(case, output="Paris")
    results = [
        EvaluationResult(item=item, scorer_version_id=version, value=True)
        for version in [scorer.current_version_id, OTHER_ITEM_ID]
    ]
    invoke(experiment.upload_results, results)
    invoke(experiment.complete)
    assert experiment.submission["dataset_revision_id"] == REVISION_ID
    assert experiment.submission["dataset_revision_id"] != case.dataset_revision_id
    assert len(bodies[0]["items"]) == 1
    assert {result["item_id"] for result in bodies[0]["results"]} == {item.id}
    assert bodies[0]["items"][0]["dataset_item_version_id"] == VERSION_ID


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("client_item_id", [None, "stable-key"])
def test_lost_item_create_acknowledgment_only_retries_with_explicit_identity(
    asynchronous, client_item_id
):
    attempts = []

    def payload(attempt):
        attempts.append(attempt)
        if len(attempts) == 1:
            raise (
                requests.ReadTimeout()
                if not asynchronous
                else httpx.ReadTimeout("lost acknowledgment")
            )
        return item_response(client_item_id=client_item_id)

    async def run_async():
        async with AsyncOfflineEvaluations(
            project_id=123, secret_key="phx_example"
        ) as evaluations:

            def respond(request):
                return httpx.Response(200, json=payload((request.url, request.content)))

            await evaluations._transport._client.aclose()
            evaluations._transport._client = httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            )
            with patch(
                "posthog.ai.evaluations._transport.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                return await evaluations.datasets.create_item(
                    DATASET_ID, input="q", client_item_id=client_item_id
                )

    def run_sync():
        with OfflineEvaluations(
            project_id=123, secret_key="phx_example"
        ) as evaluations:

            def respond(method, url, **kwargs):
                data = payload((url, kwargs["data"]))
                result = requests.Response()
                result.status_code = 200
                result._content = json.dumps(data).encode()
                return result

            with (
                patch.object(
                    evaluations._transport._session, "request", side_effect=respond
                ),
                patch("posthog.ai.evaluations._transport.time.sleep"),
            ):
                return evaluations.datasets.create_item(
                    DATASET_ID, input="q", client_item_id=client_item_id
                )

    def run():
        return asyncio.run(run_async()) if asynchronous else run_sync()

    if client_item_id is None:
        with pytest.raises(EvaluationAPIError) as caught:
            run()
        assert caught.value.persistence == "unknown"
        assert len(attempts) == 1
    else:
        assert run().id == ITEM_ID
        assert len(attempts) == 2
        assert attempts[0] == attempts[1]


def test_dataset_example_replays_persisted_cases_without_refetching_or_rerunning(
    monkeypatch, tmp_path
):
    example = Path(__file__).parents[3] / "examples/example-offline-evaluations/main.py"
    spec = importlib.util.spec_from_file_location("dataset_evaluation_example", example)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state_path = tmp_path / "dataset-run.json"
    monkeypatch.setenv("POSTHOG_PROJECT_ID", "123")
    monkeypatch.setenv("POSTHOG_SECRET_KEY", "never-save-this-secret")
    monkeypatch.setenv("POSTHOG_HOST", "https://us.posthog.com")
    monkeypatch.setenv("POSTHOG_SCORER_VERSION_ID", VERSION_ID)
    monkeypatch.setattr(
        "sys.argv",
        [str(example), "--dataset-id", DATASET_ID, "--state", str(state_path)],
    )
    transport = Mock(spec=SyncTransport)
    monkeypatch.setattr(_client, "SyncTransport", lambda *args: transport)

    def respond(method, path, **kwargs):
        if method == "GET":
            assert not state_path.exists()
            if path == BASE + DATASET_ID + "/":
                return dataset_response()
            return page_response(item_response(input="Capital of France?"))
        assert state_path.exists()
        assert "never-save-this-secret" not in state_path.read_text()
        return respond_to_experiment(method, path, **kwargs)

    transport.request.side_effect = respond
    module.main()
    state = json.loads(state_path.read_text())
    assert state["experiment"]["dataset_revision_id"] == REVISION_ID
    assert state["items"][0]["dataset_item_version_id"] == VERSION_ID
    assert state["results"][0]["value"] is True
    first_writes = [
        call for call in transport.request.call_args_list if call.args[0] == "POST"
    ]
    transport.request.reset_mock()
    monkeypatch.setattr(
        module,
        "capital_answer",
        Mock(side_effect=AssertionError("Execution must not replay")),
    )
    module.main()
    assert transport.request.call_args_list == first_writes


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_snapshot_cannot_read_after_owning_client_closes(asynchronous):
    async def run_async():
        async with AsyncOfflineEvaluations(
            project_id=123, secret_key="phx_example"
        ) as evaluations:
            with patch.object(
                evaluations._transport,
                "request",
                new_callable=AsyncMock,
                return_value=dataset_response(),
            ):
                snapshot = await evaluations.datasets.snapshot(DATASET_ID)
        return [item async for item in snapshot.iter_items()]

    def run_sync():
        with OfflineEvaluations(
            project_id=123, secret_key="phx_example"
        ) as evaluations:
            with patch.object(
                evaluations._transport, "request", return_value=dataset_response()
            ):
                snapshot = evaluations.datasets.snapshot(DATASET_ID)
        return list(snapshot.iter_items())

    with pytest.raises(EvaluationAPIError) as caught:
        asyncio.run(run_async()) if asynchronous else run_sync()
    assert caught.value.code == "client_closed"
    assert caught.value.persistence == "not_sent"
