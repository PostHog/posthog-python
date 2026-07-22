from unittest.mock import MagicMock

import pytest
from openfeature import api

from posthog.types import FeatureFlagResult


def make_result(
    key: str = "flag",
    enabled: bool = True,
    variant=None,
    payload=None,
    reason: str = "matched condition set 1",
) -> FeatureFlagResult:
    return FeatureFlagResult(
        key=key,
        enabled=enabled,
        variant=variant,
        payload=payload,
        reason=reason,
    )


@pytest.fixture
def fake_client():
    """A stand-in Posthog client exposing only ``get_feature_flag_result``."""
    client = MagicMock()
    # No secret key -> provider.initialize() skips load_feature_flags().
    client.secret_key = None
    return client


@pytest.fixture(autouse=True)
def _reset_openfeature():
    """Reset global OpenFeature provider state between tests."""
    yield
    clear = getattr(api, "clear_providers", None)
    if callable(clear):
        clear()
