"""A stream that raises mid-flight must be captured as an error, not as a 200."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from posthog.ai.types import StreamingEventData
from posthog.ai.utils import capture_streaming_event


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        yield mock_client


def _props(mock_client):
    assert mock_client.capture_ai.call_count == 1
    return mock_client.capture_ai.call_args[1]["properties"]


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


@pytest.fixture(
    params=[
        pytest.param(
            (
                {"code": "server_error", "message": "provider failed for this input"},
                "provider failed for this input",
                "_ResponsesStreamError server_error",
            ),
            id="server-error",
        ),
        pytest.param(
            (
                {
                    "code": "rate_limit_exceeded",
                    "message": "provider failed for this input",
                },
                "provider failed for this input",
                "_ResponsesStreamError rate_limit_exceeded",
            ),
            id="rate-limit",
        ),
        pytest.param(
            (None, "OpenAI response failed", "_ResponsesStreamError"),
            id="missing-error",
        ),
        pytest.param(
            (
                {"code": "server_error", "message": ""},
                "OpenAI response failed",
                "_ResponsesStreamError server_error",
            ),
            id="empty-message",
        ),
        pytest.param(
            (
                {"code": "server_error", "message": " \t\n "},
                "OpenAI response failed",
                "_ResponsesStreamError server_error",
            ),
            id="whitespace-message",
        ),
        pytest.param(
            (
                {
                    "code": "invalid code containing SECRET_INPUT",
                    "message": "provider failed for this input",
                },
                "provider failed for this input",
                "_ResponsesStreamError",
            ),
            id="unsafe-code",
        ),
    ]
)
def failed_response_error(request):
    return request.param


@pytest.fixture
def failed_response_transport(failed_response_error):
    error, _, _ = failed_response_error
    event = {
        "type": "response.failed",
        "sequence_number": 1,
        "response": {
            "id": "resp_failed",
            "status": "failed",
            "model": "gpt-4",
            "output": [],
            "error": error,
        },
    }

    def respond(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
        )

    return httpx.MockTransport(respond)


@pytest.mark.parametrize("privacy_mode", [False, True])
def test_openai_responses_failed_event(
    mock_client, failed_response_transport, failed_response_error, privacy_mode
):
    from posthog.ai.openai import OpenAI

    with OpenAI(
        api_key="test",
        posthog_client=mock_client,
        http_client=httpx.Client(transport=failed_response_transport),
    ) as client:
        events = list(
            client.responses.create(
                model="gpt-4",
                input="hi",
                stream=True,
                posthog_distinct_id="user",
                posthog_options={"process_person_profile": True},
                posthog_privacy_mode=privacy_mode,
            )
        )

    assert [event.type for event in events] == ["response.failed"]
    error, message, privacy_summary = failed_response_error
    if error is None:
        assert events[0].response.error is None
    else:
        assert events[0].response.error.code == error["code"]
        assert events[0].response.error.message == error["message"]
    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == 0
    assert props["$ai_stop_reason"] == "failed"
    assert props["$ai_error"] == (privacy_summary if privacy_mode else message)
    assert mock_client.capture_ai.call_args.kwargs["options"]["process_person_profile"]


@pytest.mark.asyncio
@pytest.mark.parametrize("privacy_mode", [False, True])
async def test_openai_responses_failed_event_async(
    mock_client, failed_response_transport, failed_response_error, privacy_mode
):
    from posthog.ai.openai import AsyncOpenAI

    async with AsyncOpenAI(
        api_key="test",
        posthog_client=mock_client,
        http_client=httpx.AsyncClient(transport=failed_response_transport),
    ) as client:
        stream = await client.responses.create(
            model="gpt-4",
            input="hi",
            stream=True,
            posthog_distinct_id="user",
            posthog_options={"process_person_profile": True},
            posthog_privacy_mode=privacy_mode,
        )
        events = [event async for event in stream]

    assert [event.type for event in events] == ["response.failed"]
    error, message, privacy_summary = failed_response_error
    if error is None:
        assert events[0].response.error is None
    else:
        assert events[0].response.error.code == error["code"]
        assert events[0].response.error.message == error["message"]
    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == 0
    assert props["$ai_error"] == (privacy_summary if privacy_mode else message)
    assert props["$ai_stop_reason"] == "failed"
    assert mock_client.capture_ai.call_args.kwargs["options"]["process_person_profile"]


@pytest.fixture
def failed_response_chunk():
    return SimpleNamespace(
        type="response.failed",
        response=SimpleNamespace(
            status="failed",
            model="gpt-4",
            output=[],
            usage=None,
            error=SimpleNamespace(code="server_error", message="event failure"),
        ),
    )


@pytest.mark.parametrize("privacy_mode", [False, True])
def test_openai_responses_stream_exception_precedence(
    mock_client, failed_response_chunk, privacy_mode
):
    from posthog.ai.openai import OpenAI

    failure = _StatusError("stream reset")

    def stream():
        yield failed_response_chunk
        raise failure

    with patch("openai.resources.responses.Responses.create", return_value=stream()):
        with OpenAI(api_key="test", posthog_client=mock_client) as client:
            events = client.responses.create(
                model="gpt-4",
                input="hi",
                stream=True,
                posthog_privacy_mode=privacy_mode,
            )
            assert next(events) is failed_response_chunk
            with pytest.raises(_StatusError, match="stream reset") as raised:
                next(events)

    assert raised.value is failure
    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == 529
    assert props["$ai_error"] == (
        "_StatusError: 529" if privacy_mode else "stream reset"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("privacy_mode", [False, True])
async def test_openai_responses_stream_exception_precedence_async(
    mock_client, failed_response_chunk, privacy_mode
):
    from posthog.ai.openai import AsyncOpenAI

    failure = _StatusError("stream reset")

    async def stream():
        yield failed_response_chunk
        raise failure

    async def create(self, **kwargs):
        return stream()

    with patch("openai.resources.responses.AsyncResponses.create", new=create):
        async with AsyncOpenAI(api_key="test", posthog_client=mock_client) as client:
            events = await client.responses.create(
                model="gpt-4",
                input="hi",
                stream=True,
                posthog_privacy_mode=privacy_mode,
            )
            assert await events.__anext__() is failed_response_chunk
            with pytest.raises(_StatusError, match="stream reset") as raised:
                await events.__anext__()

    assert raised.value is failure
    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == 529
    assert props["$ai_error"] == (
        "_StatusError: 529" if privacy_mode else "stream reset"
    )


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


class _ConnectError(RuntimeError):
    status_code = 401


# A caller's options must reach the error event too. No distinct id, so the
# event is personless and process_person_profile: False joins the caller's.
_CALLER = {"posthog_options": {"cookieless_mode": True}}
_SENT_OPTIONS = {"cookieless_mode": True, "process_person_profile": False}


def _assert_connect_error_event(mock_client):
    props = _props(mock_client)
    assert props["$ai_is_error"] is True
    assert props["$ai_http_status"] == 401
    assert mock_client.capture_ai.call_args[1]["options"] == _SENT_OPTIONS


def test_openai_chat_stream_connect_error(mock_client):
    from posthog.ai.openai import OpenAI

    with patch("openai.resources.chat.completions.Completions.create") as create:
        create.side_effect = _ConnectError("bad key")
        client = OpenAI(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                **_CALLER,
            )

    _assert_connect_error_event(mock_client)


@pytest.mark.asyncio
async def test_openai_chat_stream_connect_error_async(mock_client):
    from posthog.ai.openai import AsyncOpenAI

    async def create(**kwargs):
        raise _ConnectError("bad key")

    with patch(
        "openai.resources.chat.completions.AsyncCompletions.create", side_effect=create
    ):
        client = AsyncOpenAI(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            await client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                **_CALLER,
            )

    _assert_connect_error_event(mock_client)


def test_openai_responses_stream_connect_error(mock_client):
    from posthog.ai.openai import OpenAI

    with patch("openai.resources.responses.Responses.create") as create:
        create.side_effect = _ConnectError("bad key")
        client = OpenAI(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            client.responses.create(model="gpt-4", input="hi", stream=True, **_CALLER)

    _assert_connect_error_event(mock_client)


@pytest.mark.asyncio
async def test_openai_responses_stream_connect_error_async(mock_client):
    from posthog.ai.openai import AsyncOpenAI

    async def create(**kwargs):
        raise _ConnectError("bad key")

    with patch("openai.resources.responses.AsyncResponses.create", side_effect=create):
        client = AsyncOpenAI(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            await client.responses.create(
                model="gpt-4", input="hi", stream=True, **_CALLER
            )

    _assert_connect_error_event(mock_client)


def test_anthropic_stream_connect_error(mock_client):
    from posthog.ai.anthropic import Anthropic

    with patch("anthropic.resources.messages.Messages.create") as create:
        create.side_effect = _ConnectError("bad key")
        client = Anthropic(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            client.messages.create(
                model="claude-3-opus-20240229",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
                stream=True,
                **_CALLER,
            )

    _assert_connect_error_event(mock_client)


@pytest.mark.asyncio
async def test_anthropic_stream_connect_error_async(mock_client):
    from posthog.ai.anthropic import AsyncAnthropic

    async def create(**kwargs):
        raise _ConnectError("bad key")

    with patch("anthropic.resources.messages.AsyncMessages.create", side_effect=create):
        client = AsyncAnthropic(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            await client.messages.create(
                model="claude-3-opus-20240229",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
                stream=True,
                **_CALLER,
            )

    _assert_connect_error_event(mock_client)


def test_gemini_stream_connect_error(mock_client):
    pytest.importorskip("google.genai")
    from google import genai as google_genai

    from posthog.ai.gemini import Client

    with patch.object(google_genai, "Client") as cls:
        inst = MagicMock()
        cls.return_value = inst
        inst.models.generate_content_stream.side_effect = _ConnectError("bad key")
        client = Client(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            client.models.generate_content_stream(
                model="gemini-2.0-flash", contents=["hi"], **_CALLER
            )

    _assert_connect_error_event(mock_client)


@pytest.mark.asyncio
async def test_gemini_stream_connect_error_async(mock_client):
    pytest.importorskip("google.genai")
    from google import genai as google_genai

    from posthog.ai.gemini import AsyncClient

    with patch.object(google_genai, "Client") as cls:
        inst = MagicMock()
        cls.return_value = inst
        inst.aio.models.generate_content_stream = AsyncMock(
            side_effect=_ConnectError("bad key")
        )
        client = AsyncClient(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            await client.models.generate_content_stream(
                model="gemini-2.0-flash", contents=["hi"], **_CALLER
            )

    _assert_connect_error_event(mock_client)


@pytest.mark.parametrize(
    ("cls_name", "code"), [("ClientError", 429), ("ServerError", 503)]
)
def test_capture_streaming_event_records_google_genai_error_status(
    mock_client, cls_name, code
):
    errors = pytest.importorskip("google.genai.errors")
    error = getattr(errors, cls_name)(
        code, {"error": {"code": code, "message": "nope", "status": "ERROR"}}
    )
    capture_streaming_event(mock_client, _event_data(error=error))
    props = _props(mock_client)
    assert props["$ai_http_status"] == code
    assert props["$ai_is_error"] is True


def test_anthropic_stream_connect_error_survives_unformattable_input(mock_client):
    from posthog.ai.anthropic import Anthropic

    with patch("anthropic.resources.messages.Messages.create") as create:
        create.side_effect = _ConnectError("bad request")
        client = Anthropic(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            client.messages.create(
                model="claude-3-opus-20240229",
                messages=[None],
                max_tokens=5,
                stream=True,
            )


@pytest.mark.asyncio
async def test_anthropic_stream_connect_error_survives_unformattable_input_async(
    mock_client,
):
    from posthog.ai.anthropic import AsyncAnthropic

    async def create(**kwargs):
        raise _ConnectError("bad request")

    with patch("anthropic.resources.messages.AsyncMessages.create", side_effect=create):
        client = AsyncAnthropic(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            await client.messages.create(
                model="claude-3-opus-20240229",
                messages=[None],
                max_tokens=5,
                stream=True,
            )


def test_openai_chat_stream_connect_error_survives_unformattable_input(mock_client):
    from posthog.ai.openai import OpenAI

    with patch("openai.resources.chat.completions.Completions.create") as create:
        create.side_effect = _ConnectError("bad request")
        client = OpenAI(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            client.chat.completions.create(model="gpt-4", messages=123, stream=True)


@pytest.mark.asyncio
async def test_openai_chat_stream_connect_error_survives_unformattable_input_async(
    mock_client,
):
    from posthog.ai.openai import AsyncOpenAI

    async def create(**kwargs):
        raise _ConnectError("bad request")

    with patch(
        "openai.resources.chat.completions.AsyncCompletions.create", side_effect=create
    ):
        client = AsyncOpenAI(api_key="k", posthog_client=mock_client)
        with pytest.raises(_ConnectError):
            await client.chat.completions.create(
                model="gpt-4", messages=123, stream=True
            )


def test_anthropic_connect_error_fallback_does_not_log_error_text(mock_client, caplog):
    from posthog.ai.anthropic import Anthropic

    with patch("anthropic.resources.messages.Messages.create") as create:
        create.side_effect = _ConnectError("rejected PRIVATE_CANARY")
        client = Anthropic(api_key="k", posthog_client=mock_client)
        with caplog.at_level("DEBUG", logger="posthog"):
            with pytest.raises(_ConnectError):
                client.messages.create(
                    model="claude-3-opus-20240229",
                    messages=[None],
                    max_tokens=5,
                    stream=True,
                    posthog_privacy_mode=True,
                )

    assert "Failed to capture streaming connect error" in caplog.text
    assert "PRIVATE_CANARY" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
