---
pypi/posthog: patch
---

Privacy mode now covers error text and the remaining content paths in the AI integrations.

- `$ai_error` no longer carries the exception message when privacy mode is on. Provider errors and validation errors often quote the prompt or the model output, so the SDK now sends a summary built only from structured fields: the class name, HTTP status, provider error code, `param`, request ID and the class names of chained causes (for example `BadRequestError: 400 invalid_request_error param=messages[3].content request_id=req_123`). This applies to the OpenAI, Anthropic and Gemini wrappers (including Gemini embeddings), LangChain generations, spans and traces, and OpenAI Agents spans.
- The LangChain callback handler no longer sends an `$exception` event for a failed run when privacy mode is on.
- `$ai_tools` masks the `headers` and `authorization` of MCP tool definitions (`type: "mcp"`) with `[REDACTED]`, with or without privacy mode. The tools you pass in are not modified.
- Gemini: a per-call `posthog_privacy_mode=False` no longer turns off privacy mode set on the client, matching the OpenAI and Anthropic wrappers.
- OpenAI Agents: privacy mode now redacts `$ai_trace_metadata` and the `prompt` and `instructions` in transcription and speech span `model_config`.
- OpenTelemetry: `PostHogSpanProcessor` and `PostHogTraceExporter` accept `privacy_mode` and `client`, and also honor the client's (or the module-level) `privacy_mode`. When on, exported spans drop prompt, completion, tool argument and tool result attributes, non-exception span events, exception messages and stack traces, and the span status description.
