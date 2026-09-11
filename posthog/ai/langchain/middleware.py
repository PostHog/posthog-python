"""LangChain v1 agent middleware for PostHog AI observability."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Optional, Union
from uuid import UUID, uuid4

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command
from ...client import Client
from .callbacks import (
    CallbackHandler,
    GenerationMetadata,
    SpanMetadata,
    _convert_message_to_dict,
)

log = logging.getLogger("posthog")


class _PostHogMiddlewareState(AgentState[Any], total=False):  # type: ignore[call-arg]
    """Private state used to correlate one agent invocation."""

    _posthog_root_id: Annotated[UUID | None, PrivateStateAttr]
    _posthog_root_start_time: Annotated[float | None, PrivateStateAttr]
    _posthog_root_input: Annotated[dict[str, Any] | None, PrivateStateAttr]


class PostHogMiddleware(AgentMiddleware[_PostHogMiddlewareState, Any, Any]):
    """Capture LangChain v1 agent, model, and tool activity in PostHog.

    Use either this middleware or :class:`CallbackHandler` for an agent invocation,
    not both, to avoid duplicate events. Put this middleware last in the middleware
    list so it records the final model selection and each retry attempt.
    """

    state_schema = _PostHogMiddlewareState

    def __init__(
        self,
        client: Optional[Client] = None,
        *,
        distinct_id: Optional[Union[str, int, UUID]] = None,
        trace_id: Optional[Union[str, int, float, UUID]] = None,
        properties: Optional[dict[str, Any]] = None,
        privacy_mode: bool = False,
        groups: Optional[dict[str, Any]] = None,
    ) -> None:
        self._callback = CallbackHandler(
            client,
            distinct_id=distinct_id,
            trace_id=trace_id,
            properties=properties,
            privacy_mode=privacy_mode,
            groups=groups,
        )

    def before_agent(
        self, state: _PostHogMiddlewareState, runtime: Any
    ) -> dict[str, Any]:
        return self._safely_call(self._start_agent, state) or {}

    async def abefore_agent(
        self, state: _PostHogMiddlewareState, runtime: Any
    ) -> dict[str, Any]:
        return self._safely_call(self._start_agent, state) or {}

    def after_agent(
        self, state: _PostHogMiddlewareState, runtime: Any
    ) -> dict[str, Any]:
        self._safely_call(self._finish_agent, state)
        return self._clear_agent_state()

    async def aafter_agent(
        self, state: _PostHogMiddlewareState, runtime: Any
    ) -> dict[str, Any]:
        await self._asafely_call(self._finish_agent, state)
        return self._clear_agent_state()

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        run_id = self._start_model(request)
        try:
            response = handler(request)
        except BaseException as error:
            self._safely_call(self._finish_model, request.state, run_id, error, False)
            raise
        self._safely_call(self._finish_model, request.state, run_id, response, True)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        run_id = self._start_model(request)
        try:
            response = await handler(request)
        except BaseException as error:
            await self._asafely_call(
                self._finish_model, request.state, run_id, error, False
            )
            raise
        await self._asafely_call(
            self._finish_model, request.state, run_id, response, True
        )
        return response

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        run_id = self._start_tool(request)
        try:
            response = handler(request)
        except BaseException as error:
            self._safely_call(self._finish_tool, request.state, run_id, error, False)
            raise
        output: Any = response
        if isinstance(response, ToolMessage) and response.status == "error":
            output = self._returned_tool_error(response)
        self._safely_call(self._finish_tool, request.state, run_id, output, True)
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        run_id = self._start_tool(request)
        try:
            response = await handler(request)
        except BaseException as error:
            await self._asafely_call(
                self._finish_tool, request.state, run_id, error, False
            )
            raise
        output: Any = response
        if isinstance(response, ToolMessage) and response.status == "error":
            output = self._returned_tool_error(response)
        await self._asafely_call(self._finish_tool, request.state, run_id, output, True)
        return response

    def _start_agent(self, state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "_posthog_root_id": uuid4(),
            "_posthog_root_start_time": time.time(),
            "_posthog_root_input": (
                None if self._privacy_mode_enabled() else _public_state(state)
            ),
        }

    @staticmethod
    def _clear_agent_state() -> dict[str, Any]:
        return {
            "_posthog_root_id": None,
            "_posthog_root_start_time": None,
            "_posthog_root_input": None,
        }

    def _finish_agent(self, state: Mapping[str, Any]) -> None:
        root_id = state.get("_posthog_root_id")
        start_time = state.get("_posthog_root_start_time")
        root_input = state.get("_posthog_root_input")
        if not isinstance(root_id, UUID) or not isinstance(start_time, (int, float)):
            return

        run = SpanMetadata(
            name="agent",
            input=root_input,
            start_time=start_time,
            end_time=time.time(),
        )
        self._safely_call(
            self._callback._capture_trace_or_span,
            self._trace_id(root_id),
            root_id,
            run,
            _public_state(state),
            None,
        )

    def _start_model(self, request: ModelRequest[Any]) -> UUID | None:
        run_id = uuid4()
        try:
            messages: list[BaseMessage] = []
            if request.system_message is not None:
                messages.append(request.system_message)
            messages.extend(request.messages)

            model_parameters = dict(request.model_settings)
            invocation_parameters = request.model._get_invocation_params(
                **request.model_settings
            )
            if isinstance(invocation_parameters, dict):
                model_parameters = {**invocation_parameters, **model_parameters}
            if request.tools:
                model_parameters["tools"] = [
                    self._normalize_tool(tool) for tool in request.tools
                ]

            metadata = request.model._get_ls_params(**request.model_settings)
            self._callback._set_llm_metadata(
                request.model.to_json(),
                run_id,
                [_convert_message_to_dict(message) for message in messages],
                metadata=metadata if isinstance(metadata, dict) else None,
                invocation_params=model_parameters,
            )
        except Exception:
            log.exception("Failed to prepare PostHog LangChain model telemetry")
            self._callback._runs.pop(run_id, None)
            return None
        return run_id

    def _finish_model(
        self,
        state: Mapping[str, Any],
        run_id: UUID | None,
        result: ModelResponse[Any] | BaseException,
        include_parent: bool,
    ) -> None:
        if run_id is None:
            return
        run = self._callback._pop_run_metadata(run_id)
        if not isinstance(run, GenerationMetadata):
            return

        output: LLMResult | BaseException
        if isinstance(result, BaseException):
            output = result
        else:
            generations = [
                ChatGeneration(message=message)
                for message in result.result
                if isinstance(message, AIMessage)
            ]
            if not generations:
                return
            metadata = generations[-1].message.response_metadata
            output = LLMResult(
                generations=[generations],
                llm_output=metadata if isinstance(metadata, dict) else None,
            )

        root_id = _root_id(state)
        self._safely_call(
            self._callback._capture_generation,
            self._trace_id(root_id or run_id),
            run_id,
            run,
            output,
            root_id if include_parent else None,
            include_parent,
        )

    def _start_tool(self, request: ToolCallRequest) -> UUID | None:
        run_id = uuid4()
        try:
            name = str(request.tool_call.get("name") or "tool")
            serialized = request.tool.to_json() if request.tool is not None else None
            self._callback._set_trace_or_span_metadata(
                serialized,
                request.tool_call.get("args"),
                run_id,
                _root_id(request.state),
                name=name,
            )
        except Exception:
            log.exception("Failed to prepare PostHog LangChain tool telemetry")
            self._callback._runs.pop(run_id, None)
            return None
        return run_id

    def _finish_tool(
        self,
        state: Mapping[str, Any],
        run_id: UUID | None,
        output: Any,
        include_parent: bool,
    ) -> None:
        if run_id is None:
            return
        run = self._callback._pop_run_metadata(run_id)
        if not isinstance(run, SpanMetadata) or isinstance(run, GenerationMetadata):
            return
        root_id = _root_id(state)
        self._safely_call(
            self._callback._capture_trace_or_span,
            self._trace_id(root_id or run_id),
            run_id,
            run,
            output,
            root_id if include_parent else None,
            "$ai_span",
        )

    def _trace_id(self, fallback: UUID) -> Any:
        return self._callback._trace_id or fallback

    def _privacy_mode_enabled(self) -> bool:
        return bool(
            self._callback._privacy_mode
            or getattr(self._callback._ph_client, "privacy_mode", False)
        )

    def _returned_tool_error(self, response: ToolMessage) -> RuntimeError:
        message = (
            "Tool returned an error"
            if self._privacy_mode_enabled()
            else str(response.content)
        )
        return RuntimeError(message)

    @staticmethod
    def _normalize_tool(tool: Any) -> dict[str, Any]:
        try:
            return convert_to_openai_tool(tool)
        except Exception:
            if isinstance(tool, dict):
                return tool
            return {"name": getattr(tool, "name", tool.__class__.__name__)}

    @staticmethod
    def _safely_call(function: Callable[..., Any], *args: Any) -> Any:
        try:
            return function(*args)
        except Exception:
            log.exception("Failed to capture PostHog LangChain telemetry")

    async def _asafely_call(self, function: Callable[..., Any], *args: Any) -> Any:
        if getattr(self._callback._ph_client, "sync_mode", False):
            return await asyncio.to_thread(self._safely_call, function, *args)
        return self._safely_call(function, *args)


def _root_id(state: Mapping[str, Any]) -> UUID | None:
    root_id = state.get("_posthog_root_id")
    return root_id if isinstance(root_id, UUID) else None


def _public_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in state.items() if not key.startswith("_posthog_")
    }


__all__ = ["PostHogMiddleware"]
