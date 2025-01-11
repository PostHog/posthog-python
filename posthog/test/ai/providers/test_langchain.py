from langchain_community.chat_models.fake import FakeMessagesListChatModel
from langchain_community.llms.fake import FakeListLLM, FakeStreamingListLLM

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from unittest.mock import patch

import pytest
from posthog.ai.providers.langchain import PosthogCallbackHandler


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        yield mock_client


@pytest.mark.parametrize("stream", [True, False])
def test_basic_chat_chain(mock_client, stream):
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a helpful assistant."),
        ("user", "Who won the world series in 2020?"),
    ])
    model = FakeMessagesListChatModel(responses=[AIMessage(content="The Los Angeles Dodgers won the World Series in 2020.", usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20})])
    callbacks = [PosthogCallbackHandler(mock_client)]
    chain = prompt | model
    if stream:
        result = [m for m in chain.stream({}, config={"callbacks": callbacks})][0]
    else:
        result = chain.invoke({}, config={"callbacks": callbacks})

    assert result.content == "The Los Angeles Dodgers won the World Series in 2020."
    assert mock_client.capture.call_count == 1
    args = mock_client.capture.call_args[1]
    props = args["properties"]

    assert args["event"] == "$ai_generation"
    assert "distinct_id" in args
    assert "$ai_model" in props
    assert "$ai_provider" in props
    assert props["$ai_input"] == [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "Who won the world series in 2020?"}]
    assert props["$ai_output"] == {"choices": [{"role": "assistant", "content": "The Los Angeles Dodgers won the World Series in 2020."}]}
    assert props["$ai_input_tokens"] == 10
    assert props["$ai_output_tokens"] == 10
    assert props["$ai_http_status"] == 200
    assert props["$ai_trace_id"] is not None
    assert isinstance(props["$ai_latency"], float)


@pytest.mark.parametrize("stream", [True, False])
async def test_async_basic_chat_chain(mock_client, stream):
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a helpful assistant."),
        ("user", "Who won the world series in 2020?"),
    ])
    model = FakeMessagesListChatModel(responses=[AIMessage(content="The Los Angeles Dodgers won the World Series in 2020.", usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20})])
    callbacks = [PosthogCallbackHandler(mock_client)]
    chain = prompt | model
    if stream:
        result = [m async for m in chain.astream({}, config={"callbacks": callbacks})][0]
    else:
        result = await chain.ainvoke({}, config={"callbacks": callbacks})
    assert result.content == "The Los Angeles Dodgers won the World Series in 2020."
    assert mock_client.capture.call_count == 1
    
    args = mock_client.capture.call_args[1]
    props = args["properties"]
    assert args["event"] == "$ai_generation"
    assert "distinct_id" in args
    assert "$ai_model" in props
    assert "$ai_provider" in props
    assert props["$ai_input"] == [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "Who won the world series in 2020?"}]
    assert props["$ai_output"] == {"choices": [{"role": "assistant", "content": "The Los Angeles Dodgers won the World Series in 2020."}]}
    assert props["$ai_input_tokens"] == 10
    assert props["$ai_output_tokens"] == 10
    assert props["$ai_http_status"] == 200
    assert props["$ai_trace_id"] is not None
    assert isinstance(props["$ai_latency"], float)


@pytest.mark.parametrize("Model,stream", [(FakeListLLM, True), (FakeListLLM, False), (FakeStreamingListLLM, True), (FakeStreamingListLLM, False)])
def test_basic_llm_chain(mock_client, Model, stream):
    model = Model(responses=["The Los Angeles Dodgers won the World Series in 2020."])
    callbacks: list[PosthogCallbackHandler] = [PosthogCallbackHandler(mock_client)]

    if stream:
        result = "".join([m for m in model.stream("Who won the world series in 2020?", config={"callbacks": callbacks})])
    else:
        result = model.invoke("Who won the world series in 2020?", config={"callbacks": callbacks})
    assert result == "The Los Angeles Dodgers won the World Series in 2020."

    assert mock_client.capture.call_count == 1
    args = mock_client.capture.call_args[1]
    props = args["properties"]

    assert args["event"] == "$ai_generation"
    assert "distinct_id" in args
    assert "$ai_model" in props
    assert "$ai_provider" in props
    assert props["$ai_input"] == ["Who won the world series in 2020?"]
    assert props["$ai_output"] == {"choices": ["The Los Angeles Dodgers won the World Series in 2020."]}
    assert props["$ai_http_status"] == 200
    assert props["$ai_trace_id"] is not None
    assert isinstance(props["$ai_latency"], float)


@pytest.mark.parametrize("Model,stream", [(FakeListLLM, True), (FakeListLLM, False), (FakeStreamingListLLM, True), (FakeStreamingListLLM, False)])
async def test_async_basic_llm_chain(mock_client, Model, stream):
    model = Model(responses=["The Los Angeles Dodgers won the World Series in 2020."])
    callbacks: list[PosthogCallbackHandler] = [PosthogCallbackHandler(mock_client)]

    if stream:
        result = "".join([m async for m in model.astream("Who won the world series in 2020?", config={"callbacks": callbacks})])
    else:
        result = await model.ainvoke("Who won the world series in 2020?", config={"callbacks": callbacks})
    assert result == "The Los Angeles Dodgers won the World Series in 2020."

    assert mock_client.capture.call_count == 1
    args = mock_client.capture.call_args[1]
    props = args["properties"]

    assert args["event"] == "$ai_generation"
    assert "distinct_id" in args
    assert "$ai_model" in props
    assert "$ai_provider" in props
    assert props["$ai_input"] == ["Who won the world series in 2020?"]
    assert props["$ai_output"] == {"choices": ["The Los Angeles Dodgers won the World Series in 2020."]}
    assert props["$ai_http_status"] == 200
    assert props["$ai_trace_id"] is not None
    assert isinstance(props["$ai_latency"], float)


def test_trace_id_for_multiple_chains(mock_client):
    prompt = ChatPromptTemplate.from_messages([
        ("user", "Foo"),
    ])
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    callbacks = [PosthogCallbackHandler(mock_client)]
    chain = prompt | model | RunnableLambda(lambda x: [x]) | model
    result = chain.invoke({}, config={"callbacks": callbacks})

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 2
    
    first_call_args = mock_client.capture.call_args_list[0][1]
    first_call_props = first_call_args["properties"]
    assert first_call_args["event"] == "$ai_generation"
    assert "distinct_id" in first_call_args
    assert "$ai_model" in first_call_props
    assert "$ai_provider" in first_call_props
    assert first_call_props["$ai_input"] == [{"role": "user", "content": "Foo"}]
    assert first_call_props["$ai_output"] == {"choices": [{"role": "assistant", "content": "Bar"}]}
    assert first_call_props["$ai_http_status"] == 200
    assert first_call_props["$ai_trace_id"] is not None
    assert isinstance(first_call_props["$ai_latency"], float)

    second_call_args = mock_client.capture.call_args_list[1][1]
    second_call_props = second_call_args["properties"]
    assert second_call_args["event"] == "$ai_generation"
    assert "distinct_id" in second_call_args
    assert "$ai_model" in second_call_props
    assert "$ai_provider" in second_call_props
    assert second_call_props["$ai_input"] == [{"role": "assistant", "content": "Bar"}]
    assert second_call_props["$ai_output"] == {"choices": [{"role": "assistant", "content": "Bar"}]}
    assert second_call_props["$ai_http_status"] == 200
    assert second_call_props["$ai_trace_id"] is not None
    assert isinstance(second_call_props["$ai_latency"], float)

    # Check that the trace_id is the same as the first call
    assert first_call_props["$ai_trace_id"] == second_call_props["$ai_trace_id"]


def test_metadata(mock_client):
    prompt = ChatPromptTemplate.from_messages([
        ("user", "Foo"),
    ])
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    callbacks = [PosthogCallbackHandler(mock_client, trace_id="test-trace-id", distinct_id="test_id", properties={"foo": "bar"})]
    chain = prompt | model
    result = chain.invoke({}, config={"callbacks": callbacks})

    assert result.content == "Bar"
    assert mock_client.capture.call_count == 1
    
    first_call_args = mock_client.capture.call_args[1]
    assert first_call_args["distinct_id"] == "test_id"

    first_call_props = first_call_args["properties"]
    assert first_call_args["event"] == "$ai_generation"
    assert first_call_props["$ai_trace_id"] == "test-trace-id"
    assert first_call_props["$ai_posthog_properties"] == {"foo": "bar"}
    assert first_call_props["$ai_input"] == [{"role": "user", "content": "Foo"}]
    assert first_call_props["$ai_output"] == {"choices": [{"role": "assistant", "content": "Bar"}]}
    assert first_call_props["$ai_http_status"] == 200
    assert isinstance(first_call_props["$ai_latency"], float)


def test_callbacks_logic(mock_client):
    prompt = ChatPromptTemplate.from_messages([
        ("user", "Foo"),
    ])
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Bar")])
    callbacks = PosthogCallbackHandler(mock_client, trace_id="test-trace-id", distinct_id="test_id", properties={"foo": "bar"})
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
