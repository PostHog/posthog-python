import json
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
import requests

from posthog.ai.evaluations import (
    AsyncExperiment,
    AsyncOfflineEvaluations,
    EvaluationAPIError,
    OfflineEvaluations,
)

from .test_evaluations import BASE, EXPERIMENT_ID, invoke


RESOURCE_ID = UUID("e717c026-18ae-4640-a33e-109c19c97328")
EXPERIMENT_URL = f"https://us.posthog.com{BASE}{EXPERIMENT_ID}/"


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def api(request):
    """Exercise both public clients down to the prepared HTTP request."""
    asynchronous = request.param
    client = (AsyncOfflineEvaluations if asynchronous else OfflineEvaluations)(
        project_id=123, secret_key="test-secret"
    )
    http = client._transport._client if asynchronous else client._transport._session

    def respond(status, data=None):
        body = b"" if data is None else json.dumps(data).encode()
        if asynchronous:
            return httpx.Response(status, content=body)
        response = requests.Response()
        response.status_code = status
        response._content = body
        return response

    with patch.object(http, "send") as send:
        try:
            yield client.resume_experiment(EXPERIMENT_ID), send, respond
        finally:
            invoke(client.aclose if asynchronous else client.close)


@pytest.mark.parametrize(
    "resource,collection,revision_suffix,fields,payload",
    [
        (
            "item",
            "items",
            "",
            {
                "case_key": None,
                "trial": "2",
                "model_version": "v2",
                "application_trace_id": "trace",
            },
            {"output": None},
        ),
        (
            "result",
            "results",
            "payload/",
            {"status": "ok", "value": False},
            {"metadata": {"reviewed": True}, "reasoning": None},
        ),
    ],
)
def test_read_update_and_delete_with_revisions(
    api, resource, collection, revision_suffix, fields, payload
):
    experiment, send, respond = api
    # An unchanged score keeps its revision. Item updates advance theirs.
    after_update = 0 if resource == "result" else 1
    after_payload = after_update + 1
    send.side_effect = [
        respond(200, {"id": str(RESOURCE_ID), "revision": 0}),
        respond(200, {"revision": after_update}),
        respond(200, {"revision": after_payload}),
        respond(204),
        respond(200, {"id": str(RESOURCE_ID), "revision": after_payload + 1}),
        respond(204),
    ]
    read = getattr(experiment, f"get_{resource}_revision")
    revision = invoke(read, RESOURCE_ID)
    assert revision == 0
    revision = invoke(
        getattr(experiment, f"update_{resource}"),
        RESOURCE_ID,
        revision=revision,
        **fields,
    )
    assert revision == after_update
    revision = invoke(
        getattr(experiment, f"update_{resource}_payload"),
        RESOURCE_ID,
        revision=revision,
        payload=payload,
    )
    assert revision == after_payload
    assert (
        invoke(
            getattr(experiment, f"delete_{resource}_payload"),
            RESOURCE_ID,
            revision=revision,
        )
        is None
    )
    revision = invoke(read, RESOURCE_ID)
    assert (
        invoke(
            getattr(experiment, f"delete_{resource}"), RESOURCE_ID, revision=revision
        )
        is None
    )

    url = f"{EXPERIMENT_URL}{collection}/{RESOURCE_ID}/"
    requests_sent = [call.args[0] for call in send.call_args_list]
    assert [(req.method, str(req.url)) for req in requests_sent] == [
        ("GET", url + revision_suffix),
        ("PATCH", url),
        ("PATCH", url + "payload/"),
        ("DELETE", url + f"payload/?revision={after_payload}"),
        ("GET", url + revision_suffix),
        ("DELETE", url + f"?revision={after_payload + 1}"),
    ]
    bodies = [
        req.content if isinstance(req, httpx.Request) else req.body
        for req in requests_sent
    ]
    assert [json.loads(body) if body else None for body in bodies] == [
        None,
        {"revision": 0, **fields},
        {"revision": after_update, "payload": payload},
        None,
        None,
        None,
    ]


def test_ok_update_can_omit_value(api):
    experiment, send, respond = api
    send.return_value = respond(200, {"revision": 0})

    assert invoke(experiment.update_result, RESOURCE_ID, revision=0, status="ok") == 0
    send.assert_called_once()
    request = send.call_args.args[0]
    body = request.content if isinstance(request, httpx.Request) else request.body
    assert json.loads(body) == {"revision": 0, "status": "ok"}


def test_retry_revision_read_then_delete_with_revision_zero(api):
    experiment, send, respond = api
    send.side_effect = [
        respond(503, {"code": "unavailable"}),
        respond(200, {"id": str(RESOURCE_ID), "revision": 0}),
        respond(204),
    ]
    with (
        patch("posthog.ai.evaluations._transport.time.sleep"),
        patch("posthog.ai.evaluations._transport.asyncio.sleep"),
    ):
        revision = invoke(experiment.get_item_revision, RESOURCE_ID)
    assert revision == 0
    assert invoke(experiment.delete_item, RESOURCE_ID, revision=revision) is None

    url = f"{EXPERIMENT_URL}items/{RESOURCE_ID}/"
    requests_sent = [call.args[0] for call in send.call_args_list]
    assert [(req.method, str(req.url)) for req in requests_sent] == [
        ("GET", url),
        ("GET", url),
        ("DELETE", url + "?revision=0"),
    ]


@pytest.mark.parametrize("method", ["update_item_payload", "update_result_payload"])
def test_empty_payload_update_is_sent(api, method):
    experiment, send, respond = api
    update = getattr(experiment, method)
    send.side_effect = [
        respond(200, {"revision": 4}),
        respond(409, {"code": "payload_unavailable"}),
    ]

    assert invoke(update, RESOURCE_ID, revision=3, payload={}) == 4
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(update, RESOURCE_ID, revision=4, payload={})
    assert (caught.value.status, caught.value.code) == (409, "payload_unavailable")

    for call, revision in zip(send.call_args_list, [3, 4], strict=True):
        request = call.args[0]
        body = request.content if isinstance(request, httpx.Request) else request.body
        assert json.loads(body) == {"revision": revision, "payload": {}}


@pytest.mark.parametrize("method", ["update_result", "delete_result"])
@pytest.mark.parametrize(
    "status,code,persistence",
    [(409, "revision_conflict", "rejected"), (503, "unavailable", "unknown")],
)
def test_mutations_surface_errors_without_replaying(
    api, method, status, code, persistence
):
    experiment, send, respond = api
    send.side_effect = [respond(status, {"code": code})]
    fields = {"error_code": "timeout"} if method == "update_result" else {}
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(getattr(experiment, method), RESOURCE_ID, revision=1, **fields)
    assert (caught.value.status, caught.value.code, caught.value.persistence) == (
        status,
        code,
        persistence,
    )
    send.assert_called_once()


@pytest.mark.parametrize("method", ["update_result", "delete_result"])
def test_lost_mutation_response_is_unknown_and_never_retried(api, method):
    experiment, send, _ = api
    send.side_effect = (
        httpx.ReadTimeout("lost response")
        if isinstance(experiment, AsyncExperiment)
        else requests.ReadTimeout("lost response")
    )
    fields = {"value": True} if method == "update_result" else {}
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(getattr(experiment, method), RESOURCE_ID, revision=1, **fields)
    assert (caught.value.code, caught.value.persistence) == (
        "transport_error",
        "unknown",
    )
    send.assert_called_once()


@pytest.mark.parametrize(
    "method,fields,status,response",
    [
        ("get_item_revision", {}, 200, {"id": EXPERIMENT_ID, "revision": 2}),
        ("get_result_revision", {}, 200, {"id": str(RESOURCE_ID)}),
        ("update_result", {"revision": 0, "value": 0}, 204, None),
        ("update_item", {"revision": 0, "trial": None}, 200, {"revision": False}),
        ("update_item", {"revision": 3, "trial": None}, 200, {"revision": 2}),
        ("delete_item", {"revision": 0}, 202, {}),
    ],
)
def test_malformed_acknowledgments_have_unknown_outcome(
    api, method, fields, status, response
):
    experiment, send, respond = api
    send.side_effect = [respond(status, response)]
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(getattr(experiment, method), RESOURCE_ID, **fields)
    assert caught.value.code == "invalid_response"
    assert caught.value.persistence == "unknown"
    send.assert_called_once()


@pytest.mark.parametrize(
    "method,identity,fields",
    [
        ("update_item", RESOURCE_ID, {"revision": 0}),
        ("update_result", RESOURCE_ID, {"revision": 1, "value": 2**53 + 1}),
        ("update_result", RESOURCE_ID, {"revision": 0, "status": "ok", "value": None}),
        (
            "update_result",
            RESOURCE_ID,
            {"revision": 0, "value": False, "error_code": "timeout"},
        ),
        (
            "update_result",
            RESOURCE_ID,
            {"revision": 1, "status": "error", "value": False},
        ),
        (
            "update_result_payload",
            RESOURCE_ID,
            {"revision": 1, "payload": {"error_message": 2}},
        ),
        (
            "update_item_payload",
            RESOURCE_ID,
            {"revision": 1, "payload": {"reasoning": "unexpected"}},
        ),
        ("update_item", RESOURCE_ID, {"revision": 0, "dataset_item_version_id": None}),
        ("delete_item", RESOURCE_ID, {"revision": False}),
        ("delete_result_payload", RESOURCE_ID, {"revision": -1}),
        ("delete_result", "../items", {"revision": 0}),
    ],
)
def test_invalid_mutations_send_nothing(api, method, identity, fields):
    experiment, send, _ = api
    with pytest.raises((ValueError, TypeError)):
        invoke(getattr(experiment, method), identity, **fields)
    send.assert_not_called()
