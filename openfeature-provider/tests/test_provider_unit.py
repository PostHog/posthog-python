import pytest
from openfeature.evaluation_context import EvaluationContext
from openfeature.exception import (
    FlagNotFoundError,
    TargetingKeyMissingError,
    TypeMismatchError,
)
from openfeature.flag_evaluation import Reason

from openfeature.contrib.provider.posthog import PostHogProvider

from tests.conftest import make_result


def _provider(fake_client, **kwargs):
    return PostHogProvider(fake_client, default_distinct_id="anon", **kwargs)


def test_metadata(fake_client):
    assert _provider(fake_client).get_metadata().name == "PostHogProvider"


def test_boolean_targeting_match(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant=None
    )
    details = _provider(fake_client).resolve_boolean_details(
        "flag", False, EvaluationContext("user-1")
    )
    assert details.value is True
    assert details.reason == Reason.TARGETING_MATCH
    fake_client.get_feature_flag_result.assert_called_once()


def test_boolean_disabled(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=False, variant=None, reason="no match"
    )
    details = _provider(fake_client).resolve_boolean_details(
        "flag", True, EvaluationContext("user-1")
    )
    assert details.value is False
    assert details.reason == Reason.DISABLED


def test_string_variant(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="control"
    )
    details = _provider(fake_client).resolve_string_details(
        "exp", "x", EvaluationContext("user-1")
    )
    assert details.value == "control"
    assert details.variant == "control"


def test_string_on_boolean_flag_is_type_mismatch(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant=None
    )
    with pytest.raises(TypeMismatchError):
        _provider(fake_client).resolve_string_details(
            "flag", "x", EvaluationContext("user-1")
        )


def test_integer_variant_parse(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="42"
    )
    details = _provider(fake_client).resolve_integer_details(
        "n", 0, EvaluationContext("u")
    )
    assert details.value == 42


def test_integer_variant_parse_failure(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="not-an-int"
    )
    with pytest.raises(TypeMismatchError):
        _provider(fake_client).resolve_integer_details("n", 0, EvaluationContext("u"))


def test_float_variant_parse(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="3.5"
    )
    details = _provider(fake_client).resolve_float_details(
        "n", 0.0, EvaluationContext("u")
    )
    assert details.value == 3.5


def test_object_payload(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="v1", payload={"color": "blue"}
    )
    details = _provider(fake_client).resolve_object_details(
        "cfg", {}, EvaluationContext("u")
    )
    assert details.value == {"color": "blue"}


def test_object_missing_payload_is_type_mismatch(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="v1", payload=None
    )
    with pytest.raises(TypeMismatchError):
        _provider(fake_client).resolve_object_details("cfg", {}, EvaluationContext("u"))


def test_flag_not_found_raises(fake_client):
    fake_client.get_feature_flag_result.return_value = None
    with pytest.raises(FlagNotFoundError):
        _provider(fake_client).resolve_boolean_details(
            "missing", False, EvaluationContext("u")
        )


def test_missing_targeting_key_no_default(fake_client):
    provider = PostHogProvider(fake_client)  # no default_distinct_id
    with pytest.raises(TargetingKeyMissingError):
        provider.resolve_boolean_details("flag", False, EvaluationContext())


def test_default_distinct_id_used_when_no_targeting_key(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result()
    _provider(fake_client).resolve_boolean_details("flag", False, EvaluationContext())
    args, _ = fake_client.get_feature_flag_result.call_args
    assert args[1] == "anon"


def test_context_split(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result()
    ctx = EvaluationContext(
        "u",
        {
            "plan": "pro",
            "groups": {"org": "acme"},
            "group_properties": {"org": {"tier": "ent"}},
        },
    )
    _provider(fake_client).resolve_boolean_details("flag", False, ctx)
    kwargs = fake_client.get_feature_flag_result.call_args.kwargs
    assert kwargs["groups"] == {"org": "acme"}
    assert kwargs["group_properties"] == {"org": {"tier": "ent"}}
    assert kwargs["person_properties"] == {"plan": "pro"}


def test_send_feature_flag_events_forwarded(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result()
    _provider(fake_client, send_feature_flag_events=False).resolve_boolean_details(
        "flag", False, EvaluationContext("u")
    )
    assert (
        fake_client.get_feature_flag_result.call_args.kwargs["send_feature_flag_events"]
        is False
    )
