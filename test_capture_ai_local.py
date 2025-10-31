#!/usr/bin/env python3
"""
Test script to send capture_ai events to localhost:8010.
This script tests the actual network request to a local PostHog instance.
"""

from posthog import Posthog
from uuid import uuid4

# Create a client pointing to localhost:8010
posthog = Posthog(
    "test-api-key",  # Use your actual project API key if needed
    host="http://localhost:8010",
    debug=True,  # Enable debug mode to see detailed logs
)

print("Testing capture_ai with localhost:8010")
print("=" * 60)

# Test 1: $ai_generation event with blobs
print("\n1. Testing $ai_generation event with blobs...")
print("-" * 60)

trace_id = f"trace_{uuid4().hex[:8]}"

try:
    event_uuid = posthog.capture_ai(
        "$ai_generation",
        distinct_id="test_user_123",
        properties={
            "$ai_model": "gpt-4",
            "$ai_provider": "openai",
            "$ai_trace_id": trace_id,
            "$ai_input": {
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a helpful assistant that answers questions about Python.",
                    },
                    {
                        "role": "user",
                        "content": "What is the difference between a list and a tuple?",
                    },
                ],
                "temperature": 0.7,
                "max_tokens": 500,
            },
            "$ai_output_choices": {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "A list is mutable (can be changed) while a tuple is immutable (cannot be changed after creation). Lists use square brackets [] and tuples use parentheses ().",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "model": "gpt-4",
                "usage": {
                    "prompt_tokens": 45,
                    "completion_tokens": 32,
                    "total_tokens": 77,
                },
            },
            "$ai_completion_tokens": 32,
            "$ai_prompt_tokens": 45,
            "$ai_total_tokens": 77,
            "$ai_latency": 1.234,
        },
        blob_properties=["$ai_input", "$ai_output_choices"],
    )

    if event_uuid:
        print("✓ SUCCESS: $ai_generation event sent")
        print(f"  UUID: {event_uuid}")
        print(f"  Trace ID: {trace_id}")
    else:
        print("✗ FAILED: No UUID returned")

except Exception as e:
    print(f"✗ ERROR: {e}")
    import traceback

    traceback.print_exc()

# Test 2: $ai_trace event
print("\n2. Testing $ai_trace event...")
print("-" * 60)

try:
    event_uuid = posthog.capture_ai(
        "$ai_trace",
        distinct_id="test_user_123",
        properties={
            "$ai_model": "gpt-4",
            "$ai_trace_id": trace_id,
            "$ai_trace_name": "python_qa_session",
            "$ai_input_state": {
                "session_id": "session_123",
                "user_context": "learning Python",
            },
            "$ai_output_state": {"questions_answered": 1, "satisfaction_score": 5},
        },
        blob_properties=["$ai_input_state", "$ai_output_state"],
    )

    if event_uuid:
        print("✓ SUCCESS: $ai_trace event sent")
        print(f"  UUID: {event_uuid}")
        print(f"  Trace ID: {trace_id}")
    else:
        print("✗ FAILED: No UUID returned")

except Exception as e:
    print(f"✗ ERROR: {e}")
    import traceback

    traceback.print_exc()

# Test 3: $ai_span event
print("\n3. Testing $ai_span event...")
print("-" * 60)

span_id = f"span_{uuid4().hex[:8]}"

try:
    event_uuid = posthog.capture_ai(
        "$ai_span",
        distinct_id="test_user_123",
        properties={
            "$ai_model": "gpt-4",
            "$ai_trace_id": trace_id,
            "$ai_span_id": span_id,
            "$ai_span_name": "answer_generation",
            "$ai_parent_id": trace_id,
            "$ai_span_kind": "llm",
            "$ai_latency": 0.8,
        },
    )

    if event_uuid:
        print("✓ SUCCESS: $ai_span event sent")
        print(f"  UUID: {event_uuid}")
        print(f"  Span ID: {span_id}")
    else:
        print("✗ FAILED: No UUID returned")

except Exception as e:
    print(f"✗ ERROR: {e}")
    import traceback

    traceback.print_exc()

# Test 4: $ai_embedding event
print("\n4. Testing $ai_embedding event...")
print("-" * 60)

try:
    event_uuid = posthog.capture_ai(
        "$ai_embedding",
        distinct_id="test_user_123",
        properties={
            "$ai_model": "text-embedding-ada-002",
            "$ai_provider": "openai",
            "$ai_trace_id": trace_id,
            "$ai_input": {
                "text": "What is the difference between a list and a tuple in Python?"
            },
            "$ai_embedding_dimension": 1536,
            "$ai_latency": 0.123,
        },
        blob_properties=["$ai_input"],
    )

    if event_uuid:
        print("✓ SUCCESS: $ai_embedding event sent")
        print(f"  UUID: {event_uuid}")
    else:
        print("✗ FAILED: No UUID returned")

except Exception as e:
    print(f"✗ ERROR: {e}")
    import traceback

    traceback.print_exc()

# Test 5: $ai_metric event
print("\n5. Testing $ai_metric event...")
print("-" * 60)

try:
    event_uuid = posthog.capture_ai(
        "$ai_metric",
        distinct_id="test_user_123",
        properties={
            "$ai_model": "gpt-4",
            "$ai_trace_id": trace_id,
            "$ai_metric_name": "response_quality",
            "$ai_metric_value": "0.95",
        },
    )

    if event_uuid:
        print("✓ SUCCESS: $ai_metric event sent")
        print(f"  UUID: {event_uuid}")
    else:
        print("✗ FAILED: No UUID returned")

except Exception as e:
    print(f"✗ ERROR: {e}")
    import traceback

    traceback.print_exc()

# Test 6: $ai_feedback event
print("\n6. Testing $ai_feedback event...")
print("-" * 60)

try:
    event_uuid = posthog.capture_ai(
        "$ai_feedback",
        distinct_id="test_user_123",
        properties={
            "$ai_model": "gpt-4",
            "$ai_trace_id": trace_id,
            "$ai_feedback_text": "Great explanation! Very clear and helpful.",
            "$ai_feedback_rating": 5,
        },
    )

    if event_uuid:
        print("✓ SUCCESS: $ai_feedback event sent")
        print(f"  UUID: {event_uuid}")
    else:
        print("✗ FAILED: No UUID returned")

except Exception as e:
    print(f"✗ ERROR: {e}")
    import traceback

    traceback.print_exc()

# Test 7: Test with custom blob properties
print("\n7. Testing with custom blob properties...")
print("-" * 60)

try:
    event_uuid = posthog.capture_ai(
        "$ai_generation",
        distinct_id="test_user_123",
        properties={
            "$ai_model": "claude-3-opus",
            "$ai_provider": "anthropic",
            "$ai_trace_id": trace_id,
            "$ai_input": {
                "messages": [{"role": "user", "content": "Write a haiku about Python"}]
            },
            "$ai_output_choices": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Snake glides through code\nSimple syntax, powerful tools\nDevelopers smile",
                        }
                    }
                ]
            },
            "$ai_custom_data": {
                "large_context": "This is some large custom data that should be sent as a blob"
            },
            "$ai_completion_tokens": 20,
            "$ai_prompt_tokens": 10,
        },
        # Custom blob properties - including the default ones plus a custom one
        blob_properties=["$ai_input", "$ai_output_choices", "$ai_custom_data"],
    )

    if event_uuid:
        print("✓ SUCCESS: Event with custom blob properties sent")
        print(f"  UUID: {event_uuid}")
    else:
        print("✗ FAILED: No UUID returned")

except Exception as e:
    print(f"✗ ERROR: {e}")
    import traceback

    traceback.print_exc()

# Test 8: Test with groups
print("\n8. Testing with groups...")
print("-" * 60)

try:
    event_uuid = posthog.capture_ai(
        "$ai_generation",
        distinct_id="test_user_123",
        groups={"company": "posthog_inc", "team": "engineering"},
        properties={
            "$ai_model": "gpt-4",
            "$ai_provider": "openai",
            "$ai_trace_id": trace_id,
            "$ai_input": {"messages": [{"role": "user", "content": "test"}]},
            "$ai_output_choices": {
                "choices": [{"message": {"role": "assistant", "content": "response"}}]
            },
        },
    )

    if event_uuid:
        print("✓ SUCCESS: Event with groups sent")
        print(f"  UUID: {event_uuid}")
    else:
        print("✗ FAILED: No UUID returned")

except Exception as e:
    print(f"✗ ERROR: {e}")
    import traceback

    traceback.print_exc()

print("\n" + "=" * 60)
print("All tests completed!")
print("\nMake sure your local PostHog instance is running on http://localhost:8010")
print("and that the /i/v0/ai endpoint is available.")
