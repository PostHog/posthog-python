"""Privacy mode across the AI integrations: nothing user-authored may leave the process."""

import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from posthog.ai.utils import (
    _REDACTED_MCP_CREDENTIAL,
    extract_available_tool_calls,
    _summarize_error_for_privacy,
)
from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY

CANARY = "CANARY_7f3a"


class _Parsed(BaseModel):
    answer: int


def _validation_error() -> ValidationError:
    try:
        _Parsed.model_validate_json(json.dumps({"answer": CANARY}))
    except ValidationError as exc:
        return exc
    raise AssertionError("validation unexpectedly passed")


def _httpx_response(status: int) -> Any:
    import httpx

    return httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.example.com/v1/run"),
        headers={"x-request-id": "req_123", "request-id": "req_123"},
    )


def _openai_bad_request() -> Exception:
    import openai

    return openai.BadRequestError(
        f"Error code: 400 - {CANARY}",
        response=_httpx_response(400),
        body={
            "message": f"echoed request: {CANARY}",
            "type": "invalid_request_error",
            "param": "messages[3].content",
            "code": None,
        },
    )


def _anthropic_bad_request() -> Exception:
    import anthropic

    return anthropic.BadRequestError(
        f"echoed request: {CANARY}",
        response=_httpx_response(400),
        body={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": CANARY},
        },
    )


def _gemini_client_error() -> Exception:
    from google.genai import errors

    return errors.ClientError(
        400,
        {"error": {"code": 400, "message": CANARY, "status": "INVALID_ARGUMENT"}},
    )


class _ProviderShapedError(Exception):
    def __init__(self, **fields: Any) -> None:
        super().__init__(CANARY)
        self.__dict__.update(fields)


class _UnreadableError(Exception):
    @property
    def status_code(self) -> int:
        raise RuntimeError(CANARY)


def _chained_error(depth: int) -> BaseException:
    error_types = [KeyError, ValueError, TypeError, LookupError, RuntimeError]
    error: BaseException = error_types[0](CANARY)
    for error_type in error_types[1 : depth + 1]:
        try:
            try:
                raise error
            except BaseException as cause:
                raise error_type(CANARY) from cause
        except BaseException as chained:
            error = chained
    return error


def _implicit_context_error() -> BaseException:
    try:
        try:
            raise KeyError(CANARY)
        except KeyError:
            raise ValueError(CANARY)
    except ValueError as exc:
        return exc


@pytest.mark.parametrize(
    "make_error, expected",
    [
        pytest.param(
            _openai_bad_request,
            "BadRequestError: 400 invalid_request_error param=messages[3].content request_id=req_123",
            id="openai",
        ),
        pytest.param(
            _anthropic_bad_request,
            "BadRequestError: 400 invalid_request_error request_id=req_123",
            id="anthropic",
        ),
        pytest.param(
            _gemini_client_error, "ClientError: 400 INVALID_ARGUMENT", id="gemini"
        ),
        pytest.param(_validation_error, "ValidationError", id="pydantic"),
        pytest.param(
            lambda: _ProviderShapedError(
                code="invalid_request_error",
                type="invalid_request_error",
                param="x",
            ),
            "_ProviderShapedError invalid_request_error param=x",
            id="no-status-and-duplicate-codes",
        ),
        pytest.param(
            lambda: _ProviderShapedError(status_code=503, code="overloaded"),
            "_ProviderShapedError: 503 overloaded",
            id="generic-status-and-code",
        ),
        pytest.param(
            lambda: _ProviderShapedError(
                status_code=700,
                code=f"{CANARY} with spaces",
                type="x" * 65,
                param="messages[0] content",
                request_id="req/123",
            ),
            "_ProviderShapedError",
            id="grammar-rejects-every-field",
        ),
        pytest.param(
            lambda: _ProviderShapedError(status_code=True, status="429"),
            "_ProviderShapedError 429",
            id="bool-and-string-status-are-not-http-status",
        ),
        pytest.param(
            lambda: type("Bad Name", (Exception,), {})(CANARY),
            "Exception",
            id="class-name-rejected",
        ),
        pytest.param(
            lambda: _UnreadableError(CANARY), "_UnreadableError", id="raising-property"
        ),
        pytest.param(
            lambda: _chained_error(2),
            "TypeError (caused by ValueError, KeyError)",
            id="explicit-cause",
        ),
        pytest.param(
            _implicit_context_error,
            "ValueError (caused by KeyError)",
            id="implicit-context",
        ),
        pytest.param(
            lambda: _chained_error(4),
            "RuntimeError (caused by LookupError, TypeError, ValueError)",
            id="cause-chain-capped-at-three",
        ),
    ],
)
def test_summarize_error_for_privacy(
    make_error: Callable[[], BaseException], expected: str
) -> None:
    summary = _summarize_error_for_privacy(make_error())

    assert summary == expected
    assert CANARY not in summary


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_mcp_tool_credentials_are_masked_without_mutating_caller_tools(
    provider: str,
) -> None:
    from openai.types.responses.tool import Mcp

    mcp_dict = {
        "type": "mcp",
        "server_label": "docs",
        "server_url": "https://mcp.example.com",
        "headers": {"Authorization": f"Bearer {CANARY}"},
        "authorization": CANARY,
    }
    mcp_model = Mcp(
        type="mcp",
        server_label="docs",
        server_url="https://mcp.example.com",
        headers={"X-Api-Key": CANARY},
    )
    mcp_object = SimpleNamespace(type="mcp", headers={"X-Api-Key": CANARY})
    function_tool = {"type": "function", "name": "lookup", "headers": "kept"}
    tools = [mcp_dict, mcp_model, mcp_object, function_tool]
    kwargs: Dict[str, Any] = (
        {"config": SimpleNamespace(tools=tools)}
        if provider == "gemini"
        else {"tools": tools}
    )

    captured = extract_available_tool_calls(provider, kwargs)

    assert captured[0] == {
        **mcp_dict,
        "headers": _REDACTED_MCP_CREDENTIAL,
        "authorization": _REDACTED_MCP_CREDENTIAL,
    }
    assert captured[1]["headers"] == _REDACTED_MCP_CREDENTIAL
    assert captured[1]["authorization"] is None
    assert captured[1]["server_url"] == "https://mcp.example.com"
    assert captured[2].headers == _REDACTED_MCP_CREDENTIAL
    assert captured[3] is function_tool
    assert CANARY not in json.dumps(captured, default=str)
    assert tools == [mcp_dict, mcp_model, mcp_object, function_tool]
    assert mcp_dict["headers"] == {"Authorization": f"Bearer {CANARY}"}
    assert mcp_model.headers == {"X-Api-Key": CANARY}
    assert mcp_object.headers == {"X-Api-Key": CANARY}


@pytest.fixture
def private_client() -> Iterator[Client]:
    client = Client(
        FAKE_TEST_API_KEY, privacy_mode=True, enable_exception_autocapture=True
    )
    client._enqueue = MagicMock()  # type: ignore[method-assign]
    yield client
    if client.exception_capture:
        client.exception_capture.close()


def _sent_events(client: Client) -> List[Dict[str, Any]]:
    return [call.args[0] for call in client._enqueue.call_args_list]  # type: ignore[attr-defined]


def _assert_no_canary(client: Client) -> List[Dict[str, Any]]:
    events = _sent_events(client)
    assert events
    assert any(event["properties"].get("$ai_is_error") for event in events)
    assert CANARY not in json.dumps(events, default=str)
    return events


def _function_tool() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": "lookup", "parameters": {"type": "object"}},
    }


@pytest.mark.parametrize(
    "resource, method",
    [("chat_create", "create"), ("chat_parse", "parse"), ("responses", "create")],
)
def test_openai_privacy_mode_sends_no_content(
    private_client: Client, resource: str, method: str
) -> None:
    import openai

    from posthog.ai.openai import OpenAI

    error = _validation_error() if resource == "chat_parse" else _openai_bad_request()
    target = (
        openai.resources.responses.Responses
        if resource == "responses"
        else openai.resources.chat.completions.Completions
    )
    client = OpenAI(posthog_client=private_client, api_key="test-key")
    call = (
        getattr(client.responses, method)
        if resource == "responses"
        else getattr(client.chat.completions, method)
    )
    request: Dict[str, Any] = (
        {
            "input": f"user prompt {CANARY}",
            "instructions": CANARY,
            "tools": [
                {
                    "type": "mcp",
                    "server_label": "docs",
                    "headers": {"Authorization": CANARY},
                }
            ],
        }
        if resource == "responses"
        else {
            "messages": [{"role": "user", "content": f"user prompt {CANARY}"}],
            "tools": [_function_tool()],
        }
    )

    with patch.object(target, method, side_effect=error):
        with pytest.raises(type(error)):
            call(model="gpt-4o", **request)

    _assert_no_canary(private_client)


def test_anthropic_privacy_mode_sends_no_content(private_client: Client) -> None:
    from posthog.ai.anthropic import Anthropic

    client = Anthropic(api_key="test-key", posthog_client=private_client)

    with patch(
        "anthropic.resources.Messages.create", side_effect=_anthropic_bad_request()
    ):
        with pytest.raises(Exception):
            client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=16,
                system=CANARY,
                messages=[{"role": "user", "content": f"user prompt {CANARY}"}],
            )

    events = _assert_no_canary(private_client)
    assert (
        events[0]["properties"]["$ai_error"]
        == "BadRequestError: 400 invalid_request_error request_id=req_123"
    )


@pytest.mark.parametrize("method", ["generate_content", "embed_content"])
def test_gemini_privacy_mode_sends_no_content(
    private_client: Client, method: str
) -> None:
    google_genai = pytest.importorskip("google.genai")
    from posthog.ai.gemini import Client as GeminiClient

    error = (
        _validation_error() if method == "generate_content" else _gemini_client_error()
    )
    client = GeminiClient(api_key="test-key", posthog_client=private_client)

    with patch.object(google_genai.models.Models, method, side_effect=error):
        with pytest.raises(type(error)):
            getattr(client.models, method)(
                model="gemini-2.5-flash",
                contents=[f"user prompt {CANARY}"],
                posthog_privacy_mode=False,
            )

    _assert_no_canary(private_client)


def test_langchain_privacy_mode_sends_no_content_or_exception_event(
    private_client: Client,
) -> None:
    import uuid

    from langchain_core.exceptions import OutputParserException
    from langchain_core.messages import HumanMessage
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import RunnableLambda

    from posthog.ai.langchain import CallbackHandler

    callbacks = CallbackHandler(private_client)

    def failing_parser(_: Any) -> None:
        raise OutputParserException(
            f"Could not parse output: {CANARY}", llm_output=CANARY
        )

    chain = ChatPromptTemplate.from_messages([("user", CANARY)]) | RunnableLambda(
        failing_parser
    )
    with pytest.raises(OutputParserException):
        chain.invoke({}, config={"callbacks": [callbacks]})

    run_id = uuid.uuid4()
    callbacks.on_chat_model_start(
        {"kwargs": {"model_name": "gpt-4o"}},
        [[HumanMessage(content=CANARY)]],
        run_id=run_id,
        invocation_params={
            "tools": [
                _function_tool(),
                {"type": "mcp", "server_label": "docs", "headers": {"k": CANARY}},
            ]
        },
    )
    callbacks.on_llm_error(_openai_bad_request(), run_id=run_id)

    events = _assert_no_canary(private_client)
    assert "$exception" not in {event["event"] for event in events}
    errors = {event["properties"].get("$ai_error") for event in events}
    assert "OutputParserException" in errors


def test_openai_agents_privacy_mode_sends_no_content(private_client: Client) -> None:
    pytest.importorskip("agents")
    from agents.tracing.span_data import (
        FunctionSpanData,
        GenerationSpanData,
        SpeechSpanData,
        TranscriptionSpanData,
    )

    from posthog.ai.openai_agents import PostHogTracingProcessor

    processor = PostHogTracingProcessor(client=private_client, distinct_id="user")
    trace = MagicMock(
        trace_id="trace_1", group_id=None, metadata={"customer_note": CANARY}
    )
    trace.name = "workflow"
    processor.on_trace_start(trace)

    span_datas = [
        GenerationSpanData(
            input=[{"role": "user", "content": CANARY}],
            output=[{"role": "assistant", "content": CANARY}],
            model="gpt-4o",
        ),
        FunctionSpanData(name="lookup", input=CANARY, output=CANARY),
        TranscriptionSpanData(
            input="AAAA",
            input_format="pcm",
            output=CANARY,
            model="gpt-4o-transcribe",
            model_config={"prompt": CANARY, "language": "en"},
        ),
        SpeechSpanData(
            input=CANARY,
            output=None,
            output_format="pcm",
            model="gpt-4o-mini-tts",
            model_config={"instructions": CANARY, "voice": "alloy"},
        ),
    ]
    for index, span_data in enumerate(span_datas):
        span = MagicMock(
            trace_id="trace_1",
            span_id=f"span_{index}",
            parent_id=None,
            started_at=None,
            ended_at=None,
            span_data=span_data,
            error={"message": f"Tool failed: {CANARY}", "data": {"detail": CANARY}},
        )
        processor.on_span_start(span)
        processor.on_span_end(span)
    processor.on_trace_end(trace)

    events = _assert_no_canary(private_client)
    audio_configs = [
        event["properties"]["model_config"]
        for event in events
        if "model_config" in event["properties"]
    ]
    assert audio_configs == [
        {"prompt": None, "language": "en"},
        {"instructions": None, "voice": "alloy"},
    ]


_KEPT_OTEL_ATTRIBUTES: Dict[str, Any] = {
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.usage.input_tokens": 3,
    "gen_ai.tool.definitions": '[{"name": "lookup"}]',
    "llm.request.functions.0.name": "lookup",
    "ai.prompt.tools": ('{"name": "lookup"}',),
    "ai.prompt.toolChoice": '{"type": "auto"}',
}


def _emit_canary_span(provider: Any) -> None:
    from opentelemetry.trace import Status, StatusCode

    tracer = provider.get_tracer("test")
    content_attributes = {
        "gen_ai.input.messages": json.dumps([{"role": "user", "content": CANARY}]),
        "gen_ai.output.messages": CANARY,
        "gen_ai.system_instructions": CANARY,
        "gen_ai.tool.call.arguments": CANARY,
        "gen_ai.tool.call.result": CANARY,
        "gen_ai.prompt.0.content": CANARY,
        "gen_ai.completion.0.content": CANARY,
        "traceloop.entity.input": CANARY,
        "traceloop.entity.output": CANARY,
        "ai.prompt": CANARY,
        "ai.prompt.messages": CANARY,
        "ai.response.text": CANARY,
        "ai.response.object": CANARY,
        "ai.response.toolCalls": CANARY,
        "ai.response.reasoning": CANARY,
        "ai.toolCall.args": CANARY,
        "ai.toolCall.result": CANARY,
        "ai.value": CANARY,
        "ai.values": [CANARY],
        "pydantic_ai.all_messages": CANARY,
        "final_result": CANARY,
        "tool_arguments": CANARY,
        "tool_response": CANARY,
        "anthropic.tool_result.content": CANARY,
        "events": CANARY,
        "error.message": CANARY,
    }
    with tracer.start_as_current_span(
        "gen_ai.chat",
        attributes={
            **_KEPT_OTEL_ATTRIBUTES,
            **content_attributes,
        },
    ) as span:
        span.add_event("gen_ai.content.prompt", {"gen_ai.prompt": CANARY})
        span.record_exception(ValueError(CANARY))
        span.set_status(Status(StatusCode.ERROR, CANARY))


@pytest.mark.parametrize("integration", ["processor", "exporter"])
@pytest.mark.parametrize("privacy_source", ["option", "client", "module"])
def test_otel_privacy_mode_sends_no_content(
    integration: str, privacy_source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    import posthog
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from posthog.ai.otel import PostHogSpanProcessor, PostHogTraceExporter

    monkeypatch.setattr(posthog, "privacy_mode", privacy_source == "module")
    options: Dict[str, Any] = {
        "privacy_mode": privacy_source == "option",
        "client": (
            Client(FAKE_TEST_API_KEY, privacy_mode=True)
            if privacy_source == "client"
            else None
        ),
    }
    exported: List[ReadableSpan] = []
    provider = TracerProvider()
    with (
        patch("posthog.ai.otel.processor.OTLPSpanExporter"),
        patch("posthog.ai.otel.processor.BatchSpanProcessor") as batch,
        patch("posthog.ai.otel.exporter.OTLPSpanExporter") as otlp,
    ):
        batch.return_value.on_end.side_effect = exported.append
        otlp.return_value.export.side_effect = exported.extend
        if integration == "processor":
            provider.add_span_processor(
                PostHogSpanProcessor(api_key="phc_test", **options)
            )
        else:
            provider.add_span_processor(
                SimpleSpanProcessor(PostHogTraceExporter(api_key="phc_test", **options))
            )
        _emit_canary_span(provider)

    [span] = exported
    assert CANARY not in span.to_json()
    assert dict(span.attributes or {}) == _KEPT_OTEL_ATTRIBUTES
    [exception_event] = span.events
    assert exception_event.name == "exception"
    exception_attributes = dict(exception_event.attributes or {})
    assert exception_attributes["exception.type"] == "ValueError"
    assert set(exception_attributes) <= {"exception.type", "exception.escaped"}
    assert span.status.is_ok is False
    assert span.status.description is None
