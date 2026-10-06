import asyncio
import copy
import inspect
import json
from datetime import timezone
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from posthog.ai.evaluations._errors import EvaluationAPIError
from posthog.ai.evaluations._scorer_types import (
    BooleanScorerConfig,
    CategoricalPassingRule,
    CategoricalScorerConfig,
    CategoricalScorerOption,
    NumericPassingRule,
    NumericScorerConfig,
)
from posthog.ai.evaluations._scorers import AsyncScorers, Scorers
from posthog.ai.evaluations._transport import AsyncTransport, SyncTransport


SCORER_ID = "8cba6d9e-d2ee-4b23-a545-a61057da5870"
VERSION_ID = "f72481ec-1310-41de-b5bb-d20e1c59f121"
BASE = "/api/projects/123/llm_analytics/score_definitions/"


def scorer_response(**overrides):
    return {
        "id": SCORER_ID,
        "name": "Quality",
        "description": "",
        "kind": "numeric",
        "archived": False,
        "current_version": 1,
        "current_version_id": VERSION_ID,
        "config": {"min": 0, "max": 1},
        "created_by": None,
        "created_at": "2026-10-02T10:00:00Z",
        "updated_at": "2026-10-02T10:00:00Z",
        "team": 123,
        **overrides,
    }


def version_response(**overrides):
    return {
        "id": VERSION_ID,
        "definition_id": SCORER_ID,
        "version": 1,
        "kind": "numeric",
        "config": {"min": 0, "max": 1},
        "created_at": "2026-10-02T10:00:00Z",
        "created_by": None,
        **overrides,
    }


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def scorers(request):
    transport = Mock(spec=SyncTransport)
    namespace = Scorers(transport, 123)
    if request.param:
        transport = AsyncMock(spec=AsyncTransport)
        namespace = AsyncScorers(transport, 123)
    transport.request.return_value = scorer_response()
    return namespace, transport


def invoke(method, *args, **kwargs):
    result = method(*args, **kwargs)
    return asyncio.run(result) if inspect.isawaitable(result) else result


@pytest.mark.parametrize(
    ("kind", "config"),
    [
        (
            "boolean",
            BooleanScorerConfig(
                true_is_failure=True, true_label="Unsafe", false_label="Safe"
            ),
        ),
        ("boolean", BooleanScorerConfig(true_is_failure=False)),
        ("boolean", BooleanScorerConfig(true_is_failure=None)),
        (
            "numeric",
            NumericScorerConfig(
                min=0,
                max=1,
                step=0.01,
                passing_rule=NumericPassingRule(operator="gte", threshold=0.8),
            ),
        ),
        (
            "numeric",
            NumericScorerConfig(
                min=None,
                max=None,
                step=None,
                passing_rule=NumericPassingRule(operator="lte", threshold=0),
            ),
        ),
        ("numeric", NumericScorerConfig(passing_rule=None)),
        (
            "categorical",
            CategoricalScorerConfig(
                options=[
                    CategoricalScorerOption(key="safe", label="Safe"),
                    CategoricalScorerOption(key="helpful", label="Helpful"),
                ],
                selection_mode="multiple",
                min_selections=1,
                max_selections=2,
                passing_rule=CategoricalPassingRule(categories=["safe", "helpful"]),
            ),
        ),
        (
            "categorical",
            CategoricalScorerConfig(
                options=[CategoricalScorerOption(key="safe", label="Safe")],
                selection_mode="single",
                min_selections=None,
                max_selections=None,
                passing_rule=None,
            ),
        ),
    ],
)
def test_typed_configs_preserve_polarity_and_nullable_fields(scorers, kind, config):
    namespace, transport = scorers
    transport.request.return_value = scorer_response(kind=kind, config=config)
    scorer = invoke(namespace.create, name="Quality", kind=kind, config=config)

    assert json.loads(transport.request.call_args.kwargs["body"]) == {
        "name": "Quality",
        "kind": kind,
        "config": config,
    }
    assert scorer.current_version_id == VERSION_ID
    assert scorer.config == config
    assert scorer.created_at.tzinfo == timezone.utc
    assert transport.request.call_args.args == ("POST", BASE)
    assert transport.request.call_args.kwargs["retry_safe"] is False


@pytest.mark.parametrize("description", [None, "", "説明"])
def test_create_preserves_explicit_description(scorers, description):
    namespace, transport = scorers
    invoke(
        namespace.create,
        name="Quality",
        kind="numeric",
        config=NumericScorerConfig(),
        description=description,
    )
    assert (
        json.loads(transport.request.call_args.kwargs["body"])["description"]
        == description
    )


def test_metadata_update_preserves_omission_null_and_false(scorers):
    namespace, transport = scorers
    invoke(namespace.update, SCORER_ID, description=None, archived=False)
    assert transport.request.call_args.args == ("PATCH", BASE + SCORER_ID + "/")
    assert json.loads(transport.request.call_args.kwargs["body"]) == {
        "description": None,
        "archived": False,
    }
    assert transport.request.call_args.kwargs["retry_safe"] is False
    invoke(namespace.update, SCORER_ID, name="Renamed")
    assert json.loads(transport.request.call_args.kwargs["body"]) == {"name": "Renamed"}


def test_version_bump_preserves_configuration_and_concurrency_guard(scorers):
    namespace, transport = scorers
    config = NumericScorerConfig(min=0, max=1)
    transport.request.return_value = scorer_response(current_version=2)
    updated = invoke(
        namespace.create_version,
        UUID(SCORER_ID),
        config=config,
        base_version=1,
        name="New name",
        description=None,
    )
    assert updated.current_version == 2
    assert updated.current_version_id == VERSION_ID
    assert transport.request.call_args.args == (
        "POST",
        BASE + SCORER_ID + "/new_version/",
    )
    assert json.loads(transport.request.call_args.kwargs["body"]) == {
        "config": config,
        "base_version": 1,
        "name": "New name",
        "description": None,
    }
    assert transport.request.call_args.kwargs["retry_safe"] is False


def test_stale_version_is_not_refreshed_or_retried(scorers):
    namespace, transport = scorers
    error = EvaluationAPIError(
        status=409, response={"current_version": 5}, persistence="rejected"
    )
    transport.request.side_effect = error
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(
            namespace.create_version,
            SCORER_ID,
            config=NumericScorerConfig(),
            base_version=4,
        )
    assert caught.value is error
    assert caught.value.response == {"current_version": 5}
    transport.request.assert_called_once()


def test_scorer_list_supports_filters_and_explicit_offset_continuation(scorers):
    namespace, transport = scorers
    transport.request.return_value = {
        "count": 3,
        "next": "https://us.posthog.com" + BASE + "?limit=1&offset=2&kind=numeric",
        "previous": "https://us.posthog.com" + BASE + "?limit=1&kind=numeric",
        "results": [scorer_response()],
    }
    page = invoke(
        namespace.list,
        limit=1,
        offset=1,
        search="Quality",
        kind="numeric",
        archived=False,
        order_by="-current_version",
    )
    assert page.count == 3
    assert page.next_offset == 2
    assert page.results[0].id == SCORER_ID
    assert transport.request.call_args.kwargs["params"] == {
        "limit": 1,
        "offset": 1,
        "search": "Quality",
        "kind": "numeric",
        "archived": "false",
        "order_by": "-current_version",
    }
    assert transport.request.call_args.args == ("GET", BASE)
    transport.request.return_value = {
        "count": 0,
        "next": None,
        "previous": None,
        "results": [],
    }
    assert invoke(namespace.list).next_offset is None
    assert "archived" not in transport.request.call_args.kwargs["params"]


def test_version_history_uses_cursors_instead_of_offsets(scorers):
    namespace, transport = scorers
    transport.request.return_value = {
        "count": 2,
        "next_cursor": "opaque==",
        "results": [version_response()],
    }
    page = invoke(namespace.list_versions, SCORER_ID, limit=1, cursor="prior==")
    assert page.next_cursor == "opaque=="
    assert page.results[0].id == VERSION_ID
    assert page.results[0].definition_id == SCORER_ID
    assert transport.request.call_args.args == ("GET", BASE + SCORER_ID + "/versions/")
    assert transport.request.call_args.kwargs["params"] == {
        "limit": 1,
        "cursor": "prior==",
    }
    transport.request.return_value = version_response()
    version = invoke(namespace.get_version, UUID(SCORER_ID), UUID(VERSION_ID))
    assert version.id == VERSION_ID
    assert transport.request.call_args.args == (
        "GET",
        BASE + SCORER_ID + "/versions/" + VERSION_ID + "/",
    )


@pytest.mark.parametrize(
    "invalid_id", ["Quality", "../other-project", "?limit=10", "", 123]
)
def test_definition_paths_require_uuid_and_never_lookup_by_name(scorers, invalid_id):
    namespace, transport = scorers
    with pytest.raises(ValueError, match="UUID"):
        invoke(namespace.get, invalid_id)
    with pytest.raises(ValueError, match="UUID"):
        invoke(namespace.get_version, SCORER_ID, invalid_id)
    transport.request.assert_not_called()


@pytest.mark.parametrize("invalid_limit", [0, -1, 101, True])
def test_version_page_limit_rejected_before_io(scorers, invalid_limit):
    namespace, transport = scorers
    with pytest.raises(ValueError):
        invoke(namespace.list_versions, SCORER_ID, limit=invalid_limit)
    transport.request.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        {"id": "invalid"},
        {"archived": "false"},
        {"created_at": "2026-10-02T12:00:00"},
        {"config": []},
        {"current_version": True},
        {"current_version_id": None},
    ],
)
def test_malformed_success_preserves_unknown_persistence(scorers, override):
    namespace, transport = scorers
    transport.request.return_value = scorer_response(**override)
    with pytest.raises(EvaluationAPIError) as caught:
        invoke(
            namespace.create,
            name="Quality",
            kind="numeric",
            config=NumericScorerConfig(),
        )
    assert caught.value.code == "invalid_response"
    assert caught.value.persistence == "unknown"
    assert "Quality" not in str(caught.value)
    transport.request.assert_called_once()


def test_configuration_response_is_detached_from_transport_and_creation_input(scorers):
    namespace, transport = scorers
    config = CategoricalScorerConfig(
        options=[CategoricalScorerOption(key="a", label="A")]
    )
    response = scorer_response(kind="categorical", config=config)
    transport.request.return_value = response
    scorer = invoke(namespace.create, name="Quality", kind="categorical", config=config)
    expected = copy.deepcopy(config)
    config["options"][0]["label"] = "Changed"
    assert scorer.config == expected
    assert json.loads(transport.request.call_args.kwargs["body"])["config"] == expected


def test_nonfinite_configuration_is_never_sent(scorers):
    namespace, transport = scorers
    with pytest.raises(ValueError):
        invoke(
            namespace.create,
            name="Quality",
            kind="numeric",
            config=NumericScorerConfig(min=float("nan")),
        )
    transport.request.assert_not_called()


def test_async_cancellation_propagates_without_followup_writes():
    async def run():
        transport = AsyncMock(spec=AsyncTransport)
        transport.request.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await AsyncScorers(transport, 123).create(
                name="Quality", kind="numeric", config=NumericScorerConfig()
            )
        transport.request.assert_called_once()

    asyncio.run(run())
