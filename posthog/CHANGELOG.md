# posthog

## 7.45.4 — 2026-09-04

### Patch changes

- [86ad9e7](https://github.com/posthog/posthog-python/commit/86ad9e76c8f7168f5c05b1011828e536b2cdb621) Streaming generations interrupted before the provider reported any usage no longer send zero `$ai_cache_read_input_tokens`, `$ai_cache_creation_input_tokens`, or `$ai_reasoning_tokens`. A fabricated 0 reads as a report of nothing, so cost processing priced an unknown generation as a known $0.00 instead of leaving it unknown. Streams whose usage was reported keep the historical zero defaults. — Thanks @bernatixer!

## 7.45.3 — 2026-09-01

### Patch changes

- [caf9030](https://github.com/posthog/posthog-python/commit/caf9030b465d39ce94c1ecee59f8f08f838e9e1f) MCP tool failures now report the exception the tool actually raised on `$mcp_error_message` and `$mcp_error_type`, stepping past the SDK's dispatch `ToolError` wrapper. mcp 2.1 masks the original message out of that wrapper, which left the failures view with only `Error executing tool <name>`. The `$exception` sibling still carries the full chain. — Thanks @bernatixer!

## 7.45.2 — 2026-09-01

### Patch changes

- [9444ec5](https://github.com/posthog/posthog-python/commit/9444ec5618bb7414adf5787252ff36697b6d456b) Omit `$ai_input_tokens` and `$ai_output_tokens` when the provider never reported usage, instead of sending `0`, so an interrupted stream no longer looks like a free call. A zero reported by the provider is still sent, and zero keeps meaning a real report of nothing. Covers the OpenAI, Anthropic, Gemini, LangChain, OpenAI Agents and Claude Agent SDK integrations. — Thanks @bernatixer!

## 7.45.1 — 2026-08-31

### Patch changes

- [131cc1a](https://github.com/posthog/posthog-python/commit/131cc1a9b8f836f62495abaa394a1f302f8e5287) Match local feature flag string operators using the flags service's boolean coercion, JSON stringification, and casing rules. — Thanks @marandaneto!

## 7.45.0 — 2026-08-31

### Minor changes

- [f50f333](https://github.com/posthog/posthog-python/commit/f50f33396f42af49217f6604c90f85b1a1fe73dd) Add non-blocking feature flag evaluation and remote config APIs to AsyncPosthog — Thanks @marandaneto!
- [f50f333](https://github.com/posthog/posthog-python/commit/f50f33396f42af49217f6604c90f85b1a1fe73dd) Add an asyncio-native client for buffered and immediate event capture — Thanks @marandaneto!

## 7.44.2 — 2026-08-27

### Patch changes

- [fc7e043](https://github.com/posthog/posthog-python/commit/fc7e0432bbee915c834c2779310ab1232ffe6302) Honor `default_cache_ttl_seconds=0` in AI prompts so callers can disable default prompt caching. — Thanks @ckarnell for your first contribution 🎉!

## 7.44.1 — 2026-08-26

### Patch changes

- [0e70f0c](https://github.com/posthog/posthog-python/commit/0e70f0caf37f3aaa31ecbc7779843b98abfef2ba) Align local `is_set` and `is_not_set` evaluation with partial property context. — Thanks @marandaneto!

## 7.44.0 — 2026-08-25

### Minor changes

- [9a1d137](https://github.com/posthog/posthog-python/commit/9a1d1378388ac69eb439ac1381eeecc440beea1f) Add an opt-in `capture_trace_context` client option. When enabled, and a valid OpenTelemetry span is active at capture time, its trace and span IDs are attached to events captured with `capture()` and `capture_ai()` as `$trace_id` and `$span_id`, so they can be correlated with backend traces. Disabled by default, and explicit `$trace_id`/`$span_id` properties take precedence. — Thanks @DanielVisca!

## 7.43.1 — 2026-08-25

### Patch changes

- [8046114](https://github.com/posthog/posthog-python/commit/804611456f79d77ffcb5af1e9099a0a67056a373) Return an empty feature flag snapshot without evaluation when feature flag keys are explicitly empty. — Thanks @marandaneto!

## 7.43.0 — 2026-08-24

### Minor changes

- [35220f3](https://github.com/posthog/posthog-python/commit/35220f3b11c070d3b85fbbd9d73b7a2c2bec060c) Fall back to remote evaluation when a requested flag is missing from local definitions. This changes the previous behavior where the key was omitted without a request. — Thanks @marandaneto!

## 7.42.1 — 2026-08-23

### Patch changes

- [c55c9b2](https://github.com/posthog/posthog-python/commit/c55c9b22ffb6ae2db2c4a1e30813d2f89b50e297) MCP analytics now surfaces the previously-silent case where the stateless session mint middleware (`PostHogMcpStatelessSessionMiddleware`) never attached — the trap where an ASGI app is built or mounted before `instrument()` runs, so autowiring can't retrofit it and every session falls back to a fragmented per-process id. `instrument()` warns when `streamable_http_app()` was already called before it ran, and a one-time warning fires the first time a tool call arrives over streamable HTTP and the session still has to come from process memory. Both go to the `posthog.mcp` standard-library logger as well as the `MCPAnalyticsOptions(logger=...)` sink, so they are visible without opting in — silence them with `logging.getLogger("posthog.mcp").setLevel(logging.ERROR)`. Neither fires for stdio, a correctly-wired server, a conversation-anchored session, or the SSE transport (which the mint cannot fix). Documented in the new `posthog/mcp/README.md`. — Thanks @posthog[bot]!

## 7.42.0 — 2026-08-21

### Minor changes

- [f483bab](https://github.com/posthog/posthog-python/commit/f483bab632a7970660d59aef58afa5bbfb576072) feat(mcp): capture `$mcp_client_user_agent` and `$mcp_vendor_client` so MCP usage can be attributed to a product surface. `clientInfo.name` only says which client *library* is calling — Anthropic reports `claude-code` from the CLI, the Agent SDK, the VS Code extension and the desktop app alike — so `$mcp_client_name` collapses every surface into one bucket and the harness breakdown reads 100% "Other" for Python-backed servers. The distinguishing detail lives in the User-Agent parenthetical (`claude-code/2.1.0 (cli)` vs `(sdk-ts)`) and in vendor headers like `x-anthropic-client`. Both are captured raw and classified at query time, so labels can improve without an SDK release. HTTP transports only: stdio and in-memory servers carry no headers and their events are unchanged. Custom dispatchers pass their own via new `client_user_agent` / `vendor_client` arguments on every `PostHogMCP.capture_*` method. Parity with `@posthog/mcp`. — Thanks @gesh!

## 7.41.0 — 2026-08-21

### Minor changes

- [2863909](https://github.com/posthog/posthog-python/commit/28639097d8a11a32863b6d4bd32a153c8c4567ae) feat(mcp): emit `$mcp_error_message` and `$mcp_error_type` on failed MCP events. The reason a tool call failed previously lived only on the sibling `$exception` event, so PostHog's failures view — which reads the scalars off the primary event — showed empty error rows for every Python-backed MCP server, and switching off `enable_exception_autocapture` removed the reason entirely. Both values are read from the same `$exception_list` the sibling carries, so the two surfaces can never disagree, and the message inherits the existing 2048-character cap. `PostHogMCP.capture_tool_call()` and `capture_tools_list()` take a new optional `error_type` for custom dispatchers that want a coarse category (`"validation"`, `"timeout"`) instead of the thrown class name. Exception messages are also redacted before they leave — previously nothing sanitized the error payload, so the `$exception` sibling had been shipping them raw. Credential-looking words go through the SDK's own detector (entropy, known key formats, PEM markers), per word, so a message like `auth failed for sk-...` keeps its diagnostic text and loses only the key. Parity with `@posthog/mcp`, which sanitizes exception values the same way. — Thanks @gesh!

## 7.40.0 — 2026-08-21

### Minor changes

- [b0ab12c](https://github.com/posthog/posthog-python/commit/b0ab12c93fa72098d8e9989dfb30de58671b88ed) feat(mcp): support MCP Python SDK v2 and bring `posthog.mcp` to parity with the TypeScript SDK (`@posthog/mcp`). **Most of this reaches SDK 1.x servers too** — the parity work is not v2-only.
  
  **MCP SDK v2 / spec 2026-07-28.** `instrument()` now wraps `mcp.server.mcpserver.MCPServer` (the renamed FastMCP) and the v2 low-level `Server` (constructor-injected handlers, string-keyed registry, late `add_request_handler` registrations included), capturing tool calls, tools/list, errors, intent, client identity, and `$mcp_protocol_version` on both protocol eras — the legacy handshake and the stateless 2026-07-28 envelope, decided per request. Previously `instrument()` raised `ImportError` on `mcp>=2` and took the host application down with it; it now degrades to a logged no-op on any unsupported or unrecognized SDK.
  
  **Cross-SDK parity (SDK 1.x and 2.x alike).** Conversation-anchored sessions land as the cross-pod correlation the stateless era needs: with `enable_conversation_id`, `$session_id` derives deterministically from the agent-echoed `conversation_id` (new export `derive_session_id_from_conversation`, byte-compatible with `@posthog/mcp`). Only a handle the SDK could have minted (a uuidv7) anchors a session, so two callers inventing the same id can no longer be merged. The handle is delivered over both channels a tool result has — a `content` text block carrying it as plain JSON data on the minting response (an imperative server sentence inside a tool result is prompt-injection-shaped, and a client that strips it silently breaks the feature), and an `_mcp_instructions` key declared on the tool's output schema and mirrored into `structuredContent` on every response. That second channel is what makes the feature work at all for tools with structured output: clients that read `structuredContent` never render `content`, so the agent had no handle to echo (0% echo rate measured against Claude Code before the mirror). The prompt-back now rides errored results too, so a failure on a conversation's first call doesn't split the retry into a new session. The session is resolved only once the handle's fate is known, so the call that mints a handle joins the same session as the calls that echo it — while a handle that could not be delivered anchors nothing, rather than stranding events in a conversation nobody holds. Host callbacks (`identify`, `intent_fallback`, `event_properties`) receive the SDK's own per-request context as `extra["ctx"]` identically on both majors, with a new exported `get_request_headers(extra)` to read HTTP headers off it — the underlying shape differs per major, and a hand-rolled read that works on one silently returns nothing on the other, sending every event out anonymous.
  
  **Fixes affecting existing SDK 1.x users.** Analytics could break a tool call in three ways, each now fixed and regression-tested: the SDK's tool cache is rebuilt from an internal listing pass we skipped injecting on, so after any call to an unlisted tool name a strict schema rejected either the analytics parameters we advertise (`Input validation error`) or the conversation key we write (`Output validation error`); the conversation handle was written into the caller's result object in place, so a tool returning a shared or cached result served one conversation's handle to every later caller; and on jlowin's FastMCP the advertised schema marked `context` required while the adapter strips it before validation, failing every call under `strict_input_validation=True`. Two behavioural changes come with the parity work: an invented (non-uuidv7) `conversation_id` echo is replaced with a fresh handle rather than trusted, and minted prompt-backs are now appended to errored results. — Thanks @gesh!

## 7.39.2 — 2026-08-20

### Patch changes

- [1adf542](https://github.com/posthog/posthog-python/commit/1adf542b84000fd296c2da05fe43e548bd146d1b) Drop events when before_send callbacks raise exceptions — Thanks @marandaneto!

## 7.39.1 — 2026-08-14

### Patch changes

- [6fc55b6](https://github.com/posthog/posthog-python/commit/6fc55b6b75b208ef7910b2dcb8a62fcdeda61b46) Normalize SDK event timestamps to UTC, including datetime values and parseable ISO timestamp strings, and correct UTC serialization for exception frame timestamps — Thanks @marandaneto!

## 7.39.0 — 2026-08-13

### Minor changes

- [178ef43](https://github.com/posthog/posthog-python/commit/178ef43f8311aee8fd068e1f64d6d1cdd5832281) Public beta `capture_ai`: AI events on the dedicated AI endpoint with the event UUID returned; new `enable_full_ai_capture` flag (old private flags kept as deprecated aliases). — Thanks @carlos-marchal-ph!

## 7.38.6 — 2026-08-12

### Patch changes

- [9beed86](https://github.com/posthog/posthog-python/commit/9beed863426e50a4da1e95b4df645849b96960d5) fix: preserve event delivery when gevent monkey-patches `queue.Queue`, including in preloaded gunicorn workers — Thanks @marandaneto!

## 7.38.5 — 2026-08-12

### Patch changes

- [9c4fd84](https://github.com/posthog/posthog-python/commit/9c4fd8401d054137e646b93c91266e6bafa672f7) Fix async OpenAI streaming captures to include token usage and other generation properties emitted by synchronous streams. — Thanks @ckarnell for your first contribution 🎉!

## 7.38.4 — 2026-08-10

### Patch changes

- [f38790c](https://github.com/posthog/posthog-python/commit/f38790c867f7a504babaa22711d80f92cf9e212b) Fix local evaluation for negated, missing, and malformed cohort definitions — Thanks @marandaneto!

## 7.38.3 — 2026-08-07

### Patch changes

- [bd5cff4](https://github.com/posthog/posthog-python/commit/bd5cff4b557cfa9be092158c7c020744834ac3e3) fix: declare Gemini's cache accounting model on generations with cache reads, so ingestion prices cached tokens from `$ai_cache_reporting_exclusive` instead of inferring it from the token counts. — Thanks @fivestarspicy!

## 7.38.2 — 2026-08-07

### Patch changes

- [100f993](https://github.com/posthog/posthog-python/commit/100f993aba0ab740e55494d3683c3bede0510863) `group_identify()` now validates the group identity before enqueuing. Previously `group_identify("company", None)` (or an empty-string `group_type` / `group_key`) sent a `$groupidentify` event with a null/empty `$group_type` or `$group_key`, which cannot address a group profile and just adds an unusable event to the project. Missing values are now dropped with a warning instead, matching the sdk-specs `group-identify` contract. Valid values, including non-string group keys, are passed through unchanged. — Thanks @posthog[bot]!

## 7.38.1 — 2026-08-06

### Patch changes

- [55370ee](https://github.com/posthog/posthog-python/commit/55370ee0b0160513baf79b56d3475449d33f6c66) fix: prevent client lifecycle deadlocks when error callbacks, concurrent `join()`/`shutdown()` calls, or forked sync-mode clients interact with queue and worker teardown. — Thanks @marandaneto!

## 7.38.0 — 2026-08-05

### Minor changes

- [77821ce](https://github.com/posthog/posthog-python/commit/77821ce5c106f489bac5afedccebb40ecd033e0b) feat: `FeatureFlagEvaluations.is_enabled()` accepts a `default_value` returned when the flag has no value in the evaluation — the key was not part of the evaluated set, or the evaluation came back empty (failed `/flags` request, quota limit, no resolvable `distinct_id`). A flag that has a value still wins, so a disabled flag returns `False` even with `default_value=True`. The default is `False`, so existing calls behave exactly as before. — Thanks @posthog[bot]!

### Patch changes

- [7b6a8d8](https://github.com/posthog/posthog-python/commit/7b6a8d8e73db82b08c3e4e4a21ee99b488801037) `evaluate_flags()` now JSON-decodes payloads for locally-evaluated flags, the same way it already did for flags resolved remotely. Previously `get_flag_payload()` returned a parsed value (`{"copy": "new"}`) when the flag came back from `/flags` but the raw JSON string (`'{"copy": "new"}'`) when the poller evaluated it locally, so the payload's type depended on where the flag happened to resolve. The `$feature_flag_payload` property on `$feature_flag_called` events is decoded for locally-evaluated flags too. Payload strings that aren't valid JSON are still passed through unchanged. — Thanks @posthog[bot]!
- [92625cf](https://github.com/posthog/posthog-python/commit/92625cfb766e31f24e70ec93d4b63d922bb6ebf3) The `$feature_flag_called` dedupe tracker now evicts its oldest entry when it reaches capacity instead of clearing every entry. Previously, each time a client accumulated 50,000 distinct IDs the whole tracker was wiped, so the next flag read for every previously seen distinct ID re-emitted a `$feature_flag_called` event it had already deduped. — Thanks @posthog[bot]!

## 7.37.6 — 2026-08-05

### Patch changes

- [c5f4e8f](https://github.com/posthog/posthog-python/commit/c5f4e8f034a5648f663bffa6cbde2a5d0786309b) Normalize Gemini tool calls and tool responses in captured input so they render in traces and reach evaluations — Thanks @marco-g-pm!

## 7.37.5 — 2026-08-05

### Patch changes

- [ae26014](https://github.com/posthog/posthog-python/commit/ae260147a1300ad806152df9b48015d07f96d09f) fix: `flush()` no longer waits out `flush_interval` before delivering a partial batch. A consumer holding fewer than `flush_at` events now sends them as soon as `flush()` (or `shutdown()`) asks it to, instead of blocking the caller for the rest of the batching window — which previously made `flush()` deliver nothing at all when `flush_interval` was longer than the flush timeout. Timer-based batching without an explicit flush is unchanged. — Thanks @posthog[bot]!

## 7.37.4 — 2026-08-05

### Patch changes

- [6397d78](https://github.com/posthog/posthog-python/commit/6397d7805588bc17856f8612d6f2076deb93d64c) `alias()` now validates both identities before enqueuing. Previously `alias(None, "user-123")` (or an empty-string `previous_id`) sent a `$create_alias` event with a null/empty `distinct_id`, which cannot link anything and just adds an unusable event to the project. Missing identities are now dropped with a warning instead, matching the sdk-specs `alias` contract. The drop that already happened when no alias target could be resolved now logs a warning too, and a non-string `previous_id` such as `0` is stringified consistently in both `distinct_id` and `properties.distinct_id`. — Thanks @posthog[bot]!

## 7.37.3 — 2026-08-04

### Patch changes

- [5ed7d0d](https://github.com/posthog/posthog-python/commit/5ed7d0d12fb2b18eb341c407e109f51ff31b75e4) Prevent stale feature flag definition publication — Thanks @marandaneto!

## 7.37.2 — 2026-08-04

### Patch changes

- [b16ec74](https://github.com/posthog/posthog-python/commit/b16ec7450d9c5edcce576ab0c73ce318ac0afeec) Isolate MCP pending capture tasks by owner and loop — Thanks @marandaneto!

## 7.37.1 — 2026-08-04

### Patch changes

- [38a09b8](https://github.com/posthog/posthog-python/commit/38a09b8d39646ac03505b54ce7e4fe735901ac82) Preserve typed feature flag results in Redis fallback — Thanks @marandaneto!
- [25b9d28](https://github.com/posthog/posthog-python/commit/25b9d287b0226b4252a9846eaf4404156c6c268c) Preserve Anthropic messages.stream compatibility — Thanks @marandaneto!
- [f70602b](https://github.com/posthog/posthog-python/commit/f70602b9060451ac5ecd7389e0ea913d20c353c0) Honor false feature flag payload overrides — Thanks @marandaneto!
- [b094725](https://github.com/posthog/posthog-python/commit/b094725a5eaee9ae3681dccdd0e682f47fc853f6) Use device IDs during local feature flag evaluation — Thanks @marandaneto!

## 7.37.0 — 2026-08-03

### Minor changes

- [c5d01c9](https://github.com/posthog/posthog-python/commit/c5d01c9b0cdec50d2ffb6ee363db49e4831ede72) Support the `starts_with`, `not_starts_with`, `ends_with`, and `not_ends_with` property filter operators in feature flag local evaluation. Matching is case-insensitive and mirrors `icontains`, so flags using these operators no longer fall back to remote evaluation. — Thanks @haacked!

## 7.36.0 — 2026-08-03

### Minor changes

- [f805e5b](https://github.com/posthog/posthog-python/commit/f805e5b3d7046ee996017f7d913b54937ede49f2) `posthog.ai.openai.OpenAI` / `AsyncOpenAI` now accept a per-call `posthog_provider_override` argument. The wrapper is commonly pointed at OpenAI-compatible endpoints (DeepSeek, Groq, Mistral, Together, Fireworks, xAI, Perplexity, Ollama, Cerebras, and various gateways) via a custom `base_url`, but always reported `$ai_provider: "openai"`, which breaks PostHog's cost attribution for those calls. Passing `posthog_provider_override="deepseek"` (for example) sets `$ai_provider` on the emitted event without changing how the OpenAI-shaped response is parsed. Omitting it leaves `$ai_provider` as `"openai"`, exactly as before. Covers chat completions, the Responses API, `.parse()`, and embeddings, across sync, async, and streaming calls. — Thanks @marco-g-pm!

## 7.35.5 — 2026-08-03

### Patch changes

- [340eb2a](https://github.com/posthog/posthog-python/commit/340eb2adb49bd4449253db9e2161363cfe1bee72) Reset PostHog context after fork. Forked children no longer retain the parent process's active lexical context; they start without inherited context and can establish a new child-local context. — Thanks @marandaneto!
- [c95c9f9](https://github.com/posthog/posthog-python/commit/c95c9f91228c1bbd0bebbedb62e236c66d105d11) Make client shutdown an atomic terminal boundary — Thanks @marandaneto!
- [c2b0972](https://github.com/posthog/posthog-python/commit/c2b09723ecf824060a8e1a08ecdd0e3a227a3aee) Respect Celery task filters for exception capture — Thanks @marandaneto!

## 7.35.4 — 2026-07-31

### Patch changes

- [60a3e9c](https://github.com/posthog/posthog-python/commit/60a3e9ce987c1eeda64b9905e5bdf9124a670c8f) Reset the client registry lock after fork — Thanks @marandaneto!

## 7.35.3 — 2026-07-31

### Patch changes

- [0b353a7](https://github.com/posthog/posthog-python/commit/0b353a70c5cfedb04481354bc45445583f602157) Keep consumers alive after malformed before_send results — Thanks @marandaneto!
- [3658ed1](https://github.com/posthog/posthog-python/commit/3658ed1a679315ebebb4ce35319f981fb21909c1) Cap capture v0 Retry-After delays — Thanks @marandaneto!
- [1b30afa](https://github.com/posthog/posthog-python/commit/1b30afa1b400d137adb30628a015f18021571d7b) Reject negative capture retry counts — Thanks @marandaneto!

## 7.35.2 — 2026-07-31

### Patch changes

- [aa00432](https://github.com/posthog/posthog-python/commit/aa00432e97fb5e270ac0ae4917b763a095c1496c) Restore exception hooks safely — Thanks @marandaneto!

## 7.35.1 — 2026-07-31

### Patch changes

- [bfec2b1](https://github.com/posthog/posthog-python/commit/bfec2b16cd80ce0588d95beee1e24eb757defc35) Reset MCP background capture state after fork — Thanks @marandaneto!

## 7.35.0 — 2026-07-30

### Minor changes

- [4bf123e](https://github.com/posthog/posthog-python/commit/4bf123e08f838280f70d0c6fef9c76057a531b15) The OpenAI Agents SDK `group_id` now also maps to `$ai_session_id` on `$ai_trace` and span events, so grouped runs show up as sessions in PostHog AI observability. `$ai_group_id` is still emitted alongside it. — Thanks @marco-g-pm for your first contribution 🎉!

## 7.34.0 — 2026-07-30

### Minor changes

- [3c9aa59](https://github.com/posthog/posthog-python/commit/3c9aa59d1f2059e04bb802e93173cb7372dbbd0e) feat(ai): `Prompts.get(..., with_metadata=True)` results now include `config`, the JSON object of model parameters or agent configuration stored with the prompt version in PostHog prompt management (`None` when the version has none). Config is carried through the client-side cache and the stale-cache fallback. The hardcoded `fallback` string has no config, so use defensive access like `(result.config or {}).get("temperature", 0)`. — Thanks @jurajmajerik!

## 7.33.0 — 2026-07-29

### Minor changes

- [170f4e2](https://github.com/posthog/posthog-python/commit/170f4e276fbfc71468cbc2ffcea45eb63637ca8b) feat(mcp): emit `$mcp_protocol_version` on MCP analytics events — the MCP spec version, recovered from the session token across stateless pods (parity with the TypeScript SDK). `PostHogMCP` capture methods gain a `protocol_version` argument. — Thanks @gesh for your first contribution 🎉!

## 7.32.0 — 2026-07-28

### Minor changes

- [cdc0825](https://github.com/posthog/posthog-python/commit/cdc08251ccf36f7630a91494b16241d0b4803332) Preserve Anthropic cache-write TTL breakdowns across Python SDK AI integrations. — Thanks @gouveags!

## 7.31.1 — 2026-07-28

### Patch changes

- [13de879](https://github.com/posthog/posthog-python/commit/13de87992eb5c42b1b400e285e29f9e294349745) Fix module-level settings propagation to the default client — Thanks @marandaneto!

## 7.31.0 — 2026-07-27

### Minor changes

- [5535ecd](https://github.com/posthog/posthog-python/commit/5535ecd4fa9cb739347baae869490fc18b6505c4) fix(errors): emit `$exception_list` in canonical order — index `0` is the caught/outermost exception, causes follow in unwrap order, and the root cause is last (previously the list was reversed with the root cause first). This aligns posthog-python with the cross-SDK exception ordering spec. Frame order within each stacktrace is unchanged. — Thanks @cat-ph!

## 7.30.1 — 2026-07-27

### Patch changes

- [4c8a85a](https://github.com/posthog/posthog-python/commit/4c8a85a75e1f5473133dc93a99e0ec6dac80d3f6) AI capture now records multimodal and structured content (thinking blocks, tool calls, media, and Responses API output items) faithfully across all providers and streaming paths, and redacts base64 media structurally without leaking raw bytes or over-redacting legitimate values. — Thanks @carlos-marchal-ph!

## 7.30.0 — 2026-07-27

### Minor changes

- [37aafd3](https://github.com/posthog/posthog-python/commit/37aafd3154e94450ffb3afa589f3d96472d3b5bc) feat(mcp): stateless and multi-pod server support — carry `$session_id` and the client identity (harness) across pods via a self-encoded `Mcp-Session-Id` token minted at `initialize` and replayed on every request. Auto-wired on the `instrument()` FastMCP path (`stateless_http=True`); custom `PostHogMCP` dispatchers add `PostHogMcpStatelessSessionMiddleware` and read `get_mcp_session()`. — Thanks @gesh!

## 7.29.0 — 2026-07-23

### Minor changes

- [f9a163c](https://github.com/posthog/posthog-python/commit/f9a163c2c8793798eb696ffa38231e0c7a5a892d) Refactored capture internals to support multiple delivery lanes per client. Added an internal test lane for heavy AI events.
  
  Events captured after `shutdown()` are now dropped with a warning instead of being silently queued with no consumer to deliver them. — Thanks @carlos-marchal-ph!

## 7.28.0 — 2026-07-21

### Minor changes

- [2d7f8cc](https://github.com/posthog/posthog-python/commit/2d7f8cc1e2443794d4c375003fcd22136509b430) The `client.metrics` config can now be set through module-level settings: assign `posthog.metrics = {"service_name": ..., ...}` alongside `posthog.api_key` and the dict is applied when `setup()` builds the global client. Previously module-configured apps had no way to pass the metrics config, so every series recorded through the global client shipped `service.name='unknown_service'`. Late assignment (e.g. a Django `ready()` hook running after an early `setup()`) still applies on the next `setup()` call, as long as the metrics API hasn't been used yet. — Thanks @DanielVisca!

### Patch changes

- [6766309](https://github.com/posthog/posthog-python/commit/67663099b27da7d1ae800d4129da2908f034daa6) Harden the alpha `posthog.metrics` client based on review follow-ups.
  
  - Metric attributes are now deep-snapshotted at capture time, so mutating a nested list/dict value after `count()`/`gauge()`/`histogram()` can no longer rewrite an already-recorded series' attributes on the wire.
  - Failed metric flushes now retry with exponential backoff (first retry at the base interval, then doubling per consecutive failure, capped at 64x the flush interval — the shared JS logs ramp) instead of the fixed cadence, and the buffered window is dropped loudly after 8 consecutive failed flushes — previously documented as 3 but effectively 4.
  - Invalid `metrics` client config (non-dict config or `resource_attributes`, non-numeric `flush_interval`, non-integer `max_series_per_flush`, non-callable `before_send`) now degrades to defaults with a warning instead of raising from the first `client.metrics.count()` call, matching the client's no-throw contract. — Thanks @DanielVisca!

## 7.27.1 — 2026-07-21

### Patch changes

- [ca5e883](https://github.com/posthog/posthog-python/commit/ca5e883d61384c9087bb7b9331072e8c098ce6bb) Clarify the queue-full warning to say the event is being dropped, instead of only reporting that the queue is full. — Thanks @emmayusufu for your first contribution 🎉!

## 7.27.0 — 2026-07-18

### Minor changes

- [5ef2c23](https://github.com/posthog/posthog-python/commit/5ef2c239879424717b4da8237e918c4bc9f9fcc1) `$feature_flag_called` events are now minimized for non-experiment flags when the server enables it. When the `/flags` v2 response (`minimalFlagCalledEvents`) or the local-evaluation payload (`minimal_flag_called_events`) reports the gate as enabled and the evaluated flag has no linked experiment (`has_experiment` is `false`), the event's properties are reduced to a strict allowlist (`$feature_flag`, `$feature_flag_response`, `$feature_flag_has_experiment`, the `$feature_flag_*` debug scalars, `locally_evaluated`, `$groups`, `$process_person_profile`, `$session_id`, `$lib`, `$lib_version`, `$is_server`, `$geoip_disable`, `$os`, `$os_version`, `$os_distro`, `$python_runtime`, `$python_version`). Everything else — including super properties and custom event properties — is stripped from those events.
  
  If the server does not report the gate, if the flag's `has_experiment` signal is missing, or if the flag is linked to an experiment, the full property set is sent unchanged. There is no SDK-side configuration; the gate is controlled per-team by the server. For `evaluate_flags()` snapshots, the gate is pinned when the snapshot is created, so deferred flag accesses are shaped by the evaluation that produced them.
  
  Custom `flag_definition_cache` providers now receive an additional `minimal_flag_called_events` key in the definitions payload, so the gate survives external cache round-trips.
  
  When the server reports `has_experiment` for a flag, every `$feature_flag_called` event also carries a `$feature_flag_has_experiment` boolean property. — Thanks @haacked!

## 7.26.0 — 2026-07-17

### Minor changes

- [1653bcb](https://github.com/posthog/posthog-python/commit/1653bcb7e96eee616ce7f72ffb98a9609f06ca9c) Add a `label` option to `Prompts.get()` to fetch the prompt version a label (e.g. `production`) currently points to. Labeled fetches are cached separately, and `PromptResult` carries the resolved `label`. Requires a PostHog version with prompt labels; older servers ignore the parameter and return the latest version. — Thanks @jurajmajerik!

## 7.25.0 — 2026-07-16

### Minor changes

- [5ab6318](https://github.com/posthog/posthog-python/commit/5ab6318d0fe71cfead25a6baf4d6a74704379603) Add the active OpenTelemetry span's `$trace_id` and `$span_id` to events captured with `capture_exception`. — Thanks @hpouillot!

## 7.24.0 — 2026-07-15

### Minor changes

- [556c134](https://github.com/posthog/posthog-python/commit/556c134b9e6e017c7f4a5deeebc13a09b18f45d4) `$feature_flag_called` events now carry a `$feature_flag_has_experiment` boolean property when the server reports whether the flag is linked to an experiment. When the server does not report the signal (older deployments), the property is omitted. — Thanks @haacked!

## 7.23.0 — 2026-07-15

### Minor changes

- [5e42b1e](https://github.com/posthog/posthog-python/commit/5e42b1e2dc5d6a25e364417f6f6a9f13449991a6) Add the `posthog.metrics` API (`count`, `gauge`, `histogram`) — alpha.
  
  Backend services can now record metrics through the same statsd-style pre-aggregating client the browser SDK ships, with no OpenTelemetry setup:
  
  ```python
  client = Posthog("<ph_project_api_key>", metrics={"service_name": "billing-worker"})
  client.metrics.count("invoices.processed", 1, attributes={"plan": "pro"})
  client.metrics.gauge("queue.depth", 42)
  client.metrics.histogram("job.duration", 187, unit="ms")
  ```
  
  Samples aggregate in memory and flush as OTLP/JSON to `/i/v1/metrics` (one data point per series per window, delta temporality). Pending metrics are flushed on `shutdown()`; buffered windows are retried on transient failures and dropped loudly after 3 consecutive failed flushes. The `metrics` client option accepts `service_name`, `service_version`, `environment`, `resource_attributes`, `flush_interval` (seconds), `max_series_per_flush` (cardinality guardrail, default 1000), and a `before_send` hook. — Thanks @DanielVisca!

## 7.22.4 — 2026-07-14

### Patch changes

- [eb025c8](https://github.com/posthog/posthog-python/commit/eb025c81bed39d8aeff6698879a37ac4e895eb1b) Django middleware also sends the request user agent as `$raw_user_agent`, the standardized property PostHog's server-side classification (e.g. bot detection) reads — Thanks @lricoy!

## 7.22.3 — 2026-07-14

### Patch changes

- [ae3c4e5](https://github.com/posthog/posthog-python/commit/ae3c4e5d53741b2a895c1b3759d14f92f27259b7) Malformed flag-dependency conditions (missing key, null value, or wrong operator) now evaluate locally as no-match (false), matching the server, instead of falling back to the `/flags` endpoint on every evaluation. 7.22.1 made these conditions fall back to the server, which could massively increase billable `/flags` request volume for flag definitions containing legacy/malformed dependency conditions. — Thanks @patricio-posthog!

## 7.22.2 — 2026-07-13

### Patch changes

- [4d61b18](https://github.com/posthog/posthog-python/commit/4d61b18415b752eefb935d42121708578f9c2575) Capture pre-calculated total cost from OpenAI Agents Responses API usage. — Thanks @fuchengwarrenzhu for your first contribution 🎉!

## 7.22.1 — 2026-07-10

### Patch changes

- [650d107](https://github.com/posthog/posthog-python/commit/650d107665207e5239b8613e19bcba76695076ee) Fix local evaluation of flag dependencies with a `flag_evaluates_to: false` condition: such conditions never matched, forcing the dependent flag to `false` for every locally-evaluated user. — Thanks @matheus-vb!

## 7.22.0 — 2026-07-06

### Minor changes

- [d459b57](https://github.com/posthog/posthog-python/commit/d459b5710bcc1333ef49f89da681b9bf2aac9109) Add an opt-in `capture_mode` for the Capture V1 ingestion protocol (`POST /i/v1/analytics/events`). Set `capture_mode="v1"` on the client (or the `POSTHOG_CAPTURE_MODE=v1` environment variable) to use Bearer auth, per-event results, and partial retry. Defaults to `"v0"` (the legacy `/batch/` endpoint), so existing setups are unaffected.
  
  When using `capture_mode="v1"`, request bodies can be compressed via `capture_compression` (or `POSTHOG_CAPTURE_COMPRESSION`): `"gzip"`, `"deflate"`, `"zstd"` (requires the optional `posthog[zstd]` extra), or `"none"` (default). The legacy `gzip=True` flag is honored as a fallback.
  
  Per-event server verdicts are surfaced through the existing `on_error` handler: events the backend explicitly drops, or fails to accept after retries, raise a `CaptureV1Error` carrying the affected event UUIDs — so a rejection is never silently lost, even when the HTTP request itself succeeded. — Thanks @eli-r-ph for your first contribution 🎉!

## 7.21.3 — 2026-07-02

### Patch changes

- [30c184f](https://github.com/posthog/posthog-python/commit/30c184f4c8a9fe390f621d529fffe1ce533277e8) Stop duplicating distinct_id inside /flags person properties — Thanks @marandaneto!

## 7.21.2 — 2026-07-01

### Patch changes

- [c6350b6](https://github.com/posthog/posthog-python/commit/c6350b69c6eb5fd6489460586f052738cbc4a420) Testing release workflow. — Thanks @marandaneto!

## 7.21.1 — 2026-06-29

### Patch changes

- [cd86110](https://github.com/posthog/posthog-python/commit/cd86110e9e3c86f988a32277a0139a71e2512459) Fall back to uncompressed uploads when gzip compression fails — Thanks @marandaneto!

## 7.21.0 — 2026-06-26

### Minor changes

- [888a725](https://github.com/posthog/posthog-python/commit/888a7251f412328d59c9fc35c4c9b4b0c10fa55d) Add `posthog.mcp`, a Python SDK for PostHog MCP analytics (just `pip install posthog`; the MCP SDK is a peer dependency of `instrument()`, not bundled). `instrument(server, posthog_client)` wraps a `FastMCP` or low-level `mcp.server.Server` so every tool call, agent intent, tools/list, initialize, and failure is captured to PostHog as a `$mcp_*` event. Also adds `PostHogMCP`, a `Client` subclass for custom dispatchers (needs nothing beyond posthog), plus opt-in `context` intent capture, `identify`, `report_missing` (`get_more_tools`), and `conversation_id`. Beta. — Thanks @lucasheriques for your first contribution 🎉!

## 7.20.5 — 2026-06-24

### Patch changes

- [cdd878c](https://github.com/posthog/posthog-python/commit/cdd878c88d198efffd80ea853525589f2ec9ab30) Clear feature flag called cache on shutdown — Thanks @marandaneto!

## 7.20.4 — 2026-06-24

### Patch changes

- [89adb2f](https://github.com/posthog/posthog-python/commit/89adb2f997ba2968b25a49eb29abc6d6d3bb19f8) Fix internal imports for posthoganalytics mirror — Thanks @hpouillot!

## 7.20.3 — 2026-06-23

### Patch changes

- [42ff4ca](https://github.com/posthog/posthog-python/commit/42ff4ca6abdc11579806915d8a41ceba8d452969) Detect and redact high-entropy secrets (API keys, tokens, passwords) in exception code variables. Adds the `code_variables_detect_secrets` option (default `True`). — Thanks @ablaszkiewicz!

## 7.20.2 — 2026-06-22

### Patch changes

- [c359f93](https://github.com/posthog/posthog-python/commit/c359f939e4ca6bf7d591f62400adf3750983c3b6) Mask sensitive data held inside objects and in URL/DSN credentials when capturing exception code variables. Custom objects are now traversed so fields like `password` are redacted by attribute name instead of leaking via `repr()`, and credentials embedded in connection strings are scrubbed. Adds the `code_variables_mask_url_credentials` option (default `True`). — Thanks @ablaszkiewicz!
- [c359f93](https://github.com/posthog/posthog-python/commit/c359f939e4ca6bf7d591f62400adf3750983c3b6) Improve strict Pyright coverage for public PostHog APIs. — Thanks @ablaszkiewicz!

## 7.20.1 — 2026-06-22

### Patch changes

- [09c8fba](https://github.com/posthog/posthog-python/commit/09c8fba8096a1edf5dc125fe10fe3f447975fec9) Warn on duplicate async PostHog clients and document client lifecycle guidance — Thanks @marandaneto!

## 7.20.0 — 2026-06-22

### Minor changes

- [bc8e531](https://github.com/posthog/posthog-python/commit/bc8e5316e59006477ee4ac63407a9fc7aed2874a) Add a default timeout for flushing queued events. — Thanks @marandaneto!

## 7.19.2 — 2026-06-17

### Patch changes

- [98a305a](https://github.com/posthog/posthog-python/commit/98a305aec9601c84120b17c51ec467eab037aab3) Increase the default background flush interval to 5 seconds — Thanks @marandaneto!

## 7.19.1 — 2026-06-15

### Patch changes

- [8d416ae](https://github.com/posthog/posthog-python/commit/8d416aef20f12c0c8c405baf7c7d08f3352f3632) Add missing return type annotations to improve typing coverage without changing runtime behavior. — Thanks @miachillgood for your first contribution 🎉!

## 7.19.0 — 2026-06-15

### Minor changes

- [b9f3208](https://github.com/posthog/posthog-python/commit/b9f320808f44832fc3b4cdd6bcf0e0aad194b76d) Add opt-in client-side rate limiting for exception autocapture, using the same token bucket algorithm as the posthog-js and posthog-node SDKs: a bucket per exception type allows a burst of captures, then refills over time. Rate-limited exceptions are skipped before they reach the ingestion queue. Disabled by default; enable with the new `enable_exception_autocapture_rate_limiting` client option and tune via `exception_autocapture_bucket_size` (default 50), `exception_autocapture_refill_rate` (default 10), and `exception_autocapture_refill_interval_seconds` (default 10). — Thanks @hpouillot!

## 7.18.3 — 2026-06-12

### Patch changes

- [ee6a3c8](https://github.com/posthog/posthog-python/commit/ee6a3c8c030cfaacc065765d95991a448d59ad08) Warn when an AI wrapper's `base_url` points at the PostHog AI Gateway. The gateway emits its own `$ai_generation`, so each call would be captured (and billed) twice. The wrapper only warns and never drops the event. Detection covers the wrapper funnels (OpenAI, Anthropic, LangChain) and the OTel span path. — Thanks @richardsolomou!

## 7.18.2 — 2026-06-12

### Patch changes

- [fe76fc9](https://github.com/posthog/posthog-python/commit/fe76fc9a874a3f2f8538ee0d1c4f8a6c66f11c03) Improve mypy coverage for core SDK modules without changing runtime behavior. — Thanks @Kshitijmishradev for your first contribution 🎉!

## 7.18.1 — 2026-06-10

### Patch changes

- [00b2091](https://github.com/posthog/posthog-python/commit/00b2091ea661d3b4e87ccf11f4752f73a1e0d162) Add internal-only routing of `$ai_*` events to a dedicated capture endpoint in their own batch, gated behind the unstable `_dedicated_ai_endpoint` client option (off by default, not for general use). — Thanks @carlos-marchal-ph!

## 7.18.0 — 2026-06-05

### Minor changes

- [a2ce51e](https://github.com/posthog/posthog-python/commit/a2ce51e8a4aad82bb91f152ee8b4236ba5898472) feat(feature-flags): support the `early_exit` condition option in local evaluation. When a flag enables early exit, evaluation now stops and returns `False` as soon as a condition group's property filters match but the rollout percentage excludes the user, instead of falling through to later groups — matching the server-side evaluation behavior. — Thanks @gustavohstrassburger!

## 7.17.0 — 2026-06-03

### Minor changes

- [3aed638](https://github.com/posthog/posthog-python/commit/3aed638ce1a256545f86cf05aa6338a1f2b62a89) Add a configurable `$is_server` event property (default `true`) so PostHog can identify server-side events. Set `is_server=False` when using posthog-python as a client/CLI so the device OS is attributed normally. — Thanks @turnipdabeets for your first contribution 🎉!

## 7.16.4 — 2026-06-03

### Patch changes

- [44e6b14](https://github.com/posthog/posthog-python/commit/44e6b14affd235f523d5a719690cddcb98f0fdde) Fix async streaming responses from the AI wrappers (OpenAI, Anthropic, Gemini) so they support `async with` as well as `async for`. Previously, consuming a stream via `async with` (e.g. with pydantic-ai) raised `TypeError: 'async_generator' object does not support the asynchronous context manager protocol`. — Thanks @turnipdabeets for your first contribution 🎉!

## 7.16.3 — 2026-06-01

### Patch changes

- [643a810](https://github.com/posthog/posthog-python/commit/643a810851719cfdbbdee7d76d1fb87e6ade45aa) Return empty flag defaults from Client flag helpers when the flags API fails. — Thanks @marandaneto!

## 7.16.2 — 2026-05-28

### Patch changes

- [034dce2](https://github.com/posthog/posthog-python/commit/034dce2fcad5276bcaacd4cc7b635b91c6920353) Make module-level setup no-op when API key is blank — Thanks @marandaneto!

## 7.16.1 — 2026-05-27

### Patch changes

- [8f6d6c8](https://github.com/posthog/posthog-python/commit/8f6d6c8ba4be612ae39273c5bd47acfc31396145) Include group context in the `$feature_flag_called` dedupe key so group-scoped flags fire a separate event for each group a user is evaluated under, instead of being dedup-ed against the first group context the same `(distinct_id, flag, response)` was seen under. — Thanks @gustavohstrassburger!

## 7.16.0 — 2026-05-27

### Minor changes

- [a44e0be](https://github.com/posthog/posthog-python/commit/a44e0bebb0220ed41d4d16620b9864f6c0732055) Add async flag definition cache providers — Thanks @dustinbyrne!

## 7.15.4 — 2026-05-25

### Patch changes

- [0207088](https://github.com/posthog/posthog-python/commit/0207088decedb404abfe44ebc0c4b9f1723687c9) Track OpenAI chat completions parse calls — Thanks @marandaneto!

## 7.15.3 — 2026-05-21

### Patch changes

- [be9b78b](https://github.com/posthog/posthog-python/commit/be9b78be370fcaabeda568a50c0d72fba752dcbe) Reject semver values with leading zeros in local flag evaluation. Per semver 2.0.0 §2, numeric identifiers must not include leading zeros — values like `1.07.3` are not valid semver and should not match targeting conditions. Both override values and flag values are now validated; invalid inputs raise `InconclusiveMatchError` so the condition does not match. — Thanks @dmarticus!

## 7.15.2 — 2026-05-21

### Patch changes

- [1574b1b](https://github.com/posthog/posthog-python/commit/1574b1be68202b831b9de28fa86e5f67dcf7b728) Fix OpenAI usage parsing when token detail fields are null — Thanks @michael-ciridae!

## 7.15.1 — 2026-05-21

### Patch changes

- [a098aa7](https://github.com/posthog/posthog-python/commit/a098aa71860138ca94109f6c72fa732fafb302a1) Fix Gemini web search extraction when response candidates are null. — Thanks @marandaneto!

## 7.15.0 — 2026-05-19

### Minor changes

- [52cd20e](https://github.com/posthog/posthog-python/commit/52cd20e1237c6dd7be7364b744bd16565794c8ab) feat: add Celery integration and improve PostHog client fork safety — Thanks @parinporecha!

## 7.14.2 — 2026-05-13

### Patch changes

- [44c1261](https://github.com/posthog/posthog-python/commit/44c1261ce17f07ce4ce430d1b188501a5a1e2f45) Fix scoped context support for async functions — Thanks @marandaneto!

## 7.14.1 — 2026-05-11

### Patch changes

- [f6c8ede](https://github.com/posthog/posthog-python/commit/f6c8ede23505e3e97ba14aebb8efd4f237cb1dca) fix: type warning on new_context — Thanks @itsaphel for your first contribution 🎉!

## 7.14.0 — 2026-05-01

### Minor changes

- [69dc2a8](https://github.com/posthog/posthog-python/commit/69dc2a871a8f93fe6bcd14269f5b17c5b48fc897) Add `evaluate_flags()` and a new `flags` option on `capture()` so a single `/flags` call can power both flag branching and event enrichment per request:

  ```python
  flags = posthog.evaluate_flags(distinct_id, person_properties={"plan": "enterprise"})
  if flags.is_enabled("new-dashboard"):
      render_new_dashboard()
  posthog.capture("page_viewed", distinct_id=distinct_id, flags=flags)
  ```

  The returned `FeatureFlagEvaluations` snapshot exposes `is_enabled()`, `get_flag()`, `get_flag_payload()` for branching and `only_accessed()` / `only([keys])` filter helpers. Pass `flag_keys=[...]` to `evaluate_flags()` to scope the underlying `/flags` request itself.

  Deprecates `feature_enabled()`, `get_feature_flag()`, `get_feature_flag_payload()`, and `capture(send_feature_flags=...)`. They continue to work but now emit a `DeprecationWarning` pointing at `evaluate_flags()`. Removal is planned for the next major version. — Thanks @dmarticus!

## 7.13.2 — 2026-04-30

### Patch changes

- [f4af88a](https://github.com/posthog/posthog-python/commit/f4af88a3c3e5a095366af8789aa6c649eaeb4fd9) Prevent flush from hanging after dropping oversized queued events. — Thanks @marandaneto!
- [6b3d1c7](https://github.com/posthog/posthog-python/commit/6b3d1c75780d10eb8aec2efede9d0d1de0adc889) Sanitize PostHog tracing headers extracted by Django middleware. — Thanks @dustinbyrne!
- [dea848f](https://github.com/posthog/posthog-python/commit/dea848fd60a8857d8087c8b5058b0a8d7965981d) Remove python-dateutil as a runtime dependency — Thanks @marandaneto!
- [a1c6640](https://github.com/posthog/posthog-python/commit/a1c6640d42924a006a3495033b55e4be5ed5ede7) Improve local feature flag authentication error messages. — Thanks @marandaneto!
- [8bdd3fa](https://github.com/posthog/posthog-python/commit/8bdd3fa126b29a366efbcf66060cb54e09010c05) Treat clients with an empty project API key as disabled no-ops. — Thanks @marandaneto!

## 7.13.1 — 2026-04-24

### Patch changes

- [0d36184](https://github.com/posthog/posthog-python/commit/0d361845acc18f660b6ca1f7a3a0ae1168339d2e) Support mixed user+group targeting in local flag evaluation. — Thanks @patricio-posthog!

## 7.13.0 — 2026-04-21

### Minor changes

- [12c38e7](https://github.com/posthog/posthog-python/commit/12c38e7a788c29a244b715c4f9965b1ac0bb4b3f) Add `capture_errors` option to `Prompts` that reports prompt fetch failures to PostHog error tracking via `capture_exception()` when enabled. — Thanks @andrewm4894!

### Patch changes

- [1b098e7](https://github.com/posthog/posthog-python/commit/1b098e7dc1b25b41ee35a2eef7469e71fe42b1fc) Trim surrounding whitespace from API keys and host config before using them. — Thanks @marandaneto!

## 7.12.0 — 2026-04-16

### Minor changes

- [220d9e8](https://github.com/posthog/posthog-python/commit/220d9e88877dee7eabd34fed68c2a4a65e6526a7) `Prompts.get()` now accepts `with_metadata=True` and returns a `PromptResult` dataclass containing `source` (`api`, `cache`, `stale_cache`, or `code_fallback`), `name`, and `version` alongside the prompt text. The previous plain-string return is deprecated and will be removed in a future major version. — Thanks @marandaneto!

## 7.11.2 — 2026-04-15

### Patch changes

- [f5a95b4](https://github.com/posthog/posthog-python/commit/f5a95b454ae7fd8bf46381b1c624df827903260d) feat(flags): switch local evaluation polling from `/api/feature_flag/local_evaluation` to `/flags/definitions` — Thanks @patricio-posthog!

## 7.11.1 — 2026-04-14

### Patch changes

- [c3f097f](https://github.com/posthog/posthog-python/commit/c3f097f72f5ef6c1ecd25ade7d3ba08e57765eaf) feat: Add os_distro information to events — Thanks @parinporecha!

## 7.11.0 — 2026-04-10

### Minor changes

- [b921fe3](https://github.com/posthog/posthog-python/commit/b921fe33a9115fbf5f5171b80e1deabffd3e66ca) Add Gemini `embed_content` tracking support for both sync and async clients — Thanks @carlos-marchal-ph!
- [44b92a8](https://github.com/posthog/posthog-python/commit/44b92a844a2d8170e5b2247e509279f4654c4ef6) feat(ai): add $ai_stop_reason extraction for all providers — Thanks @carlos-marchal-ph!

### Patch changes

- [7c5cad8](https://github.com/posthog/posthog-python/commit/7c5cad8fcf818c9b8b4f074876718b937f2f8072) fix: graceful fallback in claude_agent_sdk query wrapper when PostHog is not configured — Thanks @andrewm4894!

## 7.10.3 — 2026-04-08

### Patch changes

- [e22e893](https://github.com/posthog/posthog-python/commit/e22e893b236bf6af1cb8f6c18712727d24fe5c7e) fix: pass the module-level `posthog.before_send` callback into the lazily initialized default client — Thanks @marandaneto!

## 7.10.2 — 2026-04-08

### Patch changes

- [bae355c](https://github.com/posthog/posthog-python/commit/bae355cd787f4c1a119fd2b396ba444b1a218b6a) feat(flags): make local evaluation endpoint configurable via `POSTHOG_LOCAL_EVALUATION_ENDPOINT` env var with fallback to default endpoint — Thanks @patricio-posthog for your first contribution 🎉!

## 7.10.1 — 2026-04-08

### Patch changes

- [a5052b0](https://github.com/posthog/posthog-python/commit/a5052b089b106af5a2fa5236fcf1f4f84943f899) fix: Django middleware accidentally passed capture_exceptions as positional arg, setting fresh=True and resetting context state — Thanks @marandaneto!

## 7.10.0 — 2026-04-07

### Minor changes

- [d234b53](https://github.com/posthog/posthog-python/commit/d234b53ff9578648d3bdb70d54cde98cdb7d9c87) feat(ai): add Claude Agent SDK integration for LLM analytics — Thanks @andrewm4894!

### Patch changes

- [754c45f](https://github.com/posthog/posthog-python/commit/754c45fa024be3fdb1f1d1f312a94070786652b7) fix: propagate missing params in module-level wrapper functions (`distinct_id` for `group_identify`, `flag_keys_to_evaluate` for `get_all_flags`/`get_all_flags_and_payloads`) — Thanks @dustinbyrne!

## 7.9.12 — 2026-03-12

### Patch changes

- [1729be4](https://github.com/posthog/posthog-python/commit/1729be4b5ea87ebd361cc95ce44733b0db596e63) chore(flags): expose flag_definition_cache_provider — Thanks @matheus-vb for your first contribution 🎉!

## 7.9.11 — 2026-03-11

### Patch changes

- [4547810](https://github.com/posthog/posthog-python/commit/4547810669d3c01fa1e1ab7595c00bfc1357ebe1) chore(ci): fix release attribution — Thanks @Piccirello!

## 7.9.10 — 2026-03-10

### Patch changes

- [b48a7ac](https://github.com/posthog/posthog-python/commit/b48a7ac16112a3ae338b95194404710ca57bd75b) chore(ci): attribute release tag to GitHub App — Thanks @Piccirello!

## 7.9.9 — 2026-03-10

### Patch changes

- [591d3e0](https://github.com/posthog/posthog-python/commit/591d3e0ffe88aa7d8913bec9709dc0647b3a09bb) chore(ci): use signed commits when publishing release — Thanks @Piccirello!

## 7.9.8 — 2026-03-09

### Patch changes

- [11466c6](https://github.com/posthog/posthog-python/commit/11466c625e74864dcb75c951b8efbf87f5ec4c8b) feat(llma): support fetching versioned prompts from the prompts sdk — Thanks @Radu-Raicea!
- [535e9c5](https://github.com/posthog/posthog-python/commit/535e9c530336930d27ec5dfd6072c55369d36a8e) chore(llma): clean up prompt SDK review follow-ups — Thanks @Radu-Raicea!

## 7.9.7 — 2026-03-05

### Patch changes

- [b206669](https://github.com/posthog/posthog-python/commit/b206669bf62c923346ad28881dc4694d933ca424) fix(llma): use distinct_id from outer context if not provided, fix $process_person_profile for context-based identity — Thanks @ethanporcaro for your first contribution 🎉!
- [a99c7d7](https://github.com/posthog/posthog-python/commit/a99c7d73b1e0ef1f35d856c82ace21237ee253a3) Add warning log for local flag evaluation cold start — Thanks @dmarticus!

## 7.9.6 — 2026-03-02

### Patch changes

- [8d83315](https://github.com/posthog/posthog-python/commit/8d83315b67c21eb9e7d6c17bae27ada98ca2643d) add PROPERTY_OPERATORS constant for match_property — Thanks @dmarticus!

## 7.9.5 — 2026-03-02

### Patch changes

- [830244b](https://github.com/posthog/posthog-python/commit/830244bd409b1992ae2e49610f8f87d2cdfc8096) add semver targeting support to local evaluation — Thanks @dmarticus!

## 7.9.4 — 2026-02-25

### Patch changes

- [a68a6a6](https://github.com/posthog/posthog-python/commit/a68a6a6d045072c88eeee7acac441536919b5954) feat(llma): add `$ai_tokens_source` property ("sdk" or "passthrough") to all `$ai_generation` events to detect when token values are externally overridden via `posthog_properties` — Thanks @carlos-marchal-ph!

## 7.9.3 — 2026-02-18

### Patch changes

- [9f9553a](https://github.com/posthog/posthog-python/commit/9f9553a420d22e5e6435b775993f61a059280c2a) Fix posthoganalytics release, previously broken — Thanks @rafaeelaudibert!

## 7.9.2 — 2026-02-18

### Patch changes

- [f1dc4d7](https://github.com/posthog/posthog-python/commit/f1dc4d73914712983a7f715ee4fe1b70e66e770a) Add sampo to the project — Thanks @rafaeelaudibert!

## 7.9.1 - 2026-02-17

fix(llma): make prompt fetches deterministic by requiring project_api_key and sending it as token query param

## 7.9.0 - 2026-02-17

feat: Support device_id as bucketing identifier for local evaluation

## 7.8.6 - 2026-02-09

fix: limit collections scanning in code variables

## 7.8.5 - 2026-02-09

fix: further optimize code variables pattern matching

## 7.8.4 - 2026-02-09

fix: do not pattern match long values in code variables

## 7.8.3 - 2026-02-06

fix: openAI input image sanitization

## 7.8.2 - 2026-02-04

fix(llma): fix prompts default url

## 7.8.1 - 2026-02-03

fix(llma): small fixes for prompt management

## 7.8.0 - 2026-01-28

feat(llma): add prompt management

Adds the Prompt Management feature. At the time of release, this feature is in a closed alpha.

## 7.7.0 - 2026-01-15

feat(ai): Add OpenAI Agents SDK integration

Automatic tracing for agent workflows, handoffs, tool calls, guardrails, and custom spans. Includes `$ai_total_tokens`, `$ai_error_type` categorization, and `$ai_framework` property.

## 7.6.0 - 2026-01-12

feat: add device_id to flags request payload

Add device_id parameter to all feature flag methods, allowing the server to track device identifiers for flag evaluation. The device_id can be passed explicitly or set via context using `set_context_device_id()`.

## 7.5.1 - 2026-01-07

fix: avoid return from finally block to fix Python 3.14 SyntaxWarning (#361) - thanks @jodal

## 7.5.0 - 2026-01-06

feat: Capture Langchain, OpenAI and Anthropic errors as exceptions (if exception autocapture is enabled)
feat: Add reference to exception in LLMA trace and span events

## 7.4.3 - 2026-01-02

Fixes cache creation cost for Langchain with Anthropic

## 7.4.2 - 2025-12-22

feat: add `in_app_modules` option to control code variables capturing

## 7.4.1 - 2025-12-19

fix: extract model from response for OpenAI stored prompts

When using OpenAI stored prompts, the model is defined in the OpenAI dashboard rather than passed in the API request. This fix adds a fallback to extract the model from the response object when not provided in kwargs, ensuring generations show up with the correct model and enabling cost calculations.

## 7.4.0 - 2025-12-16

feat: Add automatic retries for feature flag requests

Feature flag API requests now automatically retry on transient failures:

- Network errors (connection refused, DNS failures, timeouts)
- Server errors (500, 502, 503, 504)
- Up to 2 retries with exponential backoff (0.5s, 1s delays)

Rate limit (429) and quota (402) errors are not retried.

## 7.3.1 - 2025-12-06

fix: remove unused $exception_message and $exception_type

## 7.3.0 - 2025-12-05

feat: improve code variables capture masking

## 7.2.0 - 2025-12-01

feat: add $feature_flag_evaluated_at properties to $feature_flag_called events

## 7.1.0 - 2025-11-26

Add support for the async version of Gemini.

## 7.0.2 - 2025-11-18

Add support for Python 3.14.
Projects upgrading to Python 3.14 should ensure any Pydantic models passed into the SDK use Pydantic v2, as Pydantic v1 is not compatible with Python 3.14.

## 7.0.1 - 2025-11-15

Try to use repr() when formatting code variables

## 7.0.0 - 2025-11-11

NB Python 3.9 is no longer supported

- chore(llma): update LLM provider SDKs to latest major versions
  - openai: 1.102.0 → 2.7.1
  - anthropic: 0.64.0 → 0.72.0
  - google-genai: 1.32.0 → 1.49.0
  - langchain-core: 0.3.75 → 1.0.3
  - langchain-openai: 0.3.32 → 1.0.2
  - langchain-anthropic: 0.3.19 → 1.0.1
  - langchain-community: 0.3.29 → 0.4.1
  - langgraph: 0.6.6 → 1.0.2

## 6.9.3 - 2025-11-10

- feat(ph-ai): PostHog properties dict in GenerationMetadata

## 6.9.2 - 2025-11-10

- fix(llma): fix cache token double subtraction in Langchain for non-Anthropic providers causing negative costs

## 6.9.1 - 2025-11-07

- fix(error-tracking): pass code variables config from init to client

## 6.9.0 - 2025-11-06

- feat(error-tracking): add local variables capture

## 6.8.0 - 2025-11-03

- feat(llma): send web search calls to be used for LLM cost calculations

## 6.7.14 - 2025-11-03

- fix(django): Handle request.user access in async middleware context to prevent SynchronousOnlyOperation errors in Django 5+ (fixes #355)
- test(django): Add Django 5 integration test suite with real ASGI application testing async middleware behavior

## 6.7.13 - 2025-11-02

- fix(llma): cache cost calculation in the LangChain callback

## 6.7.12 - 2025-11-02

- fix(django): Restore process_exception method to capture view and downstream middleware exceptions (fixes #329)
- fix(ai/langchain): Add LangChain 1.0+ compatibility for CallbackHandler imports (fixes #362)

## 6.7.11 - 2025-10-28

- feat(ai): Add `$ai_framework` property for framework integrations (e.g. LangChain)

## 6.7.10 - 2025-10-24

- fix(django): Make middleware truly hybrid - compatible with both sync (WSGI) and async (ASGI) Django stacks without breaking sync-only deployments

## 6.7.9 - 2025-10-22

- fix(flags): multi-condition flags with static cohorts returning wrong variants

## 6.7.8 - 2025-10-16

- fix(llma): missing async for OpenAI's streaming implementation

## 6.7.7 - 2025-10-14

- fix: remove deprecated attribute $exception_personURL from exception events

## 6.7.6 - 2025-09-16

- fix: don't sort condition sets with variant overrides to the top
- fix: Prevent core Client methods from raising exceptions

## 6.7.5 - 2025-09-16

- feat: Django middleware now supports async request handling.

## 6.7.4 - 2025-09-05

- fix: Missing system prompts for some providers

## 6.7.3 - 2025-09-04

- fix: missing usage tokens in Gemini

## 6.7.2 - 2025-09-03

- fix: tool call results in streaming providers

## 6.7.1 - 2025-09-01

- fix: Add base64 inline image sanitization

## 6.7.0 - 2025-08-26

- feat: Add support for feature flag dependencies

## 6.6.1 - 2025-08-21

- fix: Prevent `NoneType` error when `group_properties` is `None`

## 6.6.0 - 2025-08-15

- feat: Add `flag_keys_to_evaluate` parameter to optimize feature flag evaluation performance by only evaluating specified flags
- feat: Add `flag_keys_filter` option to `send_feature_flags` for selective flag evaluation in capture events

## 6.5.0 - 2025-08-08

- feat: Add `$context_tags` to an event to know which properties were included as tags

## 6.4.1 - 2025-08-06

- fix: Always pass project API key in `remote_config` requests for deterministic project routing

## 6.4.0 - 2025-08-05

- feat: support Vertex AI for Gemini

## 6.3.4 - 2025-08-04

- fix: set `$ai_tools` for all providers and `$ai_output_choices` for all non-streaming provider flows properly

## 6.3.3 - 2025-08-01

- fix: `get_feature_flag_result` now correctly returns FeatureFlagResult when payload is empty string instead of None

## 6.3.2 - 2025-07-31

- fix: Anthropic's tool calls are now handled properly

## 6.3.0 - 2025-07-22

- feat: Enhanced `send_feature_flags` parameter to accept `SendFeatureFlagsOptions` object for declarative control over local/remote evaluation and custom properties

## 6.2.1 - 2025-07-21

- feat: make `posthog_client` an optional argument in PostHog AI providers wrappers (`posthog.ai.*`), intuitively using the default client as the default

## 6.1.1 - 2025-07-16

- fix: correctly capture exceptions processed by Django from views or middleware

## 6.1.0 - 2025-07-10

- feat: decouple feature flag local evaluation from personal API keys; support decrypting remote config payloads without relying on the feature flags poller

## 6.0.4 - 2025-07-09

- fix: add POSTHOG_MW_CLIENT setting to django middleware, to support custom clients for exception capture.

## 6.0.3 - 2025-07-07

- feat: add a feature flag evaluation cache (local storage or redis) to support returning flag evaluations when the service is down

## 6.0.2 - 2025-07-02

- fix: send_feature_flags changed to default to false in `Client::capture_exception`

## 6.0.1

- fix: response `$process_person_profile` property when passed to capture

## 6.0.0

This release contains a number of major breaking changes:

- feat: make distinct_id an optional parameter in posthog.capture and related functions
- feat: make capture and related functions return `Optional[str]`, which is the UUID of the sent event, if it was sent
- fix: remove `identify` (prefer `posthog.set()`), and `page` and `screen` (prefer `posthog.capture()`)
- fix: delete exception-capture specific integrations module. Prefer the general-purpose django middleware as a replacement for the django `Integration`.

To migrate to this version, you'll mostly just need to switch to using named keyword arguments, rather than positional ones. For example:

```python
# Old calling convention
posthog.capture("user123", "button_clicked", {"button_id": "123"})
# New calling convention
posthog.capture(distinct_id="user123", event="button_clicked", properties={"button_id": "123"})

# Better pattern
with posthog.new_context():
    posthog.identify_context("user123")

    # The event name is the first argument, and can be passed positionally, or as a keyword argument in a later position
    posthog.capture("button_pressed")
```

Generally, arguments are now appropriately typed, and docstrings have been updated. If something is unclear, please open an issue, or submit a PR!

## 5.4.0 - 2025-06-20

- feat: add support to session_id context on page method

## 5.3.0 - 2025-06-19

- fix: safely handle exception values

## 5.2.0 - 2025-06-19

- feat: construct artificial stack traces if no traceback is available on a captured exception

## 5.1.0 - 2025-06-18

- feat: session and distinct ID's can now be associated with contexts, and are used as such
- feat: django http request middleware

## 5.0.0 - 2025-06-16

- fix: removed deprecated sentry integration

## 4.10.0 - 2025-06-13

- fix: no longer fail in autocapture.

## 4.9.0 - 2025-06-13

- feat(ai): track reasoning and cache tokens in the LangChain callback

## 4.8.0 - 2025-06-10

- fix: export scoped, rather than tracked, decorator
- feat: allow use of contexts without error tracking

## 4.7.0 - 2025-06-10

- feat: add support for parse endpoint in responses API (no longer beta)

## 4.6.2 - 2025-06-09

- fix: replace `import posthog` with direct method imports

## 4.6.1 - 2025-06-09

- fix: replace `import posthog` in `posthoganalytics` package

## 4.6.0 - 2025-06-09

- feat: add additional user and request context to captured exceptions via the Django integration
- feat: Add `setup()` function to initialise default client

## 4.5.0 - 2025-06-09

- feat: add before_send callback (#249)

## 4.4.2- 2025-06-09

- empty point release to fix release automation

## 4.4.1 2025-06-09

- empty point release to fix release automation

## 4.4.0 - 2025-06-09

- Use the new `/flags` endpoint for all feature flag evaluations (don't fall back to `/decide` at all)

## 4.3.2 - 2025-06-06

1. Add context management:

- New context manager with `posthog.new_context()`
- Tag functions: `posthog.tag()`, `posthog.get_tags()`, `posthog.clear_tags()`
- Function decorator:
  - `@posthog.scoped` - Creates context and captures exceptions thrown within the function
- Automatic deduplication of exceptions to ensure each exception is only captured once

2. fix: feature flag request use geoip_disable (#235)
3. chore: pin actions versions (#210)
4. fix: opinionated setup and clean fn fix (#240)
5. fix: release action failed (#241)

## 4.2.0 - 2025-05-22

Add support for google gemini

## 4.1.0 - 2025-05-22

Moved ai openai package to a composition approach over inheritance.

## 4.0.1 – 2025-04-29

1. Remove deprecated `monotonic` library. Use Python's core `time.monotonic` function instead
2. Clarify Python 3.9+ is required

## 4.0.0 - 2025-04-24

1. Added new method `get_feature_flag_result` which returns a `FeatureFlagResult` object. This object breaks down the result of a feature flag into its enabled state, variant, and payload. The benefit of this method is it allows you to retrieve the result of a feature flag and its payload in a single API call. You can call `get_value` on the result to get the value of the feature flag, which is the same value returned by `get_feature_flag` (aka the string `variant` if the flag is a multivariate flag or the `boolean` value if the flag is a boolean flag).

Example:

```python
result = posthog.get_feature_flag_result("my-flag", "distinct_id")
print(result.enabled)     # True or False
print(result.variant)     # 'the-variant-value' or None
print(result.payload)     # {'foo': 'bar'}
print(result.get_value()) # 'the-variant-value' or True or False
print(result.reason)      # 'matched condition set 2' (Not available for local evaluation)
```

Breaking change:

1. `get_feature_flag_payload` now deserializes payloads from JSON strings to `Any`. Previously, it returned the payload as a JSON encoded string.

Before:

```python
payload = get_feature_flag_payload('key', 'distinct_id') # "{\"some\": \"payload\"}"
```

After:

```python
payload = get_feature_flag_payload('key', 'distinct_id') # {"some": "payload"}
```

## 3.25.0 – 2025-04-15

1. Roll out new `/flags` endpoint to 100% of `/decide` traffic, excluding the top 10 customers.

## 3.24.3 – 2025-04-15

1. Fix hash inclusion/exclusion for flag rollout

## 3.24.2 – 2025-04-15

1. Roll out new /flags endpoint to 10% of /decide traffic

## 3.24.1 – 2025-04-11

1. Add `log_captured_exceptions` option to proxy setup

## 3.24.0 – 2025-04-10

1. Add config option to `log_captured_exceptions`

## 3.23.0 – 2025-03-26

1. Expand automatic retries to include read errors (e.g. RemoteDisconnected)

## 3.22.0 – 2025-03-26

1. Add more information to `$feature_flag_called` events.
2. Support for the `/decide?v=4` endpoint which contains more information about feature flags.

## 3.21.0 – 2025-03-17

1. Support serializing dataclasses.

## 3.20.0 – 2025-03-13

1. Add support for OpenAI Responses API.

## 3.19.2 – 2025-03-11

1. Fix install requirements for analytics package

## 3.19.1 – 2025-03-11

1. Fix bug where None is sent as delta in azure

## 3.19.0 – 2025-03-04

1. Add support for tool calls in OpenAI and Anthropic.
2. Add support for cached tokens.

## 3.18.1 – 2025-03-03

1. Improve quota-limited feature flag logs

## 3.18.0 - 2025-02-28

1. Add support for Azure OpenAI.

## 3.17.0 - 2025-02-27

1. The LangChain handler now captures tools in `$ai_generation` events, in property `$ai_tools`. This allows for displaying tools provided to the LLM call in PostHog UI. Note that support for `$ai_tools` in OpenAI and Anthropic SDKs is coming soon.

## 3.16.0 - 2025-02-26

1. feat: add some platform info to events (#198)

## 3.15.1 - 2025-02-23

1. Fix async client support for OpenAI.

## 3.15.0 - 2025-02-19

1. Support quota-limited feature flags

## 3.14.2 - 2025-02-19

1. Evaluate feature flag payloads with case sensitivity correctly. Fixes <https://github.com/PostHog/posthog-python/issues/178>

## 3.14.1 - 2025-02-18

1. Add support for Bedrock Anthropic Usage

## 3.13.0 - 2025-02-12

1. Automatically retry connection errors

## 3.12.1 - 2025-02-11

1. Fix mypy support for 3.12.0
2. Deprecate `is_simple_flag`

## 3.12.0 - 2025-02-11

1. Add support for OpenAI beta parse API.
2. Deprecate `context` parameter

## 3.11.1 - 2025-02-06

1. Fix LangChain callback handler to capture parent run ID.

## 3.11.0 - 2025-01-28

1. Add the `$ai_span` event to the LangChain callback handler to capture the input and output of intermediary chains.

   > LLM observability naming change: event property `$ai_trace_name` is now `$ai_span_name`.

2. Fix serialiazation of Pydantic models in methods.

## 3.10.0 - 2025-01-24

1. Add `$ai_error` and `$ai_is_error` properties to LangChain callback handler, OpenAI, and Anthropic.

## 3.9.3 - 2025-01-23

1. Fix capturing of multiple traces in the LangChain callback handler.

## 3.9.2 - 2025-01-22

1. Fix importing of LangChain callback handler under certain circumstances.

## 3.9.0 - 2025-01-22

1. Add `$ai_trace` event emission to LangChain callback handler.

## 3.8.4 - 2025-01-17

1. Add Anthropic support for LLM Observability.
2. Update LLM Observability to use output_choices.

## 3.8.3 - 2025-01-14

1. Fix setuptools to include the `posthog.ai.openai` and `posthog.ai.langchain` packages for the `posthoganalytics` package.

## 3.8.2 - 2025-01-14

1. Fix setuptools to include the `posthog.ai.openai` and `posthog.ai.langchain` packages.

## 3.8.1 - 2025-01-14

1. Add LLM Observability with support for OpenAI and Langchain callbacks.

## 3.7.5 - 2025-01-03

1. Add `distinct_id` to group_identify

## 3.7.4 - 2024-11-25

1. Fix bug where this SDK incorrectly sent feature flag events with null values when calling `get_feature_flag_payload`.

## 3.7.3 - 2024-11-25

1. Use personless mode when sending an exception without a provided `distinct_id`.

## 3.7.2 - 2024-11-19

1. Add `type` property to exception stacks.

## 3.7.1 - 2024-10-24

1. Add `platform` property to each frame of exception stacks.

## 3.7.0 - 2024-10-03

1. Adds a new `super_properties` parameter on the client that are appended to every /capture call.

## 3.6.7 - 2024-09-24

1. Remove deprecated datetime.utcnow() in favour of datetime.now(tz=tzutc())

## 3.6.6 - 2024-09-16

1. Fix manual capture support for in app frames

## 3.6.5 - 2024-09-10

1. Fix django integration support for manual exception capture.

## 3.6.4 - 2024-09-05

1. Add manual exception capture.

## 3.6.3 - 2024-09-03

1. Make sure setup.py for posthoganalytics package also discovers the new exception integration package.

## 3.6.2 - 2024-09-03

1. Make sure setup.py discovers the new exception integration package.

## 3.6.1 - 2024-09-03

1. Adds django integration to exception autocapture in alpha state. This feature is not yet stable and may change in future versions.

## 3.6.0 - 2024-08-28

1. Adds exception autocapture in alpha state. This feature is not yet stable and may change in future versions.

## 3.5.2 - 2024-08-21

1. Guard for None values in local evaluation

## 3.5.1 - 2024-08-13

1. Remove "-api" suffix from ingestion hostnames

## 3.5.0 - 2024-02-29

1. - Adds a new `feature_flags_request_timeout_seconds` timeout parameter for feature flags which defaults to 3 seconds, updated from the default 10s for all other API calls.

## 3.4.2 - 2024-02-20

1. Add `historical_migration` option for bulk migration to PostHog Cloud.

## 3.4.1 - 2024-02-09

1. Use new hosts for event capture as well

## 3.4.0 - 2024-02-05

1. Point given hosts to new ingestion hosts

## 3.3.4 - 2024-01-30

1. Update type hints for module variables to work with newer versions of mypy

## 3.3.3 - 2024-01-26

1. Remove new relative date operators, combine into regular date operators

## 3.3.2 - 2024-01-19

1. Return success/failure with all capture calls from module functions

## 3.3.1 - 2024-01-10

1. Make sure we don't override any existing feature flag properties when adding locally evaluated feature flag properties.

## 3.3.0 - 2024-01-09

1. When local evaluation is enabled, we automatically add flag information to all events sent to PostHog, whenever possible. This makes it easier to use these events in experiments.

## 3.2.0 - 2024-01-09

1. Numeric property handling for feature flags now does the expected: When passed in a number, we do a numeric comparison. When passed in a string, we do a string comparison. Previously, we always did a string comparison.
2. Add support for relative date operators for local evaluation.

## 3.1.0 - 2023-12-04

1. Increase maximum event size and batch size

## 3.0.2 - 2023-08-17

1. Returns the current flag property with $feature_flag_called events, to make it easier to use in experiments

## 3.0.1 - 2023-04-21

1. Restore how feature flags work when the client library is disabled: All requests return `None` and no events are sent when the client is disabled.
2. Add a `feature_flag_definitions()` debug option, which returns currently loaded feature flag definitions. You can use this to more cleverly decide when to request local evaluation of feature flags.

## 3.0.0 - 2023-04-14

Breaking change:

All events by default now send the `$geoip_disable` property to disable geoip lookup in app. This is because usually we don't
want to update person properties to take the server's location.

The same now happens for feature flag requests, where we discard the IP address of the server for matching on geoip properties like city, country, continent.

To restore previous behaviour, you can set the default to False like so:

```python
posthog.disable_geoip = False

# // and if using client instantiation:
posthog = Posthog('api_key', disable_geoip=False)

```

## 2.5.0 - 2023-04-10

1. Add option for instantiating separate client object

## 2.4.2 - 2023-03-30

1. Update backoff dependency for posthoganalytics package to be the same as posthog package

## 2.4.1 - 2023-03-17

1. Removes accidental print call left in for decide response

## 2.4.0 - 2023-03-14

1. Support evaluating all cohorts in feature flags for local evaluation

## 2.3.1 - 2023-02-07

1. Log instead of raise error on posthog personal api key errors
2. Remove upper bound on backoff dependency

## 2.3.0 - 2023-01-31

1. Add support for returning payloads of matched feature flags

## 2.2.0 - 2022-11-14

Changes:

1. Add support for feature flag variant overrides with local evaluation

## 2.1.2 - 2022-09-15

Changes:

1. Fixes issues with date comparison.

## 2.1.1 - 2022-09-14

Changes:

1. Feature flags local evaluation now supports date property filters as well. Accepts both strings and datetime objects.

## 2.1.0 - 2022-08-11

Changes:

1. Feature flag defaults have been removed
2. Setup logging only when debug mode is enabled.

## 2.0.1 - 2022-08-04

- Make poll_interval configurable
- Add `send_feature_flag_events` parameter to feature flag calls, which determine whether the `$feature_flag_called` event should be sent or not.
- Add `only_evaluate_locally` parameter to feature flag calls, which determines whether the feature flag should only be evaluated locally or not.

## 2.0.0 - 2022-08-02

Breaking changes:

1. The minimum version requirement for PostHog servers is now 1.38. If you're using PostHog Cloud, you satisfy this requirement automatically.
2. Feature flag defaults apply only when there's an error fetching feature flag results. Earlier, if the default was set to `True`, even if a flag resolved to `False`, the default would override this.
   **Note: These are removed in 2.0.2**
3. Feature flag remote evaluation doesn't require a personal API key.

New Changes:

1. You can now evaluate feature flags locally (i.e. without sending a request to your PostHog servers) by setting a personal API key, and passing in groups and person properties to `is_feature_enabled` and `get_feature_flag` calls.
2. Introduces a `get_all_flags` method that returns all feature flags. This is useful for when you want to seed your frontend with some initial flags, given a user ID.

## 1.4.9 - 2022-06-13

- Support for sending feature flags with capture calls

## 1.4.8 - 2022-05-12

- Support multi variate feature flags

## 1.4.7 - 2022-04-25

- Allow feature flags usage without project_api_key

## 1.4.1 - 2021-05-28

- Fix packaging issues with Sentry integrations

## 1.4.0 - 2021-05-18

- Improve support for `project_api_key` (#32)
- Resolve polling issues with feature flags (#29)
- Add Sentry (and Sentry+Django) integrations (#13)
- Fix feature flag issue with no percentage rollout (#30)

## 1.3.1 - 2021-05-07

- Add `$set` and `$set_once` support (#23)
- Add distinct ID to `$create_alias` event (#27)
- Add `UUID` to `ID_TYPES` (#26)

## 1.2.1 - 2021-02-05

Initial release logged in CHANGELOG.md.
