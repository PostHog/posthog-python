# 7.7.0 - 2026-01-15

feat(ai): Add OpenAI Agents SDK integration

Automatic tracing for agent workflows, handoffs, tool calls, guardrails, and custom spans. Includes `$ai_total_tokens`, `$ai_error_type` categorization, and `$ai_framework` property.

# 7.6.0 - 2026-01-12

feat: add device_id to flags request payload

Add device_id parameter to all feature flag methods, allowing the server to track device identifiers for flag evaluation. The device_id can be passed explicitly or set via context using `set_context_device_id()`.

# 7.5.1 - 2026-01-07

fix: avoid return from finally block to fix Python 3.14 SyntaxWarning (#361) - thanks @jodal

# 7.5.0 - 2026-01-06

feat: Capture Langchain, OpenAI and Anthropic errors as exceptions (if exception autocapture is enabled)
feat: Add reference to exception in LLMA trace and span events

# 7.4.3 - 2026-01-02

Fixes cache creation cost for Langchain with Anthropic

# 7.4.2 - 2025-12-22

feat: add `in_app_modules` option to control code variables capturing

# 7.4.1 - 2025-12-19

fix: extract model from response for OpenAI stored prompts

When using OpenAI stored prompts, the model is defined in the OpenAI dashboard rather than passed in the API request. This fix adds a fallback to extract the model from the response object when not provided in kwargs, ensuring generations show up with the correct model and enabling cost calculations.

# 7.4.0 - 2025-12-16

feat: Add automatic retries for feature flag requests
