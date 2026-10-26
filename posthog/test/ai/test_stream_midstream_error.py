"""A stream that raises mid-flight must be captured as an error, not as a 200."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from posthog.ai.types import StreamingEventData
from posthog.ai.utils import capture_streaming_event


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        yield mock_client


def _props(mock_client):
    assert mock_client.capture.call_count == 1
    return mock_client.capture.call_args[1]["properties"]


class _StatusError(RuntimeError):
    status_code = 529


def _event_data(**overrides) -> StreamingEventData:
    data = StreamingEventData(
        provider="openai",
        model="gpt-4",
        base_url="https://api.openai.com/v1",
        kwargs={},
        formatted_input=[],
        formatted_output=[],
        usage_stats={},
        latency=0.1,
        distinct_id="id",
        trace_id="trace",
        properties=None,
        privacy_mode=False,
        groups=None,
        stop_reason=None,
    )
    data.update(overrides)
    return data


def test_capture_streaming_event_without_error_is_ok(mock_client):
    capture_streaming_event(mock_client, _event_data())
    props = _props(mock_client)
    assert props["$ai_http_status"] == 200
    assert "$ai_is_error" not in props


@pytest.mark.parametrize(
    "error, status", [(RuntimeError("boom"), 0), (_StatusError("overloaded"), 529)]
)
def test_capture_streaming_event_with_error(mock_client, error, status):
    capture_streaming_event(mock_client, _event_data(error=error))
    props = _props(mock_client)
    assert props["$ai_http_status"] == status
    assert props["$ai_is_error"] is True
    assert props["$ai_error"] == str(error)


def test_openai_chat_stream_error(mock_client):
    from openai.types.chat import ChatCompletionChunk
    from openai.types.chat.chat_completion_chunk import Choice, ChoiceDelta

    from posthog.ai.openai import OpenAI

    chunk = ChatCompletionChunk(
        id="c1",
        created=1,
        model="gpt-4",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="partial"))],
    )

    def stream():
        yield chunk
        raise RuntimeError("connection reset")

    with patch("openai.resources.chat.completions.Completions.create") as create:
        create.return_value = stream()
        client = OpenAI(api_key="k", posthog_client=mock_client)
        with pytest.raises(RuntimeError, match="connection reset"):
            list(
                client.chat.completions.create(
                    model="gpt-4",
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                )
            )

    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_error"] == "connection reset"
    assert props["$ai_http_status"] == 0


@pytest.mark.asyncio
async def test_openai_chat_stream_error_async(mock_client):
    from openai.types.chat import ChatCompletionChunk
    from openai.types.chat.chat_completion_chunk import Choice, ChoiceDelta

    from posthog.ai.openai import AsyncOpenAI

    chunk = ChatCompletionChunk(
        id="c1",
        created=1,
        model="gpt-4",
        object="chat.completion.chunk",
        choices=[Choice(index=0, delta=ChoiceDelta(content="partial"))],
    )

    async def stream():
        yield chunk
        raise RuntimeError("connection reset")

    async def create(self, **kwargs):
        return stream()

    with patch("openai.resources.chat.completions.AsyncCompletions.create", new=create):
        client = AsyncOpenAI(api_key="k", posthog_client=mock_client)
        response = await client.chat.completions.create(
            model="gpt-4", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        with pytest.raises(RuntimeError, match="connection reset"):
            async for _ in response:
                pass

    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == 0


def test_openai_responses_stream_error(mock_client):
    from posthog.ai.openai import OpenAI

    def stream():
        yield MagicMock(type="response.created", response=None)
        raise RuntimeError("connection reset")

    with patch("openai.resources.responses.Responses.create") as create:
        create.return_value = stream()
        client = OpenAI(api_key="k", posthog_client=mock_client)
        with pytest.raises(RuntimeError, match="connection reset"):
            list(client.responses.create(model="gpt-4", input="hi", stream=True))

    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_error"] == "connection reset"


def test_anthropic_stream_error(mock_client):
    from posthog.ai.anthropic import Anthropic

    def stream():
        yield MagicMock(type="message_start", message=None)
        raise _StatusError("overloaded")

    with patch("anthropic.resources.messages.Messages.create") as create:
        create.return_value = stream()
        client = Anthropic(api_key="k", posthog_client=mock_client)
        with pytest.raises(_StatusError):
            list(
                client.messages.create(
                    model="claude-3-opus-20240229",
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=5,
                    stream=True,
                )
            )

    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == 529


@pytest.mark.asyncio
async def test_anthropic_stream_error_async(mock_client):
    from posthog.ai.anthropic import AsyncAnthropic

    async def stream():
        yield MagicMock(type="message_start", message=None)
        raise _StatusError("overloaded")

    async def create(**kwargs):
        return stream()

    with patch("anthropic.resources.messages.AsyncMessages.create", side_effect=create):
        client = AsyncAnthropic(api_key="k", posthog_client=mock_client)
        response = await client.messages.create(
            model="claude-3-opus-20240229",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=5,
            stream=True,
        )
        with pytest.raises(_StatusError):
            async for _ in response:
                pass

    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == 529


def _gemini_chunk():
    chunk = MagicMock()
    chunk.text = "partial"
    chunk.usage_metadata = None
    return chunk


def test_gemini_stream_error(mock_client):
    pytest.importorskip("google.genai")
    from google import genai as google_genai

    from posthog.ai.gemini import Client

    def stream():
        yield _gemini_chunk()
        raise RuntimeError("503 overloaded mid-stream")

    with patch.object(google_genai, "Client") as cls:
        inst = MagicMock()
        cls.return_value = inst
        inst.models.generate_content_stream.return_value = stream()
        client = Client(api_key="k", posthog_client=mock_client)
        with pytest.raises(RuntimeError, match="503"):
            list(
                client.models.generate_content_stream(
                    model="gemini-2.0-flash", contents=["hi"]
                )
            )

    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_error"] == "503 overloaded mid-stream"
    assert props["$ai_http_status"] == 0


@pytest.mark.asyncio
async def test_gemini_stream_error_async(mock_client):
    pytest.importorskip("google.genai")
    from google import genai as google_genai

    from posthog.ai.gemini import AsyncClient

    async def stream():
        yield _gemini_chunk()
        raise RuntimeError("503 overloaded mid-stream")

    with patch.object(google_genai, "Client") as cls:
        inst = MagicMock()
        cls.return_value = inst
        inst.aio.models.generate_content_stream = AsyncMock(return_value=stream())
        client = AsyncClient(api_key="k", posthog_client=mock_client)
        response = await client.models.generate_content_stream(
            model="gemini-2.0-flash", contents=["hi"]
        )
        with pytest.raises(RuntimeError, match="503"):
            async for _ in response:
                pass

    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == 0
