"""
Test script to verify that tool_calls in conversation history are stripped from $ai_input.

This script demonstrates the issue where:
1. First API call: User asks a question that triggers a tool call
2. The response includes tool_calls
3. Second API call: We send the full conversation history (including assistant message with tool_calls)
4. We verify that tool_calls are stripped from $ai_input in the second call

Setup:
Option 1: Set environment variable
  export OPENAI_API_KEY=your-key-here
  python test_tool_calls_issue.py

Option 2: Create a .env file
  echo "OPENAI_API_KEY=your-key-here" > .env
  python test_tool_calls_issue.py
"""

import json
import os
from unittest.mock import MagicMock

try:
    from posthog.ai.openai import OpenAI
except ImportError:
    print("Error: Could not import posthog OpenAI wrapper")
    print("Make sure you've installed the package: pip install -e .")
    exit(1)

# Try to load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, will use environment variables

# Create a mock PostHog client to capture events
mock_posthog = MagicMock()
mock_posthog.privacy_mode = False

# Create OpenAI client with PostHog tracking
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    print("Error: OPENAI_API_KEY not found in environment")
    print("Please create a .env file with: OPENAI_API_KEY=your-key-here")
    exit(1)

client = OpenAI(api_key=api_key, posthog_client=mock_posthog)

# Define a simple weather tool
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city name",
                    }
                },
                "required": ["location"],
            },
        },
    }
]

print("=" * 80)
print("FIRST API CALL - User asks about weather")
print("=" * 80)

# First call - user asks about weather
messages = [{"role": "user", "content": "What's the weather in San Francisco?"}]

response1 = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=messages,
    tools=tools,
    posthog_distinct_id="test-user",
)

print(f"\nResponse finish reason: {response1.choices[0].finish_reason}")
print(f"Response message: {response1.choices[0].message}")

# Check if tool was called
has_tool_calls = (
    hasattr(response1.choices[0].message, "tool_calls")
    and response1.choices[0].message.tool_calls
)

print(f"\nTool calls in response: {has_tool_calls}")

if has_tool_calls:
    for tool_call in response1.choices[0].message.tool_calls:
        print(f"  - {tool_call.function.name}({tool_call.function.arguments})")

# Check what PostHog captured for first call
call1_args = mock_posthog.capture.call_args_list[0][1]
call1_props = call1_args["properties"]

print(f"\n📊 First call - PostHog captured:")
print(f"  $ai_input: {json.dumps(call1_props['$ai_input'], indent=2)}")
print(f"  $ai_output_choices: {json.dumps(call1_props['$ai_output_choices'], indent=2)}")

print("\n" + "=" * 80)
print("SECOND API CALL - Continuing conversation with tool call history")
print("=" * 80)

if not has_tool_calls:
    print("\n⚠️  No tool calls in first response. Exiting.")
    print("Try running again - sometimes the model doesn't call the tool.")
    exit(0)

# Build conversation history including the assistant's tool call
conversation_history = [
    {"role": "user", "content": "What's the weather in San Francisco?"},
    {
        "role": "assistant",
        "content": response1.choices[0].message.content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": tc.type,
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in response1.choices[0].message.tool_calls
        ],
    },
    {
        "role": "tool",
        "tool_call_id": response1.choices[0].message.tool_calls[0].id,
        "content": '{"temperature": "15°C", "condition": "sunny"}',
    },
]

print(f"\nConversation history being sent:")
print(json.dumps(conversation_history, indent=2))

# Second call with full conversation history
response2 = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=conversation_history,
    tools=tools,
    posthog_distinct_id="test-user",
)

print(f"\nSecond response: {response2.choices[0].message.content}")

# Check what PostHog captured for second call
call2_args = mock_posthog.capture.call_args_list[1][1]
call2_props = call2_args["properties"]

print(f"\n📊 Second call - PostHog captured:")
print(f"  $ai_input: {json.dumps(call2_props['$ai_input'], indent=2)}")

print("\n" + "=" * 80)
print("ANALYSIS - Checking for the issue")
print("=" * 80)

# Check if tool_calls were preserved in the input
input_messages = call2_props["$ai_input"]
assistant_message = next(
    (msg for msg in input_messages if msg.get("role") == "assistant"), None
)

if assistant_message:
    has_tool_calls_in_input = "tool_calls" in assistant_message or any(
        item.get("type") == "function"
        for item in (
            assistant_message.get("content", [])
            if isinstance(assistant_message.get("content"), list)
            else []
        )
    )

    print(f"\nAssistant message in captured input:")
    print(json.dumps(assistant_message, indent=2))

    if has_tool_calls_in_input:
        print("\n✅ GOOD: Tool calls ARE preserved in $ai_input")
    else:
        print("\n❌ ISSUE CONFIRMED: Tool calls are MISSING from $ai_input")
        print("   The assistant message should include the tool_calls information")
        print("   but it only has:", list(assistant_message.keys()))
else:
    print("\n⚠️  No assistant message found in captured input")

print("\n" + "=" * 80)

