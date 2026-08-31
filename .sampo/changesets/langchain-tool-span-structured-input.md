---
pypi/posthog: patch
---

Capture LangChain tool inputs on `$ai_span` as structured data rather than a Python `repr` string. `BaseTool.run`/`arun` pass the tool input to `on_tool_start` twice — positionally as `str(tool_input)`, and as the original dict under the `inputs` keyword. The handler was storing the positional value, so a dict input landed in `$ai_input_state` as `{'query': 'SELECT 1'}` (single quotes), which no JSON parser can read: `JSONExtract*` in ClickHouse returns empty, and any downstream consumer has to fall back to substring matching. Tool spans now record the `inputs` dict when LangChain supplies one, matching what `on_chain_start` already does; tools invoked with a plain string are unchanged.
