import asyncio
import logging
import math
import os
import time
import uuid
from typing import List, Literal, Optional, TypedDict, Union
from unittest.mock import patch

import pytest
from langchain_anthropic.chat_models import ChatAnthropic
from langchain_community.chat_models.fake import FakeMessagesListChatModel
from langchain_community.llms.fake import FakeListLLM, FakeStreamingListLLM
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langchain_openai.chat_models import ChatOpenAI
from langgraph.graph.state import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

from posthog.ai.langchain import CallbackHandler
from posthog.ai.langchain.callbacks import GenerationMetadata, SpanMetadata

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


@pytest.fixture(scope="function")
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        logging.getLogger("posthog").setLevel(logging.DEBUG)
        yield mock_client


def test_parent_capture(mock_client):
    callbacks = CallbackHandler(mock_client)
    parent_run_id = uuid.uuid4()
    run_id = uuid.uuid4()
    callbacks._set_parent_of_run(run_id, parent_run_id)
    assert callbacks._parent_tree == {run_id: parent_run_id}
    callbacks._pop_parent_of_run(run_id)
    assert callbacks._parent_tree == {}
    callbacks._pop_parent_of_run(parent_run_id)  # should not raise


def test_find_root_run(mock_client):
    callbacks = CallbackHandler(mock_client)
    root_run_id = uuid.uuid4()
    parent_run_id = uuid.uuid4()
    run_id = uuid.uuid4()
    callbacks._set_parent_of_run(run_id, parent_run_id)
    callbacks._set_parent_of_run(parent_run_id, root_run_id)
    assert callbacks._find_root_run(run_id) == root_run_id
    new_run_id = uuid.uuid4()
    assert callbacks._find_root_run(new_run_id) == new_run_id


def test_trace_id_generation(mock_client):
    callbacks = CallbackHandler(mock_client)
    run_id = uuid.uuid4()
    with patch("uuid.uuid4", return_value=run_id):
        assert callbacks._get_trace_id(run_id) == run_id
    run_id = uuid.uuid4()
    callbacks = CallbackHandler(mock_client, trace_id=run_id)
    assert callbacks._get_trace_id(uuid.uuid4()) == run_id


def test_metadata_capture(mock_client):
    callbacks = CallbackHandler(mock_client)
    run_id = uuid.uuid4()
    with patch("time.time", return_value=1234567890):
        callbacks._set_llm_metadata(
            {"kwargs": {"openai_api_base": "https://us.posthog.com"}},
            run_id,
            messages=[{"role": "user", "content": "Who won the world series in 2020?"}],
            invocation_params={"temperature": 0.5},
            metadata={"ls_model_name": "hog-mini", "ls_provider": "posthog"},
            name="test",
        )
    expected = GenerationMetadata(
        model="hog-mini",
        input=[{"role": "user", "content": "Who won the world series in 2020?"}],
        start_time=1234567890,
        model_params={"temperature": 0.5},
        provider="posthog",
        base_url="https://us.posthog.com",
        name="test",
        end_time=None,
    )
    assert callbacks._runs[run_id] == expected
    with patch("time.time", return_value=1234567891):
        run = callbacks._pop_run_metadata(run_id)
    expected.end_time = 1234567891
    assert run == expected
    assert callbacks._runs == {}
    callbacks._pop_run_metadata(uuid.uuid4())  # should not raise


def test_run_metadata_capture(mock_client):
    callbacks = CallbackHandler(mock_client)
    run_id = uuid.uuid4()
    with patch("time.time", return_value=1234567890):
        callbacks._set_trace_or_span_metadata(None, 1, run_id)
    expected = SpanMetadata(
        name="trace",
        input=1,
        start_time=1234567890,
        end_time=None,
    )
    assert callbacks._runs[run_id] == expected
    with patch("time.time", return_value=1234567890):
        callbacks._set_trace_or_span_metadata(None, 1, run_id, uuid.uuid4())
    expected = SpanMetadata(
        name="span",
        input=1,
        start_time=1234567890,
        end_time=None,
    )
    assert callbacks._runs[run_id] == expected

    with patch("time.time", return_value=1234567890):
        callbacks._set_trace_or_span_metadata({"name": "test"}, 1, run_id)
    expected = SpanMetadata(
        name="test",
        input=1,
        start_time=1234567890,
        end_time=None,
    )
    assert callbacks._runs[run_id] == expected


@pytest.mark.parametrize("stream", [True, False])
def test_basic_chat_chain(mock_client, stream):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a helpful assistant."),
            ("user", "Who won the world series in 2020?"),
        ]
    )
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="The Los Angeles Dodgers won the World Series in 2020.",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "total_tokens": 20,
                },
            )
        ]
    )
    callbacks = [CallbackHandler(mock_client)]
    chain = prompt | model
    if stream:
        result = [m for m in chain.stream({}, config={"callbacks": callbacks})][0]
    else:
        result = chain.invoke({}, config={"callbacks": callbacks})

    assert result.content == "The Los Angeles Dodgers won the World Series in 2020."
    assert mock_client.capture.call_count == 3

    span_args = mock_client.capture.call_args_list[0][1]
    span_props = span_args["properties"]

    generation_args = mock_client.capture.call_args_list[1][1]
    generation_props = generation_args["properties"]

    trace_args = mock_client.capture.call_args_list[2][1]
    trace_props = trace_args["properties"]

    # Span is first
    assert span_args["event"] == "$ai_span"
    assert span_props["$ai_trace_id"] == generation_props["$ai_trace_id"]
    assert span_props["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert "$ai_span_id" in span_props

    # Generation is second
    assert generation_args["event"] == "$ai_generation"
    assert "distinct_id" in generation_args
    assert "$ai_model" in generation_props
    assert "$ai_provider" in generation_props
    assert generation_props["$ai_input"] == [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Who won the world series in 2020?"},
    ]
    assert generation_props["$ai_output_choices"] == [
        {
            "role": "assistant",
            "content": "The Los Angeles Dodgers won the World Series in 2020.",
        }
    ]
    assert generation_props["$ai_input_tokens"] == 10
    assert generation_props["$ai_output_tokens"] == 10
    assert generation_props["$ai_http_status"] == 200
    assert isinstance(generation_props["$ai_latency"], float)
    assert "$ai_span_id" in generation_props
    assert generation_props["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert generation_props["$ai_trace_id"] == trace_props["$ai_trace_id"]
    assert generation_props["$ai_span_name"] == "FakeMessagesListChatModel"

    # Trace is last
    assert trace_args["event"] == "$ai_trace"
    assert "$ai_trace_id" in trace_props
    assert "$ai_parent_id" not in trace_props


@pytest.mark.parametrize("stream", [True, False])
async def test_async_basic_chat_chain(mock_client, stream):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a helpful assistant."),
            ("user", "Who won the world series in 2020?"),
        ]
    )
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="The Los Angeles Dodgers won the World Series in 2020.",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "total_tokens": 20,
                },
            )
        ]
    )
    callbacks = [CallbackHandler(mock_client)]
    chain = prompt | model
    if stream:
        result = [m async for m in chain.astream({}, config={"callbacks": callbacks})][0]
    else:
        result = await chain.ainvoke({}, config={"callbacks": callbacks})
    assert result.content == "The Los Angeles Dodgers won the World Series in 2020."
    assert mock_client.capture.call_count == 3

    span_args = mock_client.capture.call_args_list[0][1]
    span_props = span_args["properties"]
    generation_args = mock_client.capture.call_args_list[1][1]
    generation_props = generation_args["properties"]
    trace_args = mock_client.capture.call_args_list[2][1]
    trace_props = trace_args["properties"]

    # Span is first
    assert span_args["event"] == "$ai_span"
    assert span_props["$ai_trace_id"] == generation_props["$ai_trace_id"]
    assert span_props["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert "$ai_span_id" in span_props

    # Generation is second
    assert generation_args["event"] == "$ai_generation"
    assert "distinct_id" in generation_args
    assert "$ai_model" in generation_props
    assert "$ai_provider" in generation_props
    assert generation_props["$ai_input"] == [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Who won the world series in 2020?"},
    ]
    assert generation_props["$ai_output_choices"] == [
        {
            "role": "assistant",
            "content": "The Los Angeles Dodgers won the World Series in 2020.",
        }
    ]
    assert generation_props["$ai_input_tokens"] == 10
    assert generation_props["$ai_output_tokens"] == 10
    assert generation_props["$ai_http_status"] == 200
    assert isinstance(generation_props["$ai_latency"], float)
    assert "$ai_span_id" in generation_props
    assert generation_props["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert generation_props["$ai_trace_id"] == trace_props["$ai_trace_id"]

    # Trace is last
    assert trace_args["event"] == "$ai_trace"
    assert "distinct_id" in generation_args
    assert trace_props["$ai_trace_id"] == generation_props["$ai_trace_id"]
    assert "$ai_parent_id" not in trace_props


@pytest.mark.parametrize(
    "Model,stream",
    [
        (FakeListLLM, True),
        (FakeListLLM, False),
        (FakeStreamingListLLM, True),
        (FakeStreamingListLLM, False),
    ],
)
def test_basic_llm_chain(mock_client, Model, stream):
    model = Model(responses=["The Los Angeles Dodgers won the World Series in 2020."])
    callbacks: List[CallbackHandler] = [CallbackHandler(mock_client)]

    if stream:
        result = "".join(
            [m for m in model.stream("Who won the world series in 2020?", config={"callbacks": callbacks})]
        )
    else:
        result = model.invoke("Who won the world series in 2020?", config={"callbacks": callbacks})
    assert result == "The Los Angeles Dodgers won the World Series in 2020."

    assert mock_client.capture.call_count == 1
    args = mock_client.capture.call_args_list[0][1]
    props = args["properties"]

    assert args["event"] == "$ai_generation"
    assert "distinct_id" in args
    assert "$ai_model" in props
    assert "$ai_provider" in props
    assert props["$ai_input"] == ["Who won the world series in 2020?"]
    assert props["$ai_output_choices"] == ["The Los Angeles Dodgers won the World Series in 2020."]
    assert props["$ai_http_status"] == 200
    assert props["$ai_trace_id"] is not None
    assert isinstance(props["$ai_latency"], float)


@pytest.mark.parametrize(
    "Model,stream",
    [
        (FakeListLLM, True),
        (FakeListLLM, False),
        (FakeStreamingListLLM, True),
        (FakeStreamingListLLM, False),
    ],
)
async def test_async_basic_llm_chain(mock_client, Model, stream):
    model = Model(responses=["The Los Angeles Dodgers won the World Series in 2020."])
    callbacks: List[CallbackHandler] = [CallbackHandler(mock_client)]

    if stream:
        result = "".join(
            [m async for m in model.astream("Who won the world series in 2020?", config={"callbacks": callbacks})]
        )
    else:
        result = await model.ainvoke("Who won the world series in 2020?", config={"callbacks": callbacks})
    assert result == "The Los Angeles Dodgers won the World Series in 2020."

    assert mock_client.capture.call_count == 1
    args = mock_client.capture.call_args_list[0][1]
    props = args["properties"]

    assert args["event"] == "$ai_generation"
    assert "distinct_id" in args
    assert "$ai_model" in props
    assert "$ai_provider" in props
    assert props["$ai_input"] == ["Who won the world series in 2020?"]
    assert props["$ai_output_choices"] == ["The Los Angeles Dodgers won the World Series in 2020."]
    assert props["$ai_http_status"] == 200
    assert props["$ai_trace_id"] is not None
    assert isinstance(props["$ai_latency"], float)


def test_trace_id_and_inputs_for_multiple_chains(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("user", "Foo {var}"),
        ]
    )
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    callbacks = [CallbackHandler(mock_client)]
    chain = prompt | model | RunnableLambda(lambda x: [x]) | model
    result = chain.invoke({"var": "bar"}, config={"callbacks": callbacks})

    assert result.content == "Bar"
    # span, generation, span, generation, trace
    assert mock_client.capture.call_count == 5

    first_span_args = mock_client.capture.call_args_list[0][1]
    first_span_props = first_span_args["properties"]

    first_generation_args = mock_client.capture.call_args_list[1][1]
    first_generation_props = first_generation_args["properties"]

    second_span_args = mock_client.capture.call_args_list[2][1]
    second_span_props = second_span_args["properties"]

    second_generation_args = mock_client.capture.call_args_list[3][1]
    second_generation_props = second_generation_args["properties"]

    trace_args = mock_client.capture.call_args_list[4][1]
    trace_props = trace_args["properties"]

    # Prompt span
    assert first_span_args["event"] == "$ai_span"
    assert first_span_props["$ai_input_state"] == {"var": "bar"}
    assert first_span_props["$ai_trace_id"] == trace_props["$ai_trace_id"]
    assert first_span_props["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert "$ai_span_id" in first_span_props
    assert first_span_props["$ai_output_state"] == ChatPromptTemplate(
        messages=[HumanMessage(content="Foo bar")]
    ).invoke({})

    # first model
    assert first_generation_args["event"] == "$ai_generation"
    assert "distinct_id" in first_generation_args
    assert "$ai_model" in first_generation_props
    assert "$ai_provider" in first_generation_props
    assert first_generation_props["$ai_input"] == [{"role": "user", "content": "Foo bar"}]
    assert first_generation_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert first_generation_props["$ai_http_status"] == 200
    assert isinstance(first_generation_props["$ai_latency"], float)
    assert "$ai_span_id" in first_generation_props
    assert first_generation_props["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert first_generation_props["$ai_trace_id"] == trace_props["$ai_trace_id"]

    # lambda span
    assert second_span_args["event"] == "$ai_span"
    assert second_span_props["$ai_input_state"].content == "Bar"
    assert second_span_props["$ai_trace_id"] == trace_props["$ai_trace_id"]
    assert second_span_props["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert "$ai_span_id" in second_span_props
    assert second_span_props["$ai_output_state"][0].content == "Bar"

    # second model
    assert second_generation_args["event"] == "$ai_generation"
    assert "distinct_id" in second_generation_args
    assert "$ai_model" in second_generation_props
    assert "$ai_provider" in second_generation_props
    assert second_generation_props["$ai_input"] == [{"role": "assistant", "content": "Bar"}]
    assert second_generation_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert second_generation_props["$ai_http_status"] == 200
    assert second_generation_props["$ai_trace_id"] is not None
    assert isinstance(second_generation_props["$ai_latency"], float)

    # trace
    assert trace_args["event"] == "$ai_trace"
    assert "distinct_id" in trace_args
    assert trace_props["$ai_input_state"] == {"var": "bar"}
    assert isinstance(trace_props["$ai_output_state"], AIMessage)
    assert trace_props["$ai_output_state"].content == "Bar"
    assert trace_props["$ai_trace_id"] is not None
    assert trace_props["$ai_span_name"] == "RunnableSequence"


def test_personless_mode(mock_client):
    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    chain = prompt | FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    chain.invoke({}, config={"callbacks": [CallbackHandler(mock_client)]})
    assert mock_client.capture.call_count == 3
    span_args = mock_client.capture.call_args_list[0][1]
    generation_args = mock_client.capture.call_args_list[1][1]
    trace_args = mock_client.capture.call_args_list[2][1]

    # span
    assert span_args["event"] == "$ai_span"
    assert span_args["properties"]["$process_person_profile"] is False
    # generation
    assert generation_args["event"] == "$ai_generation"
    assert generation_args["properties"]["$process_person_profile"] is False
    # trace
    assert trace_args["event"] == "$ai_trace"
    assert trace_args["properties"]["$process_person_profile"] is False

    id = uuid.uuid4()
    chain.invoke({}, config={"callbacks": [CallbackHandler(mock_client, distinct_id=id)]})
    assert mock_client.capture.call_count == 6
    span_args = mock_client.capture.call_args_list[3][1]
    generation_args = mock_client.capture.call_args_list[4][1]
    trace_args = mock_client.capture.call_args_list[5][1]

    # span
    assert "$process_person_profile" not in span_args["properties"]
    assert span_args["distinct_id"] == id
    # generation
    assert "$process_person_profile" not in generation_args["properties"]
    assert generation_args["distinct_id"] == id
    # trace
    assert "$process_person_profile" not in trace_args["properties"]
    assert trace_args["distinct_id"] == id


def test_personless_mode_exception(mock_client):
    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    chain = prompt | ChatOpenAI(api_key="test", model="gpt-4o-mini")
    callbacks = CallbackHandler(mock_client)
    with pytest.raises(Exception):
        chain.invoke({}, config={"callbacks": [callbacks]})
    assert mock_client.capture.call_count == 3
    span_args = mock_client.capture.call_args_list[0][1]
    generation_args = mock_client.capture.call_args_list[1][1]
    trace_args = mock_client.capture.call_args_list[2][1]

    # span
    assert span_args["event"] == "$ai_span"
    assert span_args["properties"]["$process_person_profile"] is False
    # generation
    assert generation_args["event"] == "$ai_generation"
    assert generation_args["properties"]["$process_person_profile"] is False
    # trace
    assert trace_args["event"] == "$ai_trace"
    assert trace_args["properties"]["$process_person_profile"] is False

    id = uuid.uuid4()
    with pytest.raises(Exception):
        chain.invoke({}, config={"callbacks": [CallbackHandler(mock_client, distinct_id=id)]})
    assert mock_client.capture.call_count == 6
    span_args = mock_client.capture.call_args_list[3][1]
    generation_args = mock_client.capture.call_args_list[4][1]
    trace_args = mock_client.capture.call_args_list[5][1]

    # span
    assert span_args["event"] == "$ai_span"
    assert "$process_person_profile" not in span_args["properties"]
    assert span_args["distinct_id"] == id

    # generation
    assert generation_args["event"] == "$ai_generation"
    assert "$process_person_profile" not in generation_args["properties"]
    assert generation_args["distinct_id"] == id

    # trace
    assert trace_args["event"] == "$ai_trace"
    assert "$process_person_profile" not in trace_args["properties"]
    assert trace_args["distinct_id"] == id


def test_metadata(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("user", "Foo"),
        ]
    )
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    callbacks = [
        CallbackHandler(
            mock_client,
            trace_id="test-trace-id",
            distinct_id="test_id",
            properties={"foo": "bar"},
        )
    ]
    chain = prompt | model
    result = chain.invoke({"plan": None}, config={"callbacks": callbacks})

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 3

    span_call_args = mock_client.capture.call_args_list[0][1]
    span_call_props = span_call_args["properties"]
    assert span_call_args["distinct_id"] == "test_id"
    assert span_call_args["event"] == "$ai_span"
    assert span_call_props["$ai_trace_id"] == "test-trace-id"
    assert span_call_props["foo"] == "bar"
    assert "$ai_parent_id" in span_call_props
    assert "$ai_span_id" in span_call_props

    generation_call_args = mock_client.capture.call_args_list[1][1]
    generation_call_props = generation_call_args["properties"]
    assert generation_call_args["distinct_id"] == "test_id"
    assert generation_call_args["event"] == "$ai_generation"
    assert generation_call_props["$ai_trace_id"] == "test-trace-id"
    assert generation_call_props["foo"] == "bar"
    assert generation_call_props["$ai_input"] == [{"role": "user", "content": "Foo"}]
    assert generation_call_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert generation_call_props["$ai_http_status"] == 200
    assert isinstance(generation_call_props["$ai_latency"], float)

    trace_call_args = mock_client.capture.call_args_list[2][1]
    trace_call_props = trace_call_args["properties"]
    assert trace_call_args["distinct_id"] == "test_id"
    assert trace_call_args["event"] == "$ai_trace"
    assert trace_call_props["$ai_trace_id"] == "test-trace-id"
    assert trace_call_props["$ai_span_name"] == "RunnableSequence"
    assert trace_call_props["foo"] == "bar"
    assert trace_call_props["$ai_input_state"] == {"plan": None}
    assert isinstance(trace_call_props["$ai_output_state"], AIMessage)
    assert trace_call_props["$ai_output_state"].content == "Bar"


class FakeGraphState(TypedDict):
    messages: List[Union[HumanMessage, AIMessage]]
    xyz: Optional[str]


def test_graph_state(mock_client):
    config = {"callbacks": [CallbackHandler(mock_client)]}

    graph = StateGraph(FakeGraphState)
    graph.add_node(
        "fake_plain",
        lambda state: {
            "messages": [
                *state["messages"],
                AIMessage(content="Let's explore bar."),
            ],
            "xyz": "abc",
        },
    )
    intermediate_chain = ChatPromptTemplate.from_messages(
        [("user", "Question: What's a bar?")]
    ) | FakeMessagesListChatModel(
        responses=[
            AIMessage(content="It's a type of greeble."),
        ]
    )
    graph.add_node(
        "fake_llm",
        lambda state: {
            "messages": [
                *state["messages"],
                intermediate_chain.invoke(state),
            ],
            "xyz": state["xyz"],
        },
    )
    graph.add_edge(START, "fake_plain")
    graph.add_edge("fake_plain", "fake_llm")
    graph.add_edge("fake_llm", END)

    initial_state = {"messages": [HumanMessage(content="What's a bar?")], "xyz": None}
    result = graph.compile().invoke(initial_state, config=config)

    assert len(result["messages"]) == 3
    assert isinstance(result["messages"][0], HumanMessage)
    assert result["messages"][0].content == "What's a bar?"
    assert isinstance(result["messages"][1], AIMessage)
    assert result["messages"][1].content == "Let's explore bar."
    assert isinstance(result["messages"][2], AIMessage)
    assert result["messages"][2].content == "It's a type of greeble."

    assert mock_client.capture.call_count == 11
    calls = [call[1] for call in mock_client.capture.call_args_list]

    trace_args = calls[10]
    trace_props = calls[10]["properties"]

    # Events are captured in the reverse order.
    # Check all trace_ids
    for call in calls:
        assert call["properties"]["$ai_trace_id"] == trace_props["$ai_trace_id"]

    # First span, write the state
    assert calls[0]["event"] == "$ai_span"
    assert calls[0]["properties"]["$ai_parent_id"] == calls[2]["properties"]["$ai_span_id"]
    assert "$ai_span_id" in calls[0]["properties"]
    assert calls[0]["properties"]["$ai_input_state"] == initial_state
    assert calls[0]["properties"]["$ai_output_state"] == initial_state

    # Second span, set the START node
    assert calls[1]["event"] == "$ai_span"
    assert calls[1]["properties"]["$ai_parent_id"] == calls[2]["properties"]["$ai_span_id"]
    assert "$ai_span_id" in calls[1]["properties"]
    assert calls[1]["properties"]["$ai_input_state"] == initial_state
    assert calls[1]["properties"]["$ai_output_state"] == initial_state

    # Third span, finish initialization
    assert calls[2]["event"] == "$ai_span"
    assert "$ai_span_id" in calls[2]["properties"]
    assert calls[2]["properties"]["$ai_span_name"] == START
    assert calls[2]["properties"]["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert calls[2]["properties"]["$ai_input_state"] == initial_state
    assert calls[2]["properties"]["$ai_output_state"] == initial_state

    # Fourth span, save the value of fake_plain during its execution
    second_state = {
        "messages": [HumanMessage(content="What's a bar?"), AIMessage(content="Let's explore bar.")],
        "xyz": "abc",
    }
    assert calls[3]["event"] == "$ai_span"
    assert calls[3]["properties"]["$ai_parent_id"] == calls[4]["properties"]["$ai_span_id"]
    assert "$ai_span_id" in calls[3]["properties"]
    assert calls[3]["properties"]["$ai_input_state"] == second_state
    assert calls[3]["properties"]["$ai_output_state"] == second_state

    # Fifth span, run the fake_plain node
    assert calls[4]["event"] == "$ai_span"
    assert "$ai_span_id" in calls[4]["properties"]
    assert calls[4]["properties"]["$ai_span_name"] == "fake_plain"
    assert calls[4]["properties"]["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert calls[4]["properties"]["$ai_input_state"] == initial_state
    assert calls[4]["properties"]["$ai_output_state"] == second_state

    # Sixth span, chat prompt template
    assert calls[5]["event"] == "$ai_span"
    assert calls[5]["properties"]["$ai_parent_id"] == calls[7]["properties"]["$ai_span_id"]
    assert "$ai_span_id" in calls[5]["properties"]
    assert calls[5]["properties"]["$ai_span_name"] == "ChatPromptTemplate"

    # 7. Generation, fake_llm
    assert calls[6]["event"] == "$ai_generation"
    assert calls[6]["properties"]["$ai_parent_id"] == calls[7]["properties"]["$ai_span_id"]
    assert "$ai_span_id" in calls[6]["properties"]
    assert calls[6]["properties"]["$ai_span_name"] == "FakeMessagesListChatModel"

    # 8. Span, RunnableSequence
    assert calls[7]["event"] == "$ai_span"
    assert calls[7]["properties"]["$ai_parent_id"] == calls[9]["properties"]["$ai_span_id"]
    assert "$ai_span_id" in calls[7]["properties"]
    assert calls[7]["properties"]["$ai_span_name"] == "RunnableSequence"

    # 9. Span, fake_llm write
    assert calls[8]["event"] == "$ai_span"
    assert calls[8]["properties"]["$ai_parent_id"] == calls[9]["properties"]["$ai_span_id"]
    assert "$ai_span_id" in calls[8]["properties"]

    # 10. Span, fake_llm node
    assert calls[9]["event"] == "$ai_span"
    assert calls[9]["properties"]["$ai_parent_id"] == trace_props["$ai_trace_id"]
    assert "$ai_span_id" in calls[9]["properties"]
    assert calls[9]["properties"]["$ai_span_name"] == "fake_llm"

    # 11. Trace
    assert trace_args["event"] == "$ai_trace"
    assert trace_props["$ai_span_name"] == "LangGraph"

    assert len(trace_props["$ai_input_state"]["messages"]) == 1
    assert isinstance(trace_props["$ai_input_state"]["messages"][0], HumanMessage)
    assert trace_props["$ai_input_state"]["messages"][0].content == "What's a bar?"
    assert trace_props["$ai_input_state"]["messages"][0].type == "human"
    assert trace_props["$ai_input_state"]["xyz"] is None
    assert len(trace_props["$ai_output_state"]["messages"]) == 3

    assert isinstance(trace_props["$ai_output_state"]["messages"][0], HumanMessage)
    assert trace_props["$ai_output_state"]["messages"][0].content == "What's a bar?"
    assert isinstance(trace_props["$ai_output_state"]["messages"][1], AIMessage)
    assert trace_props["$ai_output_state"]["messages"][1].content == "Let's explore bar."
    assert isinstance(trace_props["$ai_output_state"]["messages"][2], AIMessage)
    assert trace_props["$ai_output_state"]["messages"][2].content == "It's a type of greeble."
    assert trace_args["properties"]["$ai_output_state"]["xyz"] == "abc"


def test_callbacks_logic(mock_client):
    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    callbacks = CallbackHandler(
        mock_client,
        trace_id="test-trace-id",
        distinct_id="test_id",
        properties={"foo": "bar"},
    )
    chain = prompt | model

    chain.invoke({}, config={"callbacks": [callbacks]})
    assert callbacks._runs == {}
    assert callbacks._parent_tree == {}

    def assert_intermediary_run(m):
        assert len(callbacks._runs) != 0
        run = next(iter(callbacks._runs.values()))
        assert run.name == "RunnableSequence"
        assert run.input == {}
        assert run.start_time is not None
        assert len(callbacks._parent_tree.items()) == 1
        return [m]

    (chain | RunnableLambda(assert_intermediary_run) | model).invoke({}, config={"callbacks": [callbacks]})
    assert callbacks._runs == {}
    assert callbacks._parent_tree == {}


def test_exception_in_chain(mock_client):
    def runnable(_):
        raise ValueError("test")

    callbacks = CallbackHandler(mock_client)
    with pytest.raises(ValueError):
        RunnableLambda(runnable).invoke({}, config={"callbacks": [callbacks]})

    assert callbacks._runs == {}
    assert callbacks._parent_tree == {}
    assert mock_client.capture.call_count == 1
    trace_call_args = mock_client.capture.call_args_list[0][1]
    assert trace_call_args["event"] == "$ai_trace"
    assert trace_call_args["properties"]["$ai_span_name"] == "runnable"


def test_openai_error(mock_client):
    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    chain = prompt | ChatOpenAI(api_key="test", model="gpt-4o-mini")
    callbacks = CallbackHandler(mock_client)

    # 401
    with pytest.raises(Exception):
        chain.invoke({}, config={"callbacks": [callbacks]})

    assert callbacks._runs == {}
    assert callbacks._parent_tree == {}
    assert mock_client.capture.call_count == 3
    generation_args = mock_client.capture.call_args_list[1][1]
    props = generation_args["properties"]
    assert props["$ai_http_status"] == 401
    assert props["$ai_input"] == [{"role": "user", "content": "Foo"}]
    assert "$ai_output_choices" not in props


@pytest.mark.skipif(not OPENAI_API_KEY, reason="OpenAI API key not set")
def test_openai_chain(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", 'You must always answer with "Bar".'),
            ("user", "Foo"),
        ]
    )
    chain = prompt | ChatOpenAI(
        api_key=OPENAI_API_KEY,
        model="gpt-4o-mini",
        temperature=0,
        max_tokens=1,
    )
    callbacks = CallbackHandler(
        mock_client,
        trace_id="test-trace-id",
        distinct_id="test_id",
        properties={"foo": "bar"},
    )
    start_time = time.time()
    result = chain.invoke({}, config={"callbacks": [callbacks]})
    approximate_latency = math.floor(time.time() - start_time)

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 3

    gen_args = mock_client.capture.call_args_list[1][1]
    gen_props = gen_args["properties"]
    assert gen_args["event"] == "$ai_generation"
    assert gen_props["$ai_trace_id"] == "test-trace-id"
    assert gen_props["$ai_provider"] == "openai"
    assert gen_props["$ai_model"] == "gpt-4o-mini"
    assert gen_props["foo"] == "bar"

    # langchain-openai for langchain v3
    if "max_completion_tokens" in gen_props["$ai_model_parameters"]:
        assert gen_props["$ai_model_parameters"] == {
            "temperature": 0.0,
            "max_completion_tokens": 1,
            "stream": False,
        }
    else:
        assert gen_props["$ai_model_parameters"] == {
            "temperature": 0.0,
            "max_tokens": 1,
            "n": 1,
            "stream": False,
        }
    assert gen_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert gen_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar", "refusal": None}]
    assert gen_props["$ai_http_status"] == 200
    assert isinstance(gen_props["$ai_latency"], float)
    assert min(approximate_latency - 1, 0) <= math.floor(gen_props["$ai_latency"]) <= approximate_latency
    assert gen_props["$ai_input_tokens"] == 20
    assert gen_props["$ai_output_tokens"] == 1


@pytest.mark.skipif(not OPENAI_API_KEY, reason="OpenAI API key not set")
def test_openai_captures_multiple_generations(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", 'You must always answer with "Bar".'),
            ("user", "Foo"),
        ]
    )
    chain = prompt | ChatOpenAI(
        api_key=OPENAI_API_KEY,
        model="gpt-4o-mini",
        temperature=0,
        max_tokens=1,
        n=2,
    )
    callbacks = CallbackHandler(mock_client)
    result = chain.invoke({}, config={"callbacks": [callbacks]})

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 3

    gen_args = mock_client.capture.call_args_list[1][1]
    gen_props = gen_args["properties"]
    trace_args = mock_client.capture.call_args_list[2][1]
    trace_props = trace_args["properties"]

    assert gen_args["event"] == "$ai_generation"
    assert gen_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert gen_props["$ai_output_choices"] == [
        {"role": "assistant", "content": "Bar", "refusal": None},
        {
            "role": "assistant",
            "content": "Bar",
        },
    ]

    # langchain-openai for langchain v3
    if "max_completion_tokens" in gen_props["$ai_model_parameters"]:
        assert gen_props["$ai_model_parameters"] == {
            "temperature": 0.0,
            "max_completion_tokens": 1,
            "stream": False,
            "n": 2,
        }
    else:
        assert gen_props["$ai_model_parameters"] == {
            "temperature": 0.0,
            "max_tokens": 1,
            "stream": False,
            "n": 2,
        }
    assert gen_props["$ai_http_status"] == 200

    assert trace_args["event"] == "$ai_trace"
    assert trace_props["$ai_input_state"] == {}
    assert isinstance(trace_props["$ai_output_state"], AIMessage)


@pytest.mark.skipif(not OPENAI_API_KEY, reason="OpenAI API key not set")
def test_openai_streaming(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", 'You must always answer with "Bar".'),
            ("user", "Foo"),
        ]
    )
    chain = prompt | ChatOpenAI(
        api_key=OPENAI_API_KEY,
        model="gpt-4o-mini",
        temperature=0,
        max_tokens=1,
        stream=True,
        stream_usage=True,
    )
    callbacks = CallbackHandler(mock_client)
    result = [m for m in chain.stream({}, config={"callbacks": [callbacks]})]
    result = sum(result[1:], result[0])

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 3

    gen_args = mock_client.capture.call_args_list[1][1]
    gen_props = gen_args["properties"]
    trace_args = mock_client.capture.call_args_list[2][1]
    trace_props = trace_args["properties"]

    assert gen_args["event"] == "$ai_generation"
    assert gen_props["$ai_model_parameters"]["stream"]
    assert gen_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert gen_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert gen_props["$ai_http_status"] == 200
    assert gen_props["$ai_input_tokens"] == 20
    assert gen_props["$ai_output_tokens"] == 1

    assert trace_args["event"] == "$ai_trace"
    assert trace_props["$ai_input_state"] == {"input": ""}
    assert isinstance(trace_props["$ai_output_state"], AIMessage)


@pytest.mark.skipif(not OPENAI_API_KEY, reason="OpenAI API key not set")
async def test_async_openai_streaming(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", 'You must always answer with "Bar".'),
            ("user", "Foo"),
        ]
    )
    chain = prompt | ChatOpenAI(
        api_key=OPENAI_API_KEY,
        model="gpt-4o-mini",
        temperature=0,
        max_tokens=1,
        stream=True,
        stream_usage=True,
    )
    callbacks = CallbackHandler(mock_client)
    result = [m async for m in chain.astream({}, config={"callbacks": [callbacks]})]
    result = sum(result[1:], result[0])

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 3

    gen_args = mock_client.capture.call_args_list[1][1]
    gen_props = gen_args["properties"]
    trace_args = mock_client.capture.call_args_list[2][1]
    trace_props = trace_args["properties"]

    assert gen_args["event"] == "$ai_generation"
    assert gen_props["$ai_model_parameters"]["stream"]
    assert gen_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert gen_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert gen_props["$ai_http_status"] == 200
    assert gen_props["$ai_input_tokens"] == 20
    assert gen_props["$ai_output_tokens"] == 1

    assert trace_args["event"] == "$ai_trace"
    assert trace_props["$ai_input_state"] == {"input": ""}
    assert isinstance(trace_props["$ai_output_state"], AIMessage)


def test_base_url_retrieval(mock_client):
    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    chain = prompt | ChatOpenAI(
        api_key="test",
        model="posthog-mini",
        base_url="https://test.posthog.com",
    )
    callbacks = CallbackHandler(mock_client)
    with pytest.raises(Exception):
        chain.invoke({}, config={"callbacks": [callbacks]})

    assert mock_client.capture.call_count == 3
    generation_call = mock_client.capture.call_args_list[1][1]
    assert generation_call["properties"]["$ai_base_url"] == "https://test.posthog.com"


def test_groups(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", 'You must always answer with "Bar".'),
            ("user", "Foo"),
        ]
    )
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    chain = prompt | model
    callbacks = CallbackHandler(mock_client, groups={"company": "test_company"})
    chain.invoke({}, config={"callbacks": [callbacks]})

    assert mock_client.capture.call_count == 3
    generation_call = mock_client.capture.call_args_list[1][1]
    assert generation_call["groups"] == {"company": "test_company"}


def test_privacy_mode_local(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", 'You must always answer with "Bar".'),
            ("user", "Foo"),
        ]
    )
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    chain = prompt | model
    callbacks = CallbackHandler(mock_client, privacy_mode=True)
    chain.invoke({}, config={"callbacks": [callbacks]})

    assert mock_client.capture.call_count == 3
    generation_call = mock_client.capture.call_args_list[1][1]
    assert generation_call["properties"]["$ai_input"] is None
    assert generation_call["properties"]["$ai_output_choices"] is None


def test_privacy_mode_global(mock_client):
    mock_client.privacy_mode = True
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", 'You must always answer with "Bar".'),
            ("user", "Foo"),
        ]
    )
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    chain = prompt | model
    callbacks = CallbackHandler(mock_client)
    chain.invoke({}, config={"callbacks": [callbacks]})

    assert mock_client.capture.call_count == 3
    generation_call = mock_client.capture.call_args_list[1][1]
    assert generation_call["properties"]["$ai_input"] is None
    assert generation_call["properties"]["$ai_output_choices"] is None


@pytest.mark.skipif(not ANTHROPIC_API_KEY, reason="ANTHROPIC_API_KEY is not set")
def test_anthropic_chain(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", 'You must always answer with "Bar".'),
            ("user", "Foo"),
        ]
    )
    chain = prompt | ChatAnthropic(
        api_key=ANTHROPIC_API_KEY,
        model="claude-3-opus-20240229",
        temperature=0,
        max_tokens=1,
    )
    callbacks = CallbackHandler(
        mock_client,
        trace_id="test-trace-id",
        distinct_id="test_id",
        properties={"foo": "bar"},
    )
    start_time = time.time()
    result = chain.invoke({}, config={"callbacks": [callbacks]})
    approximate_latency = math.floor(time.time() - start_time)

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 3

    gen_args = mock_client.capture.call_args_list[1][1]
    gen_props = gen_args["properties"]
    trace_args = mock_client.capture.call_args_list[2][1]
    trace_props = trace_args["properties"]

    assert gen_args["event"] == "$ai_generation"
    assert gen_props["$ai_trace_id"] == "test-trace-id"
    assert gen_props["$ai_provider"] == "anthropic"
    assert gen_props["$ai_model"] == "claude-3-opus-20240229"
    assert gen_props["foo"] == "bar"

    assert gen_props["$ai_model_parameters"] == {
        "temperature": 0.0,
        "max_tokens": 1,
        "streaming": False,
    }
    assert gen_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert gen_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert gen_props["$ai_http_status"] == 200
    assert isinstance(gen_props["$ai_latency"], float)
    assert min(approximate_latency - 1, 0) <= math.floor(gen_props["$ai_latency"]) <= approximate_latency
    assert gen_props["$ai_input_tokens"] == 17
    assert gen_props["$ai_output_tokens"] == 1

    assert trace_args["event"] == "$ai_trace"
    assert trace_props["$ai_input_state"] == {}
    assert isinstance(trace_props["$ai_output_state"], AIMessage)


@pytest.mark.skipif(not ANTHROPIC_API_KEY, reason="ANTHROPIC_API_KEY is not set")
async def test_async_anthropic_streaming(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", 'You must always answer with "Bar".'),
            ("user", "Foo"),
        ]
    )
    chain = prompt | ChatAnthropic(
        api_key=ANTHROPIC_API_KEY,
        model="claude-3-opus-20240229",
        temperature=0,
        max_tokens=1,
        streaming=True,
        stream_usage=True,
    )
    callbacks = CallbackHandler(mock_client)
    result = [m async for m in chain.astream({}, config={"callbacks": [callbacks]})]
    result = sum(result[1:], result[0])

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 3

    gen_args = mock_client.capture.call_args_list[1][1]
    gen_props = gen_args["properties"]
    trace_args = mock_client.capture.call_args_list[2][1]
    trace_props = trace_args["properties"]

    assert gen_args["event"] == "$ai_generation"
    assert gen_props["$ai_model_parameters"]["streaming"]
    assert gen_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert gen_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert gen_props["$ai_http_status"] == 200
    assert gen_props["$ai_input_tokens"] == 17
    assert gen_props["$ai_output_tokens"] is not None

    assert trace_args["event"] == "$ai_trace"
    assert trace_props["$ai_input_state"] == {
        "input": "",
    }
    assert isinstance(trace_props["$ai_output_state"], AIMessage)


def test_metadata_tools(mock_client):
    callbacks = CallbackHandler(mock_client)
    run_id = uuid.uuid4()
    tools = [
        [
            {
                "type": "function",
                "function": {
                    "name": "foo",
                    "description": "The foo.",
                    "parameters": {
                        "properties": {
                            "bar": {
                                "description": "The bar of foo.",
                                "type": "string",
                            },
                        },
                        "required": ["query_description", "query_kind"],
                        "type": "object",
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            }
        ]
    ]

    with patch("time.time", return_value=1234567890):
        callbacks._set_llm_metadata(
            {"kwargs": {"openai_api_base": "https://us.posthog.com"}},
            run_id,
            messages=[{"role": "user", "content": "What's the weather like in SF?"}],
            invocation_params={"temperature": 0.5, "tools": tools},
            metadata={"ls_model_name": "hog-mini", "ls_provider": "posthog"},
            name="test",
        )
    expected = GenerationMetadata(
        model="hog-mini",
        input=[{"role": "user", "content": "What's the weather like in SF?"}],
        start_time=1234567890,
        model_params={"temperature": 0.5},
        provider="posthog",
        base_url="https://us.posthog.com",
        name="test",
        tools=tools,
        end_time=None,
    )
    assert callbacks._runs[run_id] == expected
    with patch("time.time", return_value=1234567891):
        run = callbacks._pop_run_metadata(run_id)
    expected.end_time = 1234567891
    assert run == expected
    assert callbacks._runs == {}


def test_tool_calls(mock_client):
    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="Bar",
                additional_kwargs={
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "123",
                            "function": {
                                "name": "test",
                                "args": '{"a": 1}',
                            },
                        }
                    ]
                },
            )
        ]
    )
    chain = prompt | model
    callbacks = CallbackHandler(mock_client)
    chain.invoke({}, config={"callbacks": [callbacks]})

    assert mock_client.capture.call_count == 3
    generation_call = mock_client.capture.call_args_list[1][1]
    assert generation_call["properties"]["$ai_output_choices"][0]["tool_calls"] == [
        {
            "type": "function",
            "id": "123",
            "function": {
                "name": "test",
                "args": '{"a": 1}',
            },
        }
    ]
    assert "additional_kwargs" not in generation_call["properties"]["$ai_output_choices"][0]


async def test_async_traces(mock_client):
    async def sleep(x):  # -> Any:
        await asyncio.sleep(0.1)
        return x

    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    chain1 = RunnableLambda(sleep)
    chain2 = prompt | FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])

    cb = CallbackHandler(mock_client)

    start_time = time.time()
    await asyncio.gather(
        chain1.ainvoke({}, config={"callbacks": [cb]}),
        chain2.ainvoke({}, config={"callbacks": [cb]}),
    )
    approximate_latency = math.floor(time.time() - start_time)
    assert mock_client.capture.call_count == 4

    first_call, second_call, third_call, fourth_call = mock_client.capture.call_args_list
    assert first_call[1]["event"] == "$ai_span"
    assert second_call[1]["event"] == "$ai_generation"
    assert third_call[1]["event"] == "$ai_trace"
    assert third_call[1]["properties"]["$ai_span_name"] == "RunnableSequence"
    assert fourth_call[1]["event"] == "$ai_trace"
    assert fourth_call[1]["properties"]["$ai_span_name"] == "sleep"
    assert (
        min(approximate_latency - 1, 0) <= math.floor(third_call[1]["properties"]["$ai_latency"]) <= approximate_latency
    )


@pytest.mark.skipif(not OPENAI_API_KEY, reason="OPENAI_API_KEY is not set")
def test_langgraph_agent(mock_client):
    @tool
    def get_weather(city: Literal["nyc", "sf"]):
        """
        Use this to get weather information.

        Args:
            city: The city to get weather information for.
        """
        if city == "sf":
            return "It's always sunny in sf"
        return "No info"

    tools = [get_weather]
    model = ChatOpenAI(api_key=OPENAI_API_KEY, model="gpt-4o-mini", temperature=0)
    graph = create_react_agent(model, tools=tools)
    inputs = {"messages": [("user", "what is the weather in sf")]}
    cb = CallbackHandler(mock_client, trace_id="test-trace-id", distinct_id="test-distinct-id")
    graph.invoke(inputs, config={"callbacks": [cb]})
    calls = [call[1] for call in mock_client.capture.call_args_list]
    assert len(calls) == 21
    for call in calls:
        assert call["properties"]["$ai_trace_id"] == "test-trace-id"
    assert len([call for call in calls if call["event"] == "$ai_generation"]) == 2
    assert len([call for call in calls if call["event"] == "$ai_span"]) == 18
    assert len([call for call in calls if call["event"] == "$ai_trace"]) == 1


@pytest.mark.parametrize("trace_id", ["test-trace-id", None])
def test_span_set_parent_ids(mock_client, trace_id):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a helpful assistant."),
            ("user", "Who won the world series in 2020?"),
        ]
    )
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content="The Los Angeles Dodgers won the World Series in 2020.")]
    )
    callbacks = [CallbackHandler(mock_client, trace_id=trace_id)]
    chain = prompt | model
    chain.invoke({}, config={"callbacks": callbacks})

    assert mock_client.capture.call_count == 3

    span_props = mock_client.capture.call_args_list[0][1]
    assert span_props["properties"]["$ai_trace_id"] == span_props["properties"]["$ai_parent_id"]

    generation_props = mock_client.capture.call_args_list[1][1]
    assert generation_props["properties"]["$ai_trace_id"] == generation_props["properties"]["$ai_parent_id"]


@pytest.mark.parametrize("trace_id", ["test-trace-id", None])
def test_span_set_parent_ids_for_third_level_run(mock_client, trace_id):
    def span_1(_):
        def span_2(_):
            def span_3(_):
                return "span 3"

            return RunnableLambda(span_3)

        return RunnableLambda(span_2)

    callbacks = [CallbackHandler(mock_client, trace_id=trace_id)]
    chain = RunnableLambda(span_1)
    chain.invoke({}, config={"callbacks": callbacks})

    assert mock_client.capture.call_count == 3

    span2, span1, trace = [call[1]["properties"] for call in mock_client.capture.call_args_list]
    assert span2["$ai_parent_id"] == span1["$ai_span_id"]
    assert span1["$ai_parent_id"] == trace["$ai_trace_id"]


def test_captures_error_with_details_in_span(mock_client):
    def span(_):
        raise ValueError("test")

    callbacks = [CallbackHandler(mock_client)]
    chain = RunnableLambda(span) | RunnableLambda(lambda _: "foo")
    try:
        chain.invoke({}, config={"callbacks": callbacks})
    except ValueError:
        pass

    assert mock_client.capture.call_count == 2
    assert mock_client.capture.call_args_list[1][1]["properties"]["$ai_error"] == "ValueError: test"
    assert mock_client.capture.call_args_list[1][1]["properties"]["$ai_is_error"]


def test_captures_error_without_details_in_span(mock_client):
    def span(_):
        raise ValueError

    callbacks = [CallbackHandler(mock_client)]
    chain = RunnableLambda(span) | RunnableLambda(lambda _: "foo")
    try:
        chain.invoke({}, config={"callbacks": callbacks})
    except ValueError:
        pass

    assert mock_client.capture.call_count == 2
    assert mock_client.capture.call_args_list[1][1]["properties"]["$ai_error"] == "ValueError"
    assert mock_client.capture.call_args_list[1][1]["properties"]["$ai_is_error"]
