# Testing the Tool Calls Input Issue

This directory contains a test script (`test_tool_calls_issue.py`) to verify and demonstrate the issue where `tool_calls` in conversation history are stripped from `$ai_input`.

## The Issue

When OpenAI API calls include conversation history with assistant messages that made tool calls, those `tool_calls` are stripped out before being sent to PostHog as `$ai_input`. This prevents PostHog from displaying complete conversation traces that include function/tool calls.

## How to Run the Test

### Prerequisites

1. Install the package with test dependencies:
   ```bash
   pip install -e ".[test]"
   ```

2. You'll need an OpenAI API key

### Running the Test

**Option 1: Environment variable**
```bash
export OPENAI_API_KEY=your-key-here
python test_tool_calls_issue.py
```

**Option 2: .env file**
```bash
echo "OPENAI_API_KEY=your-key-here" > .env
python test_tool_calls_issue.py
```

*(Note: python-dotenv is optional - the script works with or without it)*

## What the Script Does

1. **First API Call**: Sends a user message asking about weather, which triggers the model to call the `get_weather` tool
2. **Captures Response**: Records what PostHog captured for the first call (should show tool calls in `$ai_output_choices`)
3. **Second API Call**: Sends the full conversation history including:
   - User message
   - Assistant message with tool_calls
   - Tool result
4. **Checks Input**: Verifies whether the assistant's `tool_calls` were preserved in `$ai_input` for the second call

## Expected Output

The script will print a detailed analysis and conclude with either:

- ✅ **GOOD**: Tool calls ARE preserved in `$ai_input` (issue fixed)
- ❌ **ISSUE CONFIRMED**: Tool calls are MISSING from `$ai_input` (issue exists)

## Example Output (Issue Confirmed)

```
❌ ISSUE CONFIRMED: Tool calls are MISSING from $ai_input
   The assistant message should include the tool_calls information
   but it only has: ['role', 'content']
```

## Cleanup

After running the test, you can delete:
- `.env` file (if created)
- This README and the test script (if no longer needed)

