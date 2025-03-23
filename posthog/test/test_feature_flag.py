import pytest
from posthog.types import FeatureFlag


def test_from_value_and_payload_boolean_true_value():
    # Test with boolean value
    flag = FeatureFlag.from_value_and_payload(key="my-flag", value=True, payload='{"some": "data"}')

    assert flag.key == "my-flag"
    assert flag.enabled is True
    assert flag.variant is None
    assert flag.metadata.payload == '{"some": "data"}'


def test_from_value_and_payload_boolean_false_value():
    # Test with False value
    flag = FeatureFlag.from_value_and_payload(key="my-flag", value=False, payload=None)

    assert flag.key == "my-flag"
    assert flag.enabled is False
    assert flag.variant is None
    assert flag.metadata.payload is None


def test_from_value_and_payload_string_variant():
    flag = FeatureFlag.from_value_and_payload(
        key="my-flag", value="test-variant", payload='{"variant": "test-variant"}'
    )

    assert flag.key == "my-flag"
    assert flag.enabled is True  # String values should make enabled True
    assert flag.variant == "test-variant"
    assert flag.metadata.payload == '{"variant": "test-variant"}'
