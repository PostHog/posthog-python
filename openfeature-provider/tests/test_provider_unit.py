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


@pytest.mark.parametrize(
    ("enabled", "reason", "expected_value", "expected_reason"),
    [
        (True, "matched condition set 1", True, Reason.TARGETING_MATCH),
        # Active flag, user matched nothing -> DEFAULT (not DISABLED).
        (False, "no condition set matched", False, Reason.DEFAULT),
        # Only an explicitly-disabled flag maps to DISABLED.
        (False, "flag is disabled", False, Reason.DISABLED),
    ],
)
def test_boolean_reason_mapping(
    fake_client, enabled, reason, expected_value, expected_reason
):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=enabled, variant=None, reason=reason
    )
    details = _provider(fake_client).resolve_boolean_details(
        "flag", not expected_value, EvaluationContext("user-1")
    )
    assert details.value is expected_value
    assert details.reason == expected_reason
    assert details.flag_metadata["posthog_reason"] == reason
    fake_client.get_feature_flag_result.assert_called_once()


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


@pytest.mark.parametrize(
    ("resolver", "variant", "expected"),
    [
        ("resolve_integer_details", "42", 42),
        ("resolve_integer_details", "3", 3),
        ("resolve_float_details", "3.5", 3.5),
        ("resolve_float_details", "3", 3.0),
    ],
)
def test_number_variant_parse(fake_client, resolver, variant, expected):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant=variant
    )
    details = getattr(_provider(fake_client), resolver)("n", 0, EvaluationContext("u"))
    assert details.value == expected


@pytest.mark.parametrize(
    ("resolver", "variant"),
    [
        ("resolve_integer_details", "not-an-int"),
        ("resolve_integer_details", None),
        ("resolve_float_details", "abc"),
        ("resolve_float_details", None),
    ],
)
def test_number_variant_parse_failure(fake_client, resolver, variant):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant=variant
    )
    with pytest.raises(TypeMismatchError):
        getattr(_provider(fake_client), resolver)("n", 0, EvaluationContext("u"))


def test_object_payload(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="v1", payload={"color": "blue"}
    )
    details = _provider(fake_client).resolve_object_details(
        "cfg", {}, EvaluationContext("u")
    )
    assert details.value == {"color": "blue"}


def test_object_payload_list(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="v1", payload=[1, 2, 3]
    )
    details = _provider(fake_client).resolve_object_details(
        "cfg", {}, EvaluationContext("u")
    )
    assert details.value == [1, 2, 3]


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


def test_initialize_skips_preload_without_personal_api_key(fake_client):
    # fake_client.personal_api_key is None by default.
    PostHogProvider(fake_client).initialize(EvaluationContext())
    fake_client.load_feature_flags.assert_not_called()


def test_initialize_logs_warning_on_preload_failure(fake_client, caplog):
    fake_client.personal_api_key = "phx_test"
    fake_client.load_feature_flags.side_effect = RuntimeError("bad key")
    with caplog.at_level("WARNING"):
        PostHogProvider(fake_client).initialize(EvaluationContext())
    fake_client.load_feature_flags.assert_called_once()
    assert "failed to preload" in caplog.text


def test_shutdown_does_not_touch_client(fake_client):
    PostHogProvider(fake_client).shutdown()
    fake_client.shutdown.assert_not_called()
