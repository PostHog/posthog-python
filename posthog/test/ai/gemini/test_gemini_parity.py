from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.fixture
def provider_client_class():
    with patch.object(google_genai, "Client") as patched:
        patched.return_value = MagicMock()
        yield patched


@pytest.fixture
def provider_client(provider_client_class):
    """A stand-in for the underlying google-genai client, with both surfaces."""
    return provider_client_class.return_value


@pytest.fixture
def gemini_response():
    response = MagicMock()
    response.text = "Test response from Gemini"

    usage = MagicMock()
    usage.prompt_token_count = 20
    usage.candidates_token_count = 10
    usage.cached_content_token_count = 0
    usage.thoughts_token_count = 0
    response.usage_metadata = usage

    part = MagicMock()
    part.text = "Test response from Gemini"
    content = MagicMock()
    content.parts = [part]
    candidate = MagicMock()
    candidate.content = content
    response.candidates = [candidate]

    return response


@pytest.mark.parametrize("client_class", [Client, AsyncClient])
def test_every_surface_shares_one_provider_client(
    client_class, provider_client_class, provider_client
):
    """`models`, `aio.models` and `files` must not open separate connections."""
    client = client_class(api_key="test-key", posthog_client=MagicMock())

    provider_client_class.assert_called_once()
    assert client.models._client is provider_client
    assert client.aio.models._client is provider_client


def test_sync_client_exposes_the_provider_files_api(provider_client):
    client = Client(api_key="test-key", posthog_client=MagicMock())

    assert client.files is provider_client.files
    assert client.aio.files is provider_client.aio.files

    uploaded = client.files.upload(file="notes.pdf")

    provider_client.files.upload.assert_called_once_with(file="notes.pdf")
    assert uploaded is provider_client.files.upload.return_value


def test_async_client_exposes_the_async_files_api(provider_client):
    """AsyncClient is async end to end, so its files API is the aio one."""
    client = AsyncClient(api_key="test-key", posthog_client=MagicMock())

    assert client.files is provider_client.aio.files
    assert client.aio.files is provider_client.aio.files
    # `models` is already async; `aio.models` is an alias so SDK-shaped code works.
    assert client.aio.models is client.models


def test_sync_client_aio_models_inherits_posthog_defaults(provider_client):
    client = Client(
        api_key="test-key",
        posthog_client=MagicMock(),
        posthog_distinct_id="default-id",
        posthog_properties={"team": "ai"},
        posthog_privacy_mode=True,
        posthog_groups={"organization": "default-org"},
    )

    assert client.aio.models._merge_posthog_params(None, "trace", None, None, None) == (
        "default-id",
        "trace",
        {"team": "ai"},
        True,
        {"organization": "default-org"},
    )


@pytest.mark.asyncio
async def test_sync_client_aio_models_tracks_generations(
    provider_client, gemini_response
):
    """`client.aio.models.generate_content` is the drop-in async entry point."""
    provider_client.aio.models.generate_content = AsyncMock(
        return_value=gemini_response
    )
    posthog_client = MagicMock()
    posthog_client.privacy_mode = False

    client = Client(api_key="test-key", posthog_client=posthog_client)

    response = await client.aio.models.generate_content(
        model="gemini-2.0-flash",
        contents=["Tell me a fun fact about hedgehogs"],
        posthog_distinct_id="test-id",
    )

    assert response is gemini_response
    provider_client.aio.models.generate_content.assert_awaited_once()

    assert posthog_client.capture.call_count == 1
    call_args = posthog_client.capture.call_args[1]
    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert call_args["properties"]["$ai_model"] == "gemini-2.0-flash"
