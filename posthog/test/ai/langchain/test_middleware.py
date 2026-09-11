import asyncio
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Sequence
from unittest.mock import MagicMock

import pytest
from pydantic import PrivateAttr

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.middleware.types import (
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import posthog.ai.langchain.middleware as middleware_module
from posthog.ai.langchain import CallbackHandler
from posthog.ai.langchain.middleware import PostHogMiddleware


class StubAgentModel(BaseChatModel):
    responses: list[AIMessage]
    _response_index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "posthog-test-agent-model"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        response = self.responses[self._response_index]
        self._response_index += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> Any:
        return {"ls_model_name": "test-model", "ls_provider": "test-provider"}

    def _get_invocation_params(
        self, stop: list[str] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        return {"temperature": 0.25, **kwargs}


class FailingAgentModel(StubAgentModel):
    error: Exception

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise self.error


class RetryOnceAgentModel(StubAgentModel):
    _attempt: int = PrivateAttr(default=0)

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._attempt += 1
        if self._attempt == 1:
            raise RuntimeError("retryable failure")
        return super()._generate(messages, stop, run_manager, **kwargs)


class CustomAgentState(AgentState):
    tenant_id: str
    workflow: str


@tool
def weather(city: str) -> str:
    """Get the weather for a city."""
    return f"Sunny in {city}"


def _client() -> MagicMock:
    client = MagicMock()
    client.privacy_mode = False
    client.sync_mode = False
    client.enable_exception_autocapture = False
    return client


def _events(client: MagicMock) -> list[dict[str, Any]]:
    return [call.kwargs for call in client.capture.call_args_list]


def _event(client: MagicMock, event_name: str) -> dict[str, Any]:
    return next(event for event in _events(client) if event["event"] == event_name)


def _middleware_state(middleware: PostHogMiddleware) -> dict[str, Any]:
    state = {"messages": [HumanMessage(content="Hello")]}
    update = middleware.before_agent(state, None)
    return {**state, **(update or {})}


def _model_request(
    model: BaseChatModel,
    state: dict[str, Any],
    *,
    tools: list[BaseTool | dict[str, Any]] | None = None,
) -> ModelRequest[Any]:
    return ModelRequest(
        model=model,
        messages=[HumanMessage(content="Hello")],
        system_message=SystemMessage(content="Be concise"),
        tools=tools,
        state=state,
        runtime=None,
        model_settings={"max_tokens": 100},
    )


def _tool_request(state: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "id": "weather-call",
            "name": "weather",
            "args": {"city": "London"},
            "type": "tool_call",
        },
        tool=weather,
        state=state,
        runtime=None,
    )


def test_instruments_sync_agent_model_and_tool_loop() -> None:
    client = _client()
    agent = create_agent(
        model=StubAgentModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "weather-call",
                            "name": "weather",
                            "args": {"city": "London"},
                        }
                    ],
                ),
                AIMessage(
                    content="It is sunny",
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "total_tokens": 14,
                    },
                    response_metadata={"finish_reason": "stop"},
                ),
            ]
        ),
        tools=[weather],
        middleware=[
            PostHogMiddleware(
                client,
                distinct_id="user-id",
                trace_id="trace-id",
                properties={"environment": "test"},
                groups={"company": "posthog"},
            )
        ],
    )

    result = agent.invoke({"messages": [HumanMessage(content="Hello")]})

    assert result["messages"][-1].content == "It is sunny"
    assert not any(key.startswith("_posthog_") for key in result)
    events = _events(client)
    assert [event["event"] for event in events] == [
        "$ai_generation",
        "$ai_span",
        "$ai_generation",
        "$ai_trace",
    ]
    trace = events[-1]
    assert trace["distinct_id"] == "user-id"
    assert trace["groups"] == {"company": "posthog"}
    assert all(event["properties"]["$ai_trace_id"] == "trace-id" for event in events)
    assert all(event["properties"]["environment"] == "test" for event in events)
    assert all(
        event["properties"].get("$ai_parent_id") == trace["properties"]["$ai_span_id"]
        for event in events[:-1]
    )

    generations = [event for event in events if event["event"] == "$ai_generation"]
    assert generations[0]["properties"]["$ai_tools"] == [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": "Get the weather for a city.",
                "parameters": {
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "type": "object",
                },
            },
        }
    ]
    assert generations[-1]["properties"]["$ai_model"] == "test-model"
    assert generations[-1]["properties"]["$ai_provider"] == "test-provider"
    assert generations[-1]["properties"]["$ai_input_tokens"] == 10
    assert generations[-1]["properties"]["$ai_output_tokens"] == 4
    assert generations[-1]["properties"]["$ai_stop_reason"] == "stop"


@pytest.mark.asyncio
async def test_instruments_async_agent_and_preserves_custom_state() -> None:
    client = _client()
    agent = create_agent(
        model=StubAgentModel(
            responses=[
                AIMessage(
                    content="Done",
                    usage_metadata={
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "total_tokens": 3,
                    },
                )
            ]
        ),
        tools=[],
        middleware=[PostHogMiddleware(client)],
        state_schema=CustomAgentState,
    )

    result = await agent.ainvoke(
        {
            "messages": [HumanMessage(content="Hello")],
            "tenant_id": "tenant-123",
            "workflow": "support",
        }
    )

    assert result["tenant_id"] == "tenant-123"
    assert result["workflow"] == "support"
    assert not any(key.startswith("_posthog_") for key in result)
    trace = _event(client, "$ai_trace")["properties"]
    assert trace["$ai_input_state"]["tenant_id"] == "tenant-123"
    assert trace["$ai_input_state"]["workflow"] == "support"
    assert trace["$ai_output_state"]["tenant_id"] == "tenant-123"
    assert trace["$ai_output_state"]["workflow"] == "support"
    assert not any(key.startswith("_posthog_") for key in trace["$ai_input_state"])
    assert not any(key.startswith("_posthog_") for key in trace["$ai_output_state"])


@pytest.mark.asyncio
async def test_async_hooks_do_not_capture_on_the_event_loop_thread() -> None:
    client = _client()
    client.sync_mode = True
    event_loop_thread = threading.get_ident()
    capture_threads: list[int] = []
    client.capture.side_effect = lambda **_: capture_threads.append(
        threading.get_ident()
    )
    middleware = PostHogMiddleware(client)
    state = {"messages": [HumanMessage(content="Hello")]}
    state.update(await middleware.abefore_agent(state, None))

    model_request = _model_request(StubAgentModel(responses=[]), state)

    async def model_handler(_: ModelRequest[Any]) -> ModelResponse[Any]:
        return ModelResponse(result=[AIMessage(content="Done")])

    await middleware.awrap_model_call(model_request, model_handler)

    tool_request = _tool_request(state)

    async def tool_handler(_: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="Sunny",
            tool_call_id="weather-call",
            name="weather",
        )

    await middleware.awrap_tool_call(tool_request, tool_handler)
    await middleware.aafter_agent(state, None)

    assert len(capture_threads) == 3
    assert all(thread_id != event_loop_thread for thread_id in capture_threads)


@pytest.mark.parametrize("privacy_source", ["middleware", "client"])
def test_completed_private_state_is_redacted_and_cleared_from_checkpoint(
    privacy_source: str,
) -> None:
    client = _client()
    client.privacy_mode = privacy_source == "client"
    checkpointer = InMemorySaver()
    agent = create_agent(
        model=StubAgentModel(responses=[AIMessage(content="Done")]),
        tools=[],
        middleware=[
            PostHogMiddleware(
                client,
                privacy_mode=privacy_source == "middleware",
            )
        ],
        checkpointer=checkpointer,
    )
    secret = "customer-secret-agent-input"
    config = {"configurable": {"thread_id": f"privacy-{privacy_source}"}}

    result = agent.invoke(
        {"messages": [HumanMessage(content=secret)]},
        config=config,
    )

    assert not any(key.startswith("_posthog_") for key in result)
    checkpoints = list(checkpointer.list(config))
    assert checkpoints
    for checkpoint in checkpoints:
        root_input = checkpoint.checkpoint["channel_values"].get("_posthog_root_input")
        assert secret not in repr(root_input)

    latest = checkpointer.get_tuple(config)
    assert latest is not None
    values = latest.checkpoint["channel_values"]
    for key in (
        "_posthog_root_id",
        "_posthog_root_start_time",
        "_posthog_root_input",
    ):
        assert values.get(key) is None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_model_wrapper_preserves_exact_response_and_error(
    asynchronous: bool,
) -> None:
    client = _client()
    middleware = PostHogMiddleware(client)
    state = _middleware_state(middleware)
    request = _model_request(StubAgentModel(responses=[]), state, tools=[weather])
    response = ModelResponse(
        result=[
            AIMessage(
                content="Done",
                usage_metadata={
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                },
            )
        ]
    )

    if asynchronous:

        async def handler(inner_request: ModelRequest[Any]) -> ModelResponse[Any]:
            assert inner_request is request
            return response

        actual = await middleware.awrap_model_call(request, handler)
    else:

        def handler(inner_request: ModelRequest[Any]) -> ModelResponse[Any]:
            assert inner_request is request
            return response

        actual = middleware.wrap_model_call(request, handler)

    assert actual is response
    generation = _event(client, "$ai_generation")["properties"]
    assert generation["$ai_input"] == [
        {"role": "system", "content": "Be concise"},
        {"role": "user", "content": "Hello"},
    ]
    assert generation["$ai_model_parameters"]["max_tokens"] == 100
    assert generation["$ai_model_parameters"]["temperature"] == 0.25

    client.reset_mock()
    error = RuntimeError("model failed")
    if asynchronous:

        async def failing_handler(
            inner_request: ModelRequest[Any],
        ) -> ModelResponse[Any]:
            raise error

        with pytest.raises(RuntimeError) as raised:
            await middleware.awrap_model_call(request, failing_handler)
    else:

        def failing_handler(inner_request: ModelRequest[Any]) -> ModelResponse[Any]:
            raise error

        with pytest.raises(RuntimeError) as raised:
            middleware.wrap_model_call(request, failing_handler)

    assert raised.value is error
    failed_generation = _event(client, "$ai_generation")["properties"]
    assert failed_generation["$ai_is_error"] is True
    assert "model failed" in failed_generation["$ai_error"]
    assert "$ai_parent_id" not in failed_generation


def test_model_tool_normalization_falls_back_per_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    middleware = PostHogMiddleware(client)
    state = _middleware_state(middleware)
    fallback_tool = {
        "type": "function",
        "function": {
            "name": "fallback_tool",
            "description": "Already serialized",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    request = _model_request(
        StubAgentModel(responses=[]),
        state,
        tools=[weather, fallback_tool],
    )
    real_converter = middleware_module.convert_to_openai_tool

    def flaky_converter(candidate: Any) -> dict[str, Any]:
        if candidate is fallback_tool:
            raise ValueError("unsupported tool")
        return real_converter(candidate)

    monkeypatch.setattr(
        middleware_module,
        "convert_to_openai_tool",
        flaky_converter,
    )
    response = ModelResponse(result=[AIMessage(content="Done")])

    assert middleware.wrap_model_call(request, lambda _: response) is response

    generation = _event(client, "$ai_generation")["properties"]
    assert generation["$ai_tools"][0]["function"]["name"] == "weather"
    assert generation["$ai_tools"][1] is fallback_tool


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_tool_wrapper_captures_returned_and_raised_errors(
    asynchronous: bool,
) -> None:
    client = _client()
    middleware = PostHogMiddleware(client)
    state = _middleware_state(middleware)
    request = _tool_request(state)
    response = ToolMessage(
        content="Invalid arguments",
        tool_call_id="weather-call",
        name="weather",
        status="error",
    )

    if asynchronous:

        async def handler(inner_request: ToolCallRequest) -> ToolMessage:
            assert inner_request is request
            return response

        actual = await middleware.awrap_tool_call(request, handler)
    else:

        def handler(inner_request: ToolCallRequest) -> ToolMessage:
            assert inner_request is request
            return response

        actual = middleware.wrap_tool_call(request, handler)

    assert actual is response
    span = _event(client, "$ai_span")["properties"]
    assert span["$ai_is_error"] is True
    assert "Invalid arguments" in span["$ai_error"]
    assert "$ai_parent_id" in span

    client.reset_mock()
    error = RuntimeError("tool failed")
    if asynchronous:

        async def failing_handler(inner_request: ToolCallRequest) -> ToolMessage:
            raise error

        with pytest.raises(RuntimeError) as raised:
            await middleware.awrap_tool_call(request, failing_handler)
    else:

        def failing_handler(inner_request: ToolCallRequest) -> ToolMessage:
            raise error

        with pytest.raises(RuntimeError) as raised:
            middleware.wrap_tool_call(request, failing_handler)

    assert raised.value is error
    failed_span = _event(client, "$ai_span")["properties"]
    assert failed_span["$ai_is_error"] is True
    assert "tool failed" in failed_span["$ai_error"]
    assert "$ai_parent_id" not in failed_span


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("privacy_source", ["middleware", "client"])
@pytest.mark.asyncio
async def test_returned_tool_error_respects_privacy_mode(
    asynchronous: bool,
    privacy_source: str,
) -> None:
    client = _client()
    client.privacy_mode = privacy_source == "client"
    middleware = PostHogMiddleware(
        client,
        privacy_mode=privacy_source == "middleware",
    )
    state = _middleware_state(middleware)
    request = _tool_request(state)
    secret = "customer-secret-tool-error"
    response = ToolMessage(
        content=secret,
        tool_call_id="weather-call",
        name="weather",
        status="error",
    )

    if asynchronous:

        async def handler(inner_request: ToolCallRequest) -> ToolMessage:
            assert inner_request is request
            return response

        actual = await middleware.awrap_tool_call(request, handler)
    else:

        def handler(inner_request: ToolCallRequest) -> ToolMessage:
            assert inner_request is request
            return response

        actual = middleware.wrap_tool_call(request, handler)

    assert actual is response
    span = _event(client, "$ai_span")["properties"]
    assert span["$ai_is_error"] is True
    assert secret not in str(span.get("$ai_error", ""))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_tool_wrapper_preserves_command_result(asynchronous: bool) -> None:
    client = _client()
    middleware = PostHogMiddleware(client)
    state = _middleware_state(middleware)
    request = ToolCallRequest(
        tool_call={
            "id": "weather-call",
            "name": "weather",
            "args": {"city": "London"},
            "type": "tool_call",
        },
        tool=None,
        state=state,
        runtime=None,
    )
    result = Command(
        update={
            "messages": [
                ToolMessage(
                    content="Sunny",
                    tool_call_id="weather-call",
                    name="weather",
                )
            ]
        }
    )

    if asynchronous:

        async def handler(inner_request: ToolCallRequest) -> Command[Any]:
            assert inner_request is request
            return result

        actual = await middleware.awrap_tool_call(request, handler)
    else:

        def handler(inner_request: ToolCallRequest) -> Command[Any]:
            assert inner_request is request
            return result

        actual = middleware.wrap_tool_call(request, handler)

    assert actual is result
    span = _event(client, "$ai_span")["properties"]
    assert "$ai_is_error" not in span
    assert span["$ai_span_name"] == "weather"
    assert span["$ai_parent_id"]


def test_terminal_model_error_has_no_dangling_root_trace() -> None:
    client = _client()
    error = RuntimeError("model failed")
    agent = create_agent(
        model=FailingAgentModel(responses=[], error=error),
        tools=[],
        middleware=[PostHogMiddleware(client)],
    )

    with pytest.raises(RuntimeError) as raised:
        agent.invoke({"messages": [HumanMessage(content="Hello")]})

    assert raised.value is error
    events = _events(client)
    assert [event["event"] for event in events] == ["$ai_generation"]
    assert "$ai_parent_id" not in events[0]["properties"]


def test_recovered_model_attempt_is_linked_to_completed_root() -> None:
    client = _client()
    middleware = PostHogMiddleware(client)
    state = _middleware_state(middleware)
    request = _model_request(StubAgentModel(responses=[]), state)
    error = RuntimeError("retryable failure")

    def fail(_: ModelRequest[Any]) -> ModelResponse[Any]:
        raise error

    with pytest.raises(RuntimeError) as raised:
        middleware.wrap_model_call(request, fail)
    assert raised.value is error

    response = ModelResponse(result=[AIMessage(content="Recovered")])
    assert middleware.wrap_model_call(request, lambda _: response) is response
    middleware.after_agent(state, None)

    failed_generation, successful_generation, trace = _events(client)
    root_id = trace["properties"]["$ai_span_id"]
    assert failed_generation["event"] == "$ai_generation"
    assert failed_generation["properties"]["$ai_is_error"] is True
    assert "$ai_parent_id" not in failed_generation["properties"]
    assert successful_generation["event"] == "$ai_generation"
    assert successful_generation["properties"]["$ai_parent_id"] == root_id
    assert trace["event"] == "$ai_trace"
    assert all(
        event["properties"]["$ai_trace_id"] == trace["properties"]["$ai_trace_id"]
        for event in (failed_generation, successful_generation, trace)
    )


def test_model_retry_captures_each_attempt_when_posthog_is_innermost() -> None:
    client = _client()
    agent = create_agent(
        model=RetryOnceAgentModel(responses=[AIMessage(content="Recovered")]),
        tools=[],
        middleware=[
            ModelRetryMiddleware(
                max_retries=1,
                retry_on=(RuntimeError,),
                initial_delay=0,
                jitter=False,
            ),
            PostHogMiddleware(client),
        ],
    )

    result = agent.invoke({"messages": [HumanMessage(content="Hello")]})

    assert result["messages"][-1].content == "Recovered"
    failed_generation, successful_generation, trace = _events(client)
    assert failed_generation["event"] == "$ai_generation"
    assert failed_generation["properties"]["$ai_is_error"] is True
    assert "$ai_parent_id" not in failed_generation["properties"]
    assert successful_generation["event"] == "$ai_generation"
    assert (
        successful_generation["properties"]["$ai_parent_id"]
        == trace["properties"]["$ai_span_id"]
    )
    assert trace["event"] == "$ai_trace"
    assert all(
        event["properties"]["$ai_trace_id"] == trace["properties"]["$ai_trace_id"]
        for event in (failed_generation, successful_generation, trace)
    )


def test_concurrent_invocations_keep_unique_roots_with_explicit_trace_id() -> None:
    client = _client()
    middleware = PostHogMiddleware(client, trace_id="shared-trace")
    states = [_middleware_state(middleware), _middleware_state(middleware)]
    requests = [_model_request(StubAgentModel(responses=[]), state) for state in states]

    async def run(
        request: ModelRequest[Any], state: dict[str, Any], content: str
    ) -> None:
        async def handler(inner_request: ModelRequest[Any]) -> ModelResponse[Any]:
            await asyncio.sleep(0)
            return ModelResponse(result=[AIMessage(content=content)])

        await middleware.awrap_model_call(request, handler)
        middleware.after_agent(state, None)

    async def run_concurrently() -> None:
        await asyncio.gather(
            run(requests[0], states[0], "A"),
            run(requests[1], states[1], "B"),
        )

    asyncio.run(run_concurrently())

    events = _events(client)
    traces = [event for event in events if event["event"] == "$ai_trace"]
    generations = [event for event in events if event["event"] == "$ai_generation"]
    assert len(traces) == 2
    assert len(generations) == 2
    assert all(
        event["properties"]["$ai_trace_id"] == "shared-trace" for event in events
    )
    root_ids = {trace["properties"]["$ai_span_id"] for trace in traces}
    assert len(root_ids) == 2
    assert {
        generation["properties"]["$ai_parent_id"] for generation in generations
    } == root_ids


def test_privacy_mode_redacts_agent_and_model_content() -> None:
    client = _client()
    middleware = PostHogMiddleware(client, privacy_mode=True)
    state = _middleware_state(middleware)
    request = _model_request(StubAgentModel(responses=[]), state)

    middleware.wrap_model_call(
        request,
        lambda _: ModelResponse(result=[AIMessage(content="private output")]),
    )
    middleware.after_agent(state, None)

    generation = _event(client, "$ai_generation")["properties"]
    trace = _event(client, "$ai_trace")["properties"]
    assert generation["$ai_input"] is None
    assert generation["$ai_output_choices"] is None
    assert trace["$ai_input_state"] is None
    assert trace["$ai_output_state"] is None


def test_capture_failure_never_changes_agent_result() -> None:
    client = _client()
    client.capture.side_effect = RuntimeError("telemetry unavailable")
    middleware = PostHogMiddleware(client)
    state = _middleware_state(middleware)
    request = _model_request(StubAgentModel(responses=[]), state)
    response = ModelResponse(result=[AIMessage(content="Done")])

    assert middleware.wrap_model_call(request, lambda _: response) is response
    cleanup = middleware.after_agent(state, None)
    assert cleanup is not None
    assert cleanup
    assert all(key.startswith("_posthog_") for key in cleanup)
    assert all(value is None for value in cleanup.values())


def test_root_latency_starts_before_agent_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    middleware = PostHogMiddleware(client)
    timestamps = iter([100.0, 103.0])
    monkeypatch.setattr(time, "time", lambda: next(timestamps))

    state = _middleware_state(middleware)
    middleware.after_agent(state, None)

    trace = _event(client, "$ai_trace")["properties"]
    assert trace["$ai_latency"] == 3.0


def test_callback_integration_remains_available() -> None:
    assert CallbackHandler.__module__ == "posthog.ai.langchain.callbacks"


def test_callback_only_import_does_not_require_langchain_package() -> None:
    script = """
import builtins

original_import = builtins.__import__

def import_without_langchain(name, *args, **kwargs):
    if name == "langchain" or name.startswith("langchain."):
        raise AssertionError(f"unexpected top-level LangChain import: {name}")
    return original_import(name, *args, **kwargs)

builtins.__import__ = import_without_langchain
from posthog.ai.langchain import CallbackHandler
assert CallbackHandler.__module__ == "posthog.ai.langchain.callbacks"
"""

    subprocess.run([sys.executable, "-c", script], check=True)
