"""End-to-end tests through the OpenFeature public evaluation API."""

from openfeature import api
from openfeature.evaluation_context import EvaluationContext

from openfeature.contrib.provider.posthog import PostHogProvider

from tests.conftest import make_result


def _register(fake_client):
    api.set_provider(PostHogProvider(fake_client, default_distinct_id="anon"))
    return api.get_client()


def test_end_to_end_boolean_true(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(enabled=True)
    client = _register(fake_client)
    ctx = EvaluationContext(targeting_key="user-123")
    assert client.get_boolean_value("flag", False, ctx) is True


def test_end_to_end_string_variant(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="blue"
    )
    client = _register(fake_client)
    ctx = EvaluationContext(targeting_key="user-123")
    assert client.get_string_value("exp", "control", ctx) == "blue"


def test_end_to_end_object_payload(fake_client):
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant="v1", payload={"hex": "#00f"}
    )
    client = _register(fake_client)
    ctx = EvaluationContext(targeting_key="user-123")
    assert client.get_object_value("cfg", {}, ctx) == {"hex": "#00f"}


def test_end_to_end_default_on_missing_flag(fake_client):
    fake_client.get_feature_flag_result.return_value = None
    client = _register(fake_client)
    ctx = EvaluationContext(targeting_key="user-123")
    # FlagNotFoundError inside the provider -> SDK returns the caller's default.
    assert client.get_boolean_value("missing", True, ctx) is True


def test_end_to_end_default_on_type_mismatch(fake_client):
    # Boolean flag (no variant) read as a string -> TYPE_MISMATCH -> default.
    fake_client.get_feature_flag_result.return_value = make_result(
        enabled=True, variant=None
    )
    client = _register(fake_client)
    ctx = EvaluationContext(targeting_key="user-123")
    assert client.get_string_value("flag", "fallback", ctx) == "fallback"
