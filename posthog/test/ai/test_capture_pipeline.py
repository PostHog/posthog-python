import base64

import pytest

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 400).decode()
PLACEHOLDER = "[base64 image redacted]"


class FakePH:
    privacy_mode = False

    def __init__(self):
        self.events = []

    def capture(self, *args, **kwargs):
        self.events.append(kwargs)

    def flush(self):
        pass


def props(fake):
    assert fake.events
    return fake.events[-1]["properties"]


@pytest.fixture
def fake_ph():
    return FakePH()


def test_gemini_dict_image_input_is_redacted(fake_ph, monkeypatch):
    google_genai = pytest.importorskip("google.genai")
    from google.genai import types
    from posthog.ai.gemini import Client

    resp = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text="ok")]),
                finish_reason="STOP",
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1, candidates_token_count=1, total_token_count=2
        ),
    )
    monkeypatch.setattr(
        google_genai.models.Models, "generate_content", lambda self, **kwargs: resp
    )
    client = Client(posthog_client=fake_ph, api_key="k")
    client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            {
                "role": "user",
                "parts": [{"inline_data": {"mime_type": "image/png", "data": PNG_B64}}],
            },
        ],
    )
    block = props(fake_ph)["$ai_input"][0]["content"][0]
    assert block["inline_data"]["data"] == PLACEHOLDER


def test_openai_audio_output_is_redacted(fake_ph, monkeypatch):
    openai_mod = pytest.importorskip("openai")
    from openai.types import CompletionUsage
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.chat.chat_completion_audio import ChatCompletionAudio
    from posthog.ai.openai import OpenAI

    completion = ChatCompletion(
        id="c",
        created=1,
        model="gpt-4o-audio-preview",
        object="chat.completion",
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(
                    role="assistant",
                    content=None,
                    audio=ChatCompletionAudio(
                        id="a", data=PNG_B64 * 3, expires_at=2, transcript="hi"
                    ),
                ),
            )
        ],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )
    monkeypatch.setattr(
        openai_mod.resources.chat.completions.Completions,
        "create",
        lambda self, **kwargs: completion,
    )
    client = OpenAI(posthog_client=fake_ph, api_key="k")
    client.chat.completions.create(
        model="gpt-4o-audio-preview", messages=[{"role": "user", "content": "hi"}]
    )
    audio_block = props(fake_ph)["$ai_output_choices"][0]["content"][0]
    assert audio_block["data"] == "[base64 audio redacted]"
    assert audio_block["transcript"] == "hi"


def test_openai_input_audio_is_redacted(fake_ph, monkeypatch):
    openai_mod = pytest.importorskip("openai")
    from openai.types import CompletionUsage
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from posthog.ai.openai import OpenAI

    completion = ChatCompletion(
        id="c",
        created=1,
        model="gpt-4o-audio-preview",
        object="chat.completion",
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(role="assistant", content="ok"),
            )
        ],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )
    monkeypatch.setattr(
        openai_mod.resources.chat.completions.Completions,
        "create",
        lambda self, **kwargs: completion,
    )
    client = OpenAI(posthog_client=fake_ph, api_key="k")
    client.chat.completions.create(
        model="gpt-4o-audio-preview",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": PNG_B64 * 3, "format": "wav"},
                    },
                ],
            },
        ],
    )
    part = props(fake_ph)["$ai_input"][0]["content"][0]
    assert part["input_audio"]["data"] == "[base64 audio redacted]"


def test_anthropic_nested_tool_result_image_redacted(fake_ph, monkeypatch):
    anthropic_mod = pytest.importorskip("anthropic")
    from anthropic.types import Message, TextBlock, Usage
    from posthog.ai.anthropic import Anthropic

    msg = Message(
        id="m",
        content=[TextBlock(text="ok", type="text")],
        model="claude-sonnet-4-5",
        role="assistant",
        stop_reason="end_turn",
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr(
        anthropic_mod.resources.messages.Messages, "create", lambda self, **kwargs: msg
    )
    client = Anthropic(posthog_client=fake_ph, api_key="k")
    client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=10,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": PNG_B64,
                                },
                            },
                        ],
                    },
                ],
            },
        ],
    )
    nested = props(fake_ph)["$ai_input"][0]["content"][0]["content"][0]
    assert nested["source"]["data"] == PLACEHOLDER


@pytest.mark.asyncio
async def test_async_streaming_chat_message_object_is_formatted(fake_ph, monkeypatch):
    openai_mod = pytest.importorskip("openai")
    from openai.types.chat import ChatCompletionMessage
    from posthog.ai.openai import AsyncOpenAI

    async def mock_create(self, **kwargs):
        async def empty_stream():
            return
            yield  # pragma: no cover

        return empty_stream()

    monkeypatch.setattr(
        openai_mod.resources.chat.completions.AsyncCompletions, "create", mock_create
    )
    client = AsyncOpenAI(posthog_client=fake_ph, api_key="k")
    stream = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[ChatCompletionMessage(role="assistant", content="prior turn")],
        stream=True,
    )
    async for _chunk in stream:
        pass

    assert props(fake_ph)["$ai_input"] == [
        {"role": "assistant", "content": "prior turn"}
    ]


def test_openai_responses_image_generation_call_result_is_redacted(
    fake_ph, monkeypatch
):
    pytest.importorskip("openai")
    from openai.resources.responses import Responses
    from openai.types.responses import Response
    from openai.types.responses.response_output_item import ImageGenerationCall
    from posthog.ai.openai import OpenAI

    response = Response(
        id="r1",
        created_at=1,
        model="gpt-4o",
        object="response",
        output=[
            ImageGenerationCall(
                id="ig_1",
                result=PNG_B64,
                status="completed",
                type="image_generation_call",
            )
        ],
        parallel_tool_calls=True,
        tool_choice="auto",
        tools=[],
    )

    monkeypatch.setattr(Responses, "create", lambda self, **kwargs: response)
    client = OpenAI(posthog_client=fake_ph, api_key="k")
    client.responses.create(
        model="gpt-4o", input=[{"role": "user", "content": "draw a cat"}]
    )

    output_item = props(fake_ph)["$ai_output_choices"][0]["content"][0]
    assert output_item["type"] == "image_generation_call"
    assert output_item["result"] == PLACEHOLDER


def _langchain_image_response():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content=[
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": PNG_B64,
                                },
                            },
                            {"type": "text", "text": "here is the image"},
                        ]
                    )
                )
            ]
        ],
        llm_output={},
    )


def test_langchain_callback_output_choices_image_is_redacted(fake_ph):
    pytest.importorskip("langchain_core")
    from langchain_core.messages import HumanMessage
    from uuid import uuid4

    from posthog.ai.langchain.callbacks import CallbackHandler

    cb = CallbackHandler(client=fake_ph)
    run_id = uuid4()
    cb.on_chat_model_start(
        serialized={},
        messages=[[HumanMessage(content="describe this image")]],
        run_id=run_id,
    )
    cb.on_llm_end(_langchain_image_response(), run_id=run_id)

    content = props(fake_ph)["$ai_output_choices"][0]["content"]
    assert content[0]["source"]["data"] == PLACEHOLDER
    assert content[1] == {"type": "text", "text": "here is the image"}


def test_langchain_callback_output_choices_image_passthrough_when_multimodal_enabled(
    fake_ph,
):
    pytest.importorskip("langchain_core")
    from langchain_core.messages import HumanMessage
    from uuid import uuid4

    from posthog.ai.langchain.callbacks import CallbackHandler

    fake_ph._enable_multimodal_capture = True

    cb = CallbackHandler(client=fake_ph)
    run_id = uuid4()
    cb.on_chat_model_start(
        serialized={},
        messages=[[HumanMessage(content="describe this image")]],
        run_id=run_id,
    )
    cb.on_llm_end(_langchain_image_response(), run_id=run_id)

    content = props(fake_ph)["$ai_output_choices"][0]["content"]
    assert content[0]["source"]["data"] == PNG_B64
    assert content[1] == {"type": "text", "text": "here is the image"}
