import logging
import math
import os
from pyexpat.errors import messages
import time
from typing import List, Optional, TypedDict, Union
import uuid
from unittest.mock import patch, ANY

import pytest
from langchain_anthropic.chat_models import ChatAnthropic
from langchain_community.chat_models.fake import FakeMessagesListChatModel
from langchain_community.llms.fake import FakeListLLM, FakeStreamingListLLM
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_openai.chat_models import ChatOpenAI
from langgraph.graph.state import StateGraph, START, END

from posthog.ai.langchain import CallbackHandler

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
        callbacks._set_run_metadata(
            {"kwargs": {"openai_api_base": "https://us.posthog.com"}},
            run_id,
            messages=[{"role": "user", "content": "Who won the world series in 2020?"}],
            invocation_params={"temperature": 0.5},
            metadata={"ls_model_name": "hog-mini", "ls_provider": "posthog"},
        )
    expected = {
        "model": "hog-mini",
        "messages": [{"role": "user", "content": "Who won the world series in 2020?"}],
        "start_time": 1234567890,
        "model_params": {"temperature": 0.5},
        "provider": "posthog",
        "base_url": "https://us.posthog.com",
    }
    assert callbacks._runs[run_id] == expected
    with patch("time.time", return_value=1234567891):
        run = callbacks._pop_run_metadata(run_id)
    assert run == {**expected, "end_time": 1234567891}
    assert callbacks._runs == {}
    callbacks._pop_run_metadata(uuid.uuid4())  # should not raise


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
    assert mock_client.capture.call_count == 2
    generation_args = mock_client.capture.call_args_list[0][1]
    generation_props = generation_args["properties"]
    trace_args = mock_client.capture.call_args_list[1][1]

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
    assert generation_props["$ai_trace_id"] is not None
    assert isinstance(generation_props["$ai_latency"], float)
    assert trace_args["event"] == "$ai_trace"


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
    assert mock_client.capture.call_count == 2

    generation_args = mock_client.capture.call_args_list[0][1]
    generation_props = generation_args["properties"]
    trace_args = mock_client.capture.call_args_list[1][1]
    trace_props = trace_args["properties"]

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
    assert generation_props["$ai_trace_id"] is not None
    assert isinstance(generation_props["$ai_latency"], float)

    assert trace_args["event"] == "$ai_trace"
    assert "distinct_id" in generation_args
    assert trace_props["$ai_trace_id"] == generation_props["$ai_trace_id"]


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


def test_trace_id_for_multiple_chains(mock_client):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("user", "Foo"),
        ]
    )
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    callbacks = [CallbackHandler(mock_client)]
    chain = prompt | model | RunnableLambda(lambda x: [x]) | model
    result = chain.invoke({}, config={"callbacks": callbacks})

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 3

    first_call_args = mock_client.capture.call_args_list[0][1]
    first_call_props = first_call_args["properties"]
    assert first_call_args["event"] == "$ai_generation"
    assert "distinct_id" in first_call_args
    assert "$ai_model" in first_call_props
    assert "$ai_provider" in first_call_props
    assert first_call_props["$ai_input"] == [{"role": "user", "content": "Foo"}]
    assert first_call_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert first_call_props["$ai_http_status"] == 200
    assert first_call_props["$ai_trace_id"] is not None
    assert isinstance(first_call_props["$ai_latency"], float)

    second_generation_args = mock_client.capture.call_args_list[1][1]
    second_generation_props = second_generation_args["properties"]
    assert second_generation_args["event"] == "$ai_generation"
    assert "distinct_id" in second_generation_args
    assert "$ai_model" in second_generation_props
    assert "$ai_provider" in second_generation_props
    assert second_generation_props["$ai_input"] == [{"role": "assistant", "content": "Bar"}]
    assert second_generation_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert second_generation_props["$ai_http_status"] == 200
    assert second_generation_props["$ai_trace_id"] is not None
    assert isinstance(second_generation_props["$ai_latency"], float)

    trace_args = mock_client.capture.call_args_list[2][1]
    trace_props = trace_args["properties"]
    assert trace_args["event"] == "$ai_trace"
    assert "distinct_id" in trace_args
    assert trace_props["$ai_input_state"] == {}
    assert isinstance(trace_props["$ai_output_state"], AIMessage)
    assert trace_props["$ai_output_state"].content == "Bar"
    assert trace_props["$ai_trace_id"] is not None

    # Check that the trace_id is the same as the first call
    assert first_call_props["$ai_trace_id"] == second_generation_props["$ai_trace_id"]
    assert first_call_props["$ai_trace_id"] == trace_props["$ai_trace_id"]


def test_personless_mode(mock_client):
    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    chain = prompt | FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    chain.invoke({}, config={"callbacks": [CallbackHandler(mock_client)]})
    assert mock_client.capture.call_count == 2
    generation_args = mock_client.capture.call_args_list[0][1]
    trace_args = mock_client.capture.call_args_list[1][1]
    assert generation_args["event"] == "$ai_generation"
    assert generation_args["properties"]["$process_person_profile"] is False
    assert trace_args["event"] == "$ai_trace"
    assert trace_args["properties"]["$process_person_profile"] is False

    id = uuid.uuid4()
    chain.invoke({}, config={"callbacks": [CallbackHandler(mock_client, distinct_id=id)]})
    assert mock_client.capture.call_count == 4
    generation_args = mock_client.capture.call_args_list[2][1]
    trace_args = mock_client.capture.call_args_list[3][1]
    assert "$process_person_profile" not in generation_args["properties"]
    assert generation_args["distinct_id"] == id
    assert "$process_person_profile" not in trace_args["properties"]
    assert trace_args["distinct_id"] == id


def test_personless_mode_exception(mock_client):
    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    chain = prompt | ChatOpenAI(api_key="test", model="gpt-4o-mini")
    callbacks = CallbackHandler(mock_client)
    with pytest.raises(Exception):
        chain.invoke({}, config={"callbacks": [callbacks]})
    assert mock_client.capture.call_count == 2
    generation_args = mock_client.capture.call_args_list[0][1]
    trace_args = mock_client.capture.call_args_list[1][1]
    assert generation_args["event"] == "$ai_generation"
    assert generation_args["properties"]["$process_person_profile"] is False
    assert trace_args["event"] == "$ai_trace"
    assert trace_args["properties"]["$process_person_profile"] is False

    id = uuid.uuid4()
    with pytest.raises(Exception):
        chain.invoke({}, config={"callbacks": [CallbackHandler(mock_client, distinct_id=id)]})
    assert mock_client.capture.call_count == 4
    generation_args = mock_client.capture.call_args_list[2][1]
    trace_args = mock_client.capture.call_args_list[3][1]
    assert "$process_person_profile" not in generation_args["properties"]
    assert generation_args["distinct_id"] == id
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
    assert mock_client.capture.call_count == 2

    generation_call_args = mock_client.capture.call_args_list[0][1]
    generation_call_props = generation_call_args["properties"]
    assert generation_call_args["distinct_id"] == "test_id"
    assert generation_call_args["event"] == "$ai_generation"
    assert generation_call_props["$ai_trace_id"] == "test-trace-id"
    assert generation_call_props["foo"] == "bar"
    assert generation_call_props["$ai_input"] == [{"role": "user", "content": "Foo"}]
    assert generation_call_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert generation_call_props["$ai_http_status"] == 200
    assert isinstance(generation_call_props["$ai_latency"], float)

    trace_call_args = mock_client.capture.call_args_list[1][1]
    trace_call_props = trace_call_args["properties"]
    assert trace_call_args["distinct_id"] == "test_id"
    assert trace_call_args["event"] == "$ai_trace"
    assert trace_call_props["$ai_trace_id"] == "test-trace-id"
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
        lambda state: (
            {
                "messages": [
                    *state["messages"],
                    AIMessage(content="Let's explore bar."),
                ],
                "xyz": "abc",
            }
        ),
    )
    graph.add_node(
        "fake_llm",
        lambda state: (
            ChatPromptTemplate.from_messages([("user", "Foo")])
            | FakeMessagesListChatModel(
                responses=[
                    *state["messages"],
                    AIMessage(content="It's a type of greeble."),
                ]
            )
        ).invoke(
            state,
            config=config,
        ),
    )
    graph.add_edge(START, "fake_plain")
    graph.add_edge("fake_plain", "fake_llm")
    graph.add_edge("fake_llm", END)

    result = graph.compile().invoke(
        {"messages": [HumanMessage(content="What's a bar?")], "xyz": None},
        config=config,
    )

    assert len(result["messages"]) == 2
    assert isinstance(result["messages"][0], HumanMessage)
    assert result["messages"][0].content == "What's a bar?"
    assert isinstance(result["messages"][1], AIMessage)
    assert result["messages"][1].content == "Let's explore bar."

    assert mock_client.capture.call_count == 3
    generation_args = mock_client.capture.call_args_list[0][1]
    trace_args = mock_client.capture.call_args_list[2][1]
    assert generation_args["event"] == "$ai_generation"
    assert trace_args["event"] == "$ai_trace"
    assert len(trace_args["properties"]["$ai_input_state"]["messages"]) == 1
    assert isinstance(trace_args["properties"]["$ai_input_state"]["messages"][0], HumanMessage)
    assert trace_args["properties"]["$ai_input_state"]["messages"][0].content == "What's a bar?"
    assert trace_args["properties"]["$ai_input_state"]["messages"][0].type == "human"
    assert trace_args["properties"]["$ai_input_state"]["xyz"] is None
    assert len(trace_args["properties"]["$ai_output_state"]["messages"]) == 2
    assert isinstance(trace_args["properties"]["$ai_output_state"]["messages"][0], HumanMessage)  # FIXME
    assert trace_args["properties"]["$ai_output_state"]["messages"][0].content == "What's a bar?"
    assert isinstance(trace_args["properties"]["$ai_output_state"]["messages"][1], AIMessage)
    assert trace_args["properties"]["$ai_output_state"]["messages"][1].content == "Let's explore bar."
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
        assert callbacks._runs == {}
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


def test_openai_error(mock_client):
    prompt = ChatPromptTemplate.from_messages([("user", "Foo")])
    chain = prompt | ChatOpenAI(api_key="test", model="gpt-4o-mini")
    callbacks = CallbackHandler(mock_client)

    # 401
    with pytest.raises(Exception):
        chain.invoke({}, config={"callbacks": [callbacks]})

    assert callbacks._runs == {}
    assert callbacks._parent_tree == {}
    assert mock_client.capture.call_count == 2
    generation_args = mock_client.capture.call_args_list[0][1]
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
    assert mock_client.capture.call_count == 2

    first_call_args = mock_client.capture.call_args_list[0][1]
    first_call_props = first_call_args["properties"]
    assert first_call_args["event"] == "$ai_generation"
    assert first_call_props["$ai_trace_id"] == "test-trace-id"
    assert first_call_props["$ai_provider"] == "openai"
    assert first_call_props["$ai_model"] == "gpt-4o-mini"
    assert first_call_props["foo"] == "bar"

    # langchain-openai for langchain v3
    if "max_completion_tokens" in first_call_props["$ai_model_parameters"]:
        assert first_call_props["$ai_model_parameters"] == {
            "temperature": 0.0,
            "max_completion_tokens": 1,
            "stream": False,
        }
    else:
        assert first_call_props["$ai_model_parameters"] == {
            "temperature": 0.0,
            "max_tokens": 1,
            "n": 1,
            "stream": False,
        }
    assert first_call_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert first_call_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar", "refusal": None}]
    assert first_call_props["$ai_http_status"] == 200
    assert isinstance(first_call_props["$ai_latency"], float)
    assert min(approximate_latency - 1, 0) <= math.floor(first_call_props["$ai_latency"]) <= approximate_latency
    assert first_call_props["$ai_input_tokens"] == 20
    assert first_call_props["$ai_output_tokens"] == 1


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
    assert mock_client.capture.call_count == 2

    first_call_args = mock_client.capture.call_args_list[0][1]
    first_call_props = first_call_args["properties"]
    second_call_args = mock_client.capture.call_args_list[1][1]
    second_call_props = second_call_args["properties"]

    assert first_call_args["event"] == "$ai_generation"
    assert first_call_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert first_call_props["$ai_output_choices"] == [
        {"role": "assistant", "content": "Bar", "refusal": None},
        {
            "role": "assistant",
            "content": "Bar",
        },
    ]

    # langchain-openai for langchain v3
    if "max_completion_tokens" in first_call_props["$ai_model_parameters"]:
        assert first_call_props["$ai_model_parameters"] == {
            "temperature": 0.0,
            "max_completion_tokens": 1,
            "stream": False,
            "n": 2,
        }
    else:
        assert first_call_props["$ai_model_parameters"] == {
            "temperature": 0.0,
            "max_tokens": 1,
            "stream": False,
            "n": 2,
        }
    assert first_call_props["$ai_http_status"] == 200

    assert second_call_args["event"] == "$ai_trace"
    assert second_call_props["$ai_input_state"] == {}
    assert isinstance(second_call_props["$ai_output_state"], AIMessage)


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
    assert mock_client.capture.call_count == 2

    first_call_args = mock_client.capture.call_args_list[0][1]
    first_call_props = first_call_args["properties"]
    second_call_args = mock_client.capture.call_args_list[1][1]
    second_call_props = second_call_args["properties"]

    assert first_call_args["event"] == "$ai_generation"
    assert first_call_props["$ai_model_parameters"]["stream"]
    assert first_call_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert first_call_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert first_call_props["$ai_http_status"] == 200
    assert first_call_props["$ai_input_tokens"] == 20
    assert first_call_props["$ai_output_tokens"] == 1

    assert second_call_args["event"] == "$ai_trace"
    assert second_call_props["$ai_input_state"] == {"input": ""}
    assert isinstance(second_call_props["$ai_output_state"], AIMessage)


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
    assert mock_client.capture.call_count == 2

    first_call_args = mock_client.capture.call_args_list[0][1]
    first_call_props = first_call_args["properties"]
    second_call_args = mock_client.capture.call_args_list[1][1]
    second_call_props = second_call_args["properties"]

    assert first_call_args["event"] == "$ai_generation"
    assert first_call_props["$ai_model_parameters"]["stream"]
    assert first_call_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert first_call_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert first_call_props["$ai_http_status"] == 200
    assert first_call_props["$ai_input_tokens"] == 20
    assert first_call_props["$ai_output_tokens"] == 1

    assert second_call_args["event"] == "$ai_trace"
    assert second_call_props["$ai_input_state"] == {"input": ""}
    assert isinstance(second_call_props["$ai_output_state"], AIMessage)


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

    assert mock_client.capture.call_count == 2
    generation_call = mock_client.capture.call_args_list[0][1]
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

    assert mock_client.capture.call_count == 2
    generation_call = mock_client.capture.call_args_list[0][1]
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

    assert mock_client.capture.call_count == 2
    generation_call = mock_client.capture.call_args_list[0][1]
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

    assert mock_client.capture.call_count == 2
    generation_call = mock_client.capture.call_args_list[0][1]
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
    assert mock_client.capture.call_count == 2

    first_call_args = mock_client.capture.call_args_list[0][1]
    first_call_props = first_call_args["properties"]
    second_call_args = mock_client.capture.call_args_list[1][1]
    second_call_props = second_call_args["properties"]

    assert first_call_args["event"] == "$ai_generation"
    assert first_call_props["$ai_trace_id"] == "test-trace-id"
    assert first_call_props["$ai_provider"] == "anthropic"
    assert first_call_props["$ai_model"] == "claude-3-opus-20240229"
    assert first_call_props["foo"] == "bar"

    assert first_call_props["$ai_model_parameters"] == {
        "temperature": 0.0,
        "max_tokens": 1,
        "streaming": False,
    }
    assert first_call_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert first_call_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert first_call_props["$ai_http_status"] == 200
    assert isinstance(first_call_props["$ai_latency"], float)
    assert min(approximate_latency - 1, 0) <= math.floor(first_call_props["$ai_latency"]) <= approximate_latency
    assert first_call_props["$ai_input_tokens"] == 17
    assert first_call_props["$ai_output_tokens"] == 1

    assert second_call_args["event"] == "$ai_trace"
    assert second_call_props["$ai_input_state"] == {}
    assert isinstance(second_call_props["$ai_output_state"], AIMessage)


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
    assert mock_client.capture.call_count == 2

    first_call_args = mock_client.capture.call_args_list[0][1]
    first_call_props = first_call_args["properties"]
    second_call_args = mock_client.capture.call_args_list[1][1]
    second_call_props = second_call_args["properties"]

    assert first_call_args["event"] == "$ai_generation"
    assert first_call_props["$ai_model_parameters"]["streaming"]
    assert first_call_props["$ai_input"] == [
        {"role": "system", "content": 'You must always answer with "Bar".'},
        {"role": "user", "content": "Foo"},
    ]
    assert first_call_props["$ai_output_choices"] == [{"role": "assistant", "content": "Bar"}]
    assert first_call_props["$ai_http_status"] == 200
    assert first_call_props["$ai_input_tokens"] == 17
    assert first_call_props["$ai_output_tokens"] is not None

    assert second_call_args["event"] == "$ai_trace"
    assert second_call_props["$ai_input_state"] == {
        "input": "",
    }
    assert isinstance(second_call_props["$ai_output_state"], AIMessage)


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

    assert mock_client.capture.call_count == 2
    generation_call = mock_client.capture.call_args_list[0][1]
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
