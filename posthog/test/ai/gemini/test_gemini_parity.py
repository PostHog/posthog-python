from unittest.mock import MagicMock, patch

import pytest

try:
    from google import genai as google_genai

    from posthog.ai.gemini import AsyncClient, Client
except ImportError:
    pytest.skip("Google Gemini package is not available", allow_module_level=True)


@pytest.mark.parametrize("client_class", [Client, AsyncClient])
def test_sync_and_async_clients_share_api_key_environment_precedence(
    client_class, monkeypatch
):
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("API_KEY", "legacy-key")

    with patch.object(google_genai, "Client") as provider_client:
        client_class(posthog_client=MagicMock())

    provider_client.assert_called_once_with(api_key="google-key")


@pytest.mark.parametrize("client_class", [Client, AsyncClient])
def test_sync_and_async_clients_preserve_explicit_empty_api_key(
    client_class, monkeypatch
):
    monkeypatch.setenv("GOOGLE_API_KEY", "environment-key")

    with patch.object(google_genai, "Client") as provider_client:
        client_class(api_key="", posthog_client=MagicMock())

    provider_client.assert_called_once_with(api_key="")


@pytest.mark.parametrize("client_class", [Client, AsyncClient])
def test_sync_and_async_clients_merge_posthog_defaults_without_mutation(client_class):
    default_properties = {"shared": "default", "default-only": True}

    with patch.object(google_genai, "Client"):
        client = client_class(
            api_key="test-key",
            posthog_client=MagicMock(),
            posthog_distinct_id="default-id",
            posthog_properties=default_properties,
            posthog_privacy_mode=True,
            posthog_groups={"organization": "default-org"},
        )

    merged = client.models._merge_posthog_params(
        None,
        "",
        {"shared": "call", "call-only": True},
        False,
        None,
    )

    assert merged == (
        "default-id",
        "",
        {"shared": "call", "default-only": True, "call-only": True},
        False,
        {"organization": "default-org"},
    )
    assert default_properties == {"shared": "default", "default-only": True}
