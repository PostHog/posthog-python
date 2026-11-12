# Migrating from posthog 7.x to 8.0

posthog 8.0 sends every event through capture v1 and removes the legacy capture path.
Most apps upgrade without code changes.
Read the checklist first, then the sections that apply to you.

For the changes every capture v1 SDK shares, such as event options, limits and per-event results, see [Upgrading to capture v1 SDKs](https://posthog.com/docs/libraries/capture-v1).

## Checklist

You need to change code if your app does any of these:

- passes `Client` or `AsyncPosthog` constructor arguments by position after `host`
- calls `posthog.request.post` or `posthog.request.flags` with positional arguments after `path` or `host`
- sets `$exception_level` or another reserved exception property in `capture_exception(properties=...)`
- sets `capture_mode`, `POSTHOG_CAPTURE_MODE` or `gzip`
- imports `CaptureMode`, `posthog.capture_mode`, `CaptureV1Error`, `posthog.capture_v1`, `request.batch_post`, `EVENTS_ENDPOINT` or `AI_EVENTS_ENDPOINT`
- sends events to a self-hosted PostHog that does not serve the capture v1 endpoints
- sends events through a proxy that redirects capture requests to another host
- reuses one event `uuid` for more than one event
- expects `super_properties` to override properties passed to a single call, including `$set` and `$groups`
- sets `$is_server`, `$geoip_disable` or a system property such as `$os` and expects the SDK's value to win
- has a `before_send` hook that returns a new dict: copy `options`, or the event is sent without them, including the `process_person_profile=False` the SDK sets for events without a distinct ID (see [Event options](#event-options)). The same applies to MCP's `before_send` (see [MCP analytics](#mcp-analytics))
- captures `$ai_` events with `capture()`: send them with `capture_ai()`
- tests the AI integrations with a mock client and asserts on `capture`
- passes its own client object to the AI integrations
- sets `$lib` or `$lib_version` in a call's `properties`, a context tag or `super_properties`
- filters insights or dashboards on `$lib = posthog-python-mcp`
- passes a personal API key (`phx_...`) as the project API key
- reads event content from the SDK's debug log
- calls `alias()` without a `distinct_id` and relies on the context distinct ID
- turns person processing off with `super_options`, `super_properties`, a context option or a context tag, and expects identify-style events, such as those from `alias`, `group_identify`, `set` or `set_once`, to follow it: they now default to person processing on (see [Identity and request context](#identity-and-request-context))
- runs the Django, Flask or ASGI integration on 7.68 to 7.70 without tracing headers turned on (they are now read by default)

## Endpoints

| Method | Endpoint |
| --- | --- |
| `capture`, `set`, `set_once`, `alias`, `group_identify`, `capture_exception`, `AsyncPosthog.capture_immediate` | `/i/v1/analytics/events` |
| `capture_ai`, `capture_ai_immediate`, and the AI wrapper integrations | `/i/v1/ai/events` |
| OpenTelemetry (`posthog.ai.otel`) | `/i/v0/ai/otel`, unchanged |

If you send events to a self-hosted PostHog, check that it serves both `/i/v1/analytics/events` and `/i/v1/ai/events` before you upgrade.
Self-hosted PostHog serves the AI endpoint from a separate capture service, so check each route.
An endpoint that is not served drops every event sent to it.

If you send events through your own reverse proxy, update it before you upgrade:

- Forward `/i/v1/analytics/events` and `/i/v1/ai/events` to PostHog.
- Pass `Authorization` (the project token, sent as `Bearer <token>`), every `PostHog-*` header, `Content-Type`, `Content-Encoding` and `User-Agent` through unchanged. PostHog answers `401` without `Authorization` and `400` without the others, and the batch fails.
- Accept request bodies up to 20 MB. AI requests are often larger than 1 MB, the default limit in nginx and ingress-nginx. A `413` from the proxy fails the batch.

PostHog's managed reverse proxy already does this. See the [proxy guides](https://posthog.com/docs/advanced/proxy).

The SDK follows a `307` or `308` redirect only to the origin of `host` (same scheme, host and port), at most 5 times.
Any other redirect fails the batch and reaches `on_error`.
In 7.x the sync client followed redirects to any origin.
If a proxy redirects capture requests to another host, point `host` at the final host.

## SDK identity

By default every event carries `$lib: "posthog-python"` and the installed package version in `$lib_version`.
PostHog stores the values the event is sent with.

You can set them in a call's `properties`, a context tag or `super_properties`. Like any other property, each one comes from the most specific place that sets it, so set both in the same place.
`$lib` and `$lib_version` count as set only when they are non-empty strings.
If you set `$lib` without `$lib_version`, the SDK adds its own version only when `$lib` is `posthog-python`.
A `$lib_version` without a `$lib` is replaced with the SDK's.
`before_send` sees both and can change or remove them.
In 7.x the SDK overwrote a `$lib` or `$lib_version` set in a call or a context tag.

This changes `super_properties` too:

- `super_properties={"$lib": "my-lib"}` sends no `$lib_version`. In 7.x it sent the SDK version.
- `super_properties={"$lib_version": "1.0"}` without a `$lib` sends the SDK's `$lib` and version. In 7.x it sent `1.0`.

If an event reaches PostHog without `$lib`, PostHog fills both from the SDK name and version your request sends.
If `$lib` is that SDK name and `$lib_version` is missing, empty or not a string, PostHog fills `$lib_version` the same way.

MCP analytics events from `posthog.mcp.instrument()` or `PostHogMCP` report `posthog-python-mcp` and the installed package version.
The integration sets both in each event's own properties, so neither a context tag nor `super_properties` replaces them. `before_send` can.
In 7.x the MCP integration relabeled the whole client, or the default client when `instrument()` got none: the app's own events, the request headers and feature flag requests.
In 8.0 those report `posthog-python`, so one client can serve both.

Custom events from `PostHogMCP.capture()` now report `posthog-python`. In 7.x they reported `posthog-python-mcp`.
Custom events from `McpAnalytics.capture()`, the handle `instrument()` returns, still report `posthog-python-mcp`.
If an insight or dashboard filters on `$lib = posthog-python-mcp`, check that it still finds the events you expect.

## Removed options and APIs

| Removed | Use instead |
| --- | --- |
| `capture_mode`, `CaptureMode`, `POSTHOG_CAPTURE_MODE` | Nothing. Capture v1 is the only path. Setting `posthog.capture_mode` on the module has no effect. |
| `gzip=True` | `capture_compression=CaptureCompression.GZIP`. The default is no compression. `POSTHOG_CAPTURE_COMPRESSION` still works. |
| `CaptureV1Error` (`from posthog.capture_v1 import CaptureV1Error`) | `CaptureError` (`from posthog import CaptureError`). There is no alias. |
| `posthog.capture_v1` | `posthog.capture_event` and `posthog.capture_send` |
| `request.batch_post`, `async_batch_post`, `EVENTS_ENDPOINT`, `AI_EVENTS_ENDPOINT` | Nothing. Send events through a client. |
| The `gzip` parameter of `request.post` and `request.flags` | Nothing. The parameters after `path` in `post` and after `host` in `flags` are keyword-only, so a 7.x call that passes them by position raises `TypeError`. A `gzip=` keyword raises no error: it is sent as a field in the request body. |
| The `backoff` dependency | Add it to your own requirements if your code imports it. |

The `posthoganalytics` package has the same changes. For example, import `CaptureError` from `posthoganalytics`.

Constructor arguments after `host` are keyword-only:

```python
# 7.x
Posthog("<api_key>", "https://us.i.posthog.com", True)

# 8.0
Posthog("<api_key>", host="https://us.i.posthog.com", debug=True)
```

## Errors and `on_error`

Capture v1 returns a result for every event, even when the request succeeds.
An event can be dropped inside a 2xx response, for example when it is over a product quota.
8.0 reports those events as failures.
When the whole request is over the billing limit, capture returns a `402` with no per-event results instead.

- Every capture failure is a `CaptureError`, a subclass of `APIError`. It has these fields:
  - `status`: the HTTP status, or `0` when the request never got a response. With `0`, `__cause__` holds the network error. In 7.x, `on_error` received the raw `requests` exception.
  - `endpoint`, `request_id` and `attempts`
  - `drops`: `(uuid, details)` pairs for the events capture dropped in a 2xx response
  - `retry_exhausted`: the uuids still waiting for a retry after the last attempt
  - `event_results`: a `CaptureEventResult(result, details)` for each uuid that got a result in a 2xx response, from `posthog.capture_send`. An event missing from it never got a result.
  - `verdict_summary()`: counts such as `drop/llm_events_over_quota=1, retry/not_persisted=1`
- `on_error(error, batch)` receives every failure, including in `sync_mode` and from `AsyncPosthog.capture_immediate`.
- With `on_error` set, the SDK logs nothing for a failed batch.
- Without `on_error`, the SDK logs one line per failed batch, for example `2 event(s) not persisted by /i/v1/analytics/events: ...`. The line never contains event content or the server's response text. After a 2xx response it also counts the lost events by PostHog's reason, for example `drop/event_too_big=1`.
- The 7.x lines `error uploading: ...`, `async capture upload failed (...)` and `Immediate async capture failed (...)` are gone. In `sync_mode`, a failed request no longer logs the 7.x `Error in capture: ...` line, or the same line for another method. Update any log-based alerts that match these lines.
- In `sync_mode`, a failed `capture()` calls `on_error` and returns `None`. With `debug=True` it calls `on_error` and then re-raises the error.
- In `sync_mode`, a failed request is retried like a background batch, in the calling thread. With the default `max_retries` of 3, a call makes up to 4 attempts, and each attempt can take up to `timeout` (`capture_ai_timeout` for AI events). With the defaults, an analytics event whose requests keep timing out blocks the call for about a minute. 7.x made one attempt.
- If a capture inside `on_error` fails, the SDK logs the failure and does not call `on_error` again.
- The SDK retries `404`, `408`, `429`, `500`, `502`, `503` and `504` responses and network errors. Other error statuses fail the batch at once, including the other 5xx codes. 7.x retried `429` and every 5xx, but not `404`. Capture itself does not return `429`; a proxy in front of it can. A `404` that lasts through every attempt can mean the PostHog deployment has no capture v1 route, for example an older self-hosted install.
- Each attempt makes one connection try. 7.x also retried a failed connection up to twice inside each attempt, so a capture request could block for up to three times the `timeout` per attempt.
- Retries start at 100 ms and double each attempt, up to 30 seconds. Each wait varies randomly by up to 25%, so many clients do not retry at the same moment. A `Retry-After` header sets a minimum wait, up to 30 seconds. The `Consumer` default is 3 retries, down from 10. In 7.x the first retry waited up to 1 second.
- The SDK's own log lines no longer contain event content: the debug line for a queued event names the event and the lane, and the invalid timestamp warning no longer includes the value. When your `before_send` hook raises, the error line names the event and the exception type, not the exception message, which can hold the data the hook was removing. The traceback is logged at debug level. To inspect events, use `before_send`.
- A personal API key (`phx_...`) passed as the project API key disables the client and logs one error, as an empty key does. Capture rejects personal API keys, and the SDK no longer sends one with every request.

## Invalid events

Capture rejects a whole batch when one event cannot be parsed.
8.0 drops only the invalid event, so the rest of the batch is delivered.

- An event whose name is missing, `None`, empty or not a string is dropped. The SDK logs one warning per reason per process. 7.x sent it.
- An event whose `before_send` hook sets `distinct_id` to `None` or a non-string is dropped, with one warning per reason per process. The SDK does not convert the hook's value or make up an ID. 7.x sent it, and capture would reject the whole batch.
- PostHog drops an event whose name is over 200 UTF-8 bytes (`event_name_too_long`), and `on_error` reports it. 7.x sent events to legacy capture, which had no event name limit.
- A `NaN` or infinite `float` value is sent as `null`, as in 7.75 and later, and the event is kept. Earlier 7.x versions sent invalid JSON, which made capture reject the whole batch. A `NaN` value in `$set` sets that person property to `null`.
- An event that cannot be serialized to JSON, for example one with a circular reference, is dropped and logged. As in 7.x, a circular reference logs `Error in capture: maximum recursion depth exceeded` with a long traceback.
- A property or option key that JSON cannot represent, such as a tuple, is dropped without a log, and the rest of the event is sent. 7.x dropped the event. `int`, finite `float`, `bool` and `None` keys are still converted to strings. A `NaN` or infinite `float` key still drops the event.

## Event uuids

- Each event needs its own uuid. Capture keeps the first event for each uuid in a request and drops the later copies. `on_error` reports each dropped copy in `CaptureError.drops` as `(uuid, "duplicate_event_uuid")`. 7.x sent every copy. Older PostHog versions reject the whole batch with a 400 `duplicate_event_uuid` instead.
- The SDK accepts a uuid with hyphens, 32 hex digits, `{...}` braces or a `urn:uuid:` prefix, in any case. It sends the lowercase hyphenated form and returns that form from `capture`.
- Any other value is replaced with a generated uuid, and the SDK logs one warning that names the rule, not the value. An empty string counts as unset, so the SDK generates a uuid without a warning. This applies to `AsyncPosthog` too.
- Generated uuids are UUIDv7.

## Session and window IDs

Capture v1 sends `$session_id` and `$window_id` as top-level event fields, not as properties.
The SDK moves them out of `properties` for you.

- A string is sent as given, including `""`.
- `None` counts as unset and is removed.
- Any other value is removed and not sent, because capture would reject the whole batch. 7.x sent it as a property. The SDK logs a warning for each one, with the key and the value's type but not the value.

## Identity and request context

- An empty or whitespace-only `distinct_id` counts as missing, so the event gets a generated personless distinct ID. 7.x sent a whitespace-only ID, and capture dropped the event.
- Generated personless distinct IDs are UUIDv7. 7.x generated UUIDv4.
- `group_identify` uses the `distinct_id` you pass, then the context distinct ID, then `$<group_type>_<group_key>`. 7.x fell back to a random ID, which created a new person on every call. If `$<group_type>_<group_key>` is over 200 bytes, pass `distinct_id`: PostHog drops an event whose distinct ID is over 200 bytes.
- PostHog drops an event whose `distinct_id` is over 200 UTF-8 bytes (`distinct_id_too_large`), and `on_error` reports it. 7.x sent events to legacy capture, which shortened the ID to 200 characters and kept the event.
- `group_identify` no longer adds the context session ID. A session takes the person of its latest event, and this event's distinct ID is not the session's user.
- `alias()` needs an explicit `distinct_id`. It no longer falls back to the context distinct ID, which can come from a request header the client controls. Without one, the call is dropped with a warning.
- Context tags and context options now apply to `alias` and `group_identify`, below the call's own values, as they do for `capture`.
- Identify-style events default to person processing on. These are `$identify`, `$create_alias`, `$merge_dangerously`, `$groupidentify`, `$set` and `$set_once`, whether `alias`, `group_identify`, `set`, `set_once`, `capture` or `capture_ai` sends them. The SDK sets `process_person_profile: True` on them unless the call sets the `process_person_profile` option or a `$process_person_profile` property. PostHog drops the `$create_alias` and `$groupidentify` events when person processing is off, with an ingestion warning, and ignores the person update of `$set` and `$set_once`.
  - The call's own value wins over context options and tags, `super_options` and `super_properties`, so a `False` set there never reaches these events. In 7.x, `$process_person_profile: False` in `super_properties` reached them too.
  - When the call sets only the `$process_person_profile` property, the SDK moves it into the option before `before_send` runs. A `None` option or property counts as unset here, so the event gets `True`.
  - An event whose distinct ID the SDK generated keeps `process_person_profile: False`.
  - `before_send` sees the value and can change it. A hook that builds an identify-style event, or renames an event to one, must set the option itself.

## Tracing headers

The Django, Flask and ASGI integrations read the `X-PostHog-Distinct-Id` and `X-PostHog-Session-Id` headers by default, as in 7.71 and later.
The headers only fill a missing distinct ID or session ID, and any client can set them, so never use them for authentication.

- To turn this off, set `POSTHOG_MW_CAPTURE_TRACING_HEADERS = False` in Django, or pass `capture_tracing_headers=False` to the Flask or ASGI integration.
- The 7.68 `trust_tracing_headers` and `POSTHOG_MW_TRUST_TRACING_HEADERS` names still work, but are deprecated.
- If you upgrade from 7.68, 7.69 or 7.70 and never turned these headers on, they are now read.

## Exceptions

- `capture_exception` takes a `level=` argument on `Client`, `AsyncPosthog` and the module. It sets `$exception_level`, for example `"warning"` or `"fatal"`. The default is `"error"`, and an unknown value counts as unset.
- `capture_exception` ignores reserved exception properties in `properties`, such as `$exception_list`, `$exception_level` and `$exception_source`. In 7.x they overrode the SDK's values, with a `DeprecationWarning`. Use `level=` instead of `$exception_level`.
- `AsyncPosthog.capture_exception` now sends `$exception_level`.
- `AsyncPosthog.capture_exception` now adds `$trace_id` and `$span_id` from the active OpenTelemetry span, like `Client.capture_exception`. In 7.x it added them only with `capture_trace_context=True`.

## Event options

Capture v1 sends processing options in an `options` object, next to `properties`.

- `capture`, `capture_ai`, `set`, `set_once`, `alias`, `group_identify` and `capture_exception` take an `options` argument:

  ```python
  posthog.capture("signed_up", distinct_id="user-1", options={"cookieless_mode": True})
  ```

  The MCP capture methods take `event_options` instead (see [MCP analytics](#mcp-analytics)), and the AI integrations take `posthog_options` (see [AI capture](#ai-capture)).

- `super_options`, a new `Client`, `AsyncPosthog` and module setting, sets options on every event, like `super_properties`.
- `set_context_option(key, value)` sets an option for the current context, like `tag()`. `get_context_options()` returns the options of the current context. Both exist on `Client` and as module functions. `AsyncPosthog` has no context methods, like it has no `tag()`, so call the module functions. They apply to every client in the context.
- The Celery integration propagates context options to workers, like tags.
  - A propagated `process_person_profile: True` turns person processing on for worker events without a distinct ID. Each of those events creates a person.
  - In eager mode (`task_always_eager`), Celery publishes nothing, so the distinct ID, tags and options do not reach the task's events. To carry them, pass the task's PostHog headers with `task.apply(headers=...)` or `task.apply_async(headers=...)`.
- Context tags and options reach `capture`, `capture_ai`, `capture_exception`, `alias` and `group_identify`. Context options also reach `set` and `set_once`. `super_properties` and `super_options` reach every method.
- Options are sent as given. PostHog validates them. It drops an event with an option value it cannot read, such as a list for `cookieless_mode`, as `invalid_options`, and `on_error` reports it.
- An `options` value that is not a dict is logged as an error and ignored. The event is still sent.
- Each option replaces a legacy option property:

  | Option | Replaces |
  | --- | --- |
  | `cookieless_mode` | `$cookieless_mode` |
  | `disable_skew_correction` | `$ignore_sent_at` |
  | `product_tour_id` | `$product_tour_id` |
  | `process_person_profile` | `$process_person_profile` |

  Legacy option properties still work, but an option overrides its legacy property. Use options.
- Pass `True` or `False` for `cookieless_mode`, `disable_skew_correction` and `process_person_profile`. PostHog also reads common forms such as `"false"`, `"off"` or `0`.

- These four options are the full set. PostHog ignores any other key in `options`. Session and window IDs stay `$session_id` and `$window_id` properties (see [Session and window IDs](#session-and-window-ids)).
- A `None` option counts as unset, so a later step can fill it.

Values apply in this order. Steps 2 to 4 fill only the options and properties that the steps before them left unset:

1. the `options` and `properties` of the call, then the call's `disable_geoip` argument, which fills `$geoip_disable`. An identify-style event also gets `process_person_profile: True` here when the call leaves it unset and the SDK did not generate its distinct ID (see [Identity and request context](#identity-and-request-context))
2. context options and tags
3. `super_options` and `super_properties`
4. values the SDK sets: `$lib` and `$lib_version` as a pair (see [SDK identity](#sdk-identity)), `$is_server` from `is_server`, `$geoip_disable` from the client's `disable_geoip` setting, system properties such as `$os` and `$python_version`, the `$feature/<key>` and `$active_feature_flags` properties from `flags` or `send_feature_flags`, `$trace_id` and `$span_id` from the active OpenTelemetry span (in `capture` and `capture_ai` only with `capture_trace_context=True`, and always in `capture_exception`), `options.process_person_profile = false` for events without a distinct ID, and `$release_id` from `POSTHOG_RELEASE_ID`
5. `before_send`, which sees the result of steps 1 to 4 and can change or remove any of it

A property set to `None` counts as set, so no later step fills it.
`$lib` and `$lib_version` are the exception: they count as set only when they are non-empty strings (see [SDK identity](#sdk-identity)).
`$set`, `$set_once`, `$groups` and `$group_set` fill one level deep.
When the call and a later step both set one of them to a dict, the later step adds only the keys the call left out.
For example, `super_properties={"$set": {"plan": "free", "source": "web"}}` and a call with `properties={"$set": {"plan": "pro"}}` send `{"plan": "pro", "source": "web"}`.

These changes follow from this order:

- Properties passed to a call now override `super_properties`.
- A `$is_server`, `$geoip_disable`, `$lib`, `$lib_version` or system property such as `$os` that you set in a call or a context tag now wins over the SDK's value. In 7.x the SDK overwrote it. `super_properties` win over the SDK's value too, so `super_properties={"$geoip_disable": False}` turns GeoIP lookup on for events, even with `disable_geoip=True`.
- Feature flag properties from `flags` or `send_feature_flags` still lose to the same key in the call's `properties`, a context tag or `super_properties`, as in 7.x.
- A `$trace_id` or `$span_id` in a context tag or `super_properties` now replaces the active OpenTelemetry span's IDs. In 7.x the span's IDs beat context tags. Setting either key in the call's `properties`, a context tag or `super_properties` stops the SDK adding both. In 7.x the SDK still added the other key.
- A `disable_geoip` argument to a call belongs to that event, so it wins over context tags and `super_properties`. A `$geoip_disable` in the call's own `properties` still wins over it. `disable_geoip=False` now sends `$geoip_disable: false`. In 7.x it sent no `$geoip_disable` property.
- The `groups` argument merges into a `$groups` property of the call, and wins key by key. In 7.x it replaced the property. MCP events merge their identity's groups into a custom `$groups` property the same way.
- A `$set`, `$set_once`, `$groups` or `$group_set` in `super_properties` no longer replaces the whole value of the call. The two merge, and the call wins key by key.
- An event without a distinct ID gets `options.process_person_profile = false`. To turn person processing on for it, set the `process_person_profile` option.
- Review your `before_send` hooks for the new event shape. To change an option, edit `options`. A hook that returns a new dict must copy `options`, or the event is sent without them. That includes the `process_person_profile = false` the SDK sets for an event without a distinct ID, so PostHog would create a person for its generated ID.
- When the server enables minimal `$feature_flag_called` events, those events now keep `$window_id`. Their options still apply, because the trim removes only properties.

## AI capture

- `capture_ai` and the AI wrapper integrations send to `/i/v1/ai/events`. The AI endpoint accepts events up to 8 MiB.
- Send `$ai_` events with `capture_ai`. `capture()` sends every event to `/i/v1/analytics/events`, including `$ai_` events. PostHog accepts them there for now, but this is temporary.
- `capture_ai` sends any event name to the AI endpoint. PostHog drops an event whose name does not start with `$ai_` as `misrouted_event`, and `on_error` reports it. The SDK writes only a debug log.
- The OpenTelemetry integration (`PostHogSpanProcessor`, `PostHogTraceExporter`) is unchanged: it still exports spans over OTLP to `/i/v0/ai/otel`.
- `enable_full_ai_capture` now controls only content: string truncation and media redaction. It no longer chooses the endpoint. `_use_ai_lane` and `_enable_multimodal_capture` still work as aliases.
- The AI integrations call your client's `capture_ai`, and never `capture`. A custom client object must implement `capture_ai` and accept its `options` keyword argument. If the client has no `capture_ai`, the wrapper, callback handler or tracing processor logs one error when you create it and captures no AI events; provider calls still work. Before, such a client got `capture` calls.
- Tests that pass a `Mock` client to an AI integration must assert on `mock.capture_ai`, not `mock.capture`.
- When an AI integration or MCP falls back to a trace, run or session ID, it turns person processing off with a per-event option. This is the only option the integrations set on their own. It wins over your `posthog_options`, `options`, `super_options` and context options. `before_send` can still change it.
- An empty or whitespace-only distinct ID passed to an AI integration, such as `posthog_distinct_id=""`, counts as missing, so the integration falls back the same way and turns person processing off. In 7.x, an empty ID gave non-streaming wrapper calls a random personless ID. Streaming calls, embeddings and LangChain used the trace or run ID with person processing on. OpenAI Agents already treated an empty ID as missing. Every integration sent a whitespace-only ID as given, with person processing on.
- The AI integrations take capture options:
  - Every wrapper method that takes `posthog_properties` also takes `posthog_options`, for the events of that call. This covers OpenAI, Anthropic and Gemini, sync and async, and the Claude Agent SDK `query()` and `PostHogClaudeSDKClient`. The name keeps it apart from the Claude Agent SDK's own `options`.
  - The Gemini clients take `posthog_options` as a default for every call. A call's `posthog_options` win key by key.
  - LangChain's `CallbackHandler` and `PostHogMiddleware`, OpenAI Agents' `instrument()` and `PostHogTracingProcessor`, and the Claude Agent SDK's `instrument()` and `PostHogClaudeAgentProcessor` take `options=` for every event. LangChain also reads `posthog_options` from run metadata, like `posthog_properties`. Like properties, the handler's or processor's options win over a run's or a query's. The `$exception` event LangChain captures for a failed run gets the same options.
  - These are the event's own options, so they win over context options and `super_options`, which still fill the keys they leave unset. `before_send` can still change them.
  - A value that is not a dict is logged as a warning and ignored. The provider call still runs, and the provider never receives `posthog_options`.

  ```python
  from posthog.ai.langchain import CallbackHandler
  from posthog.ai.openai import OpenAI

  client = OpenAI(posthog_client=posthog_client)
  client.chat.completions.create(
      model="gpt-5-mini",
      messages=[{"role": "user", "content": "Hello"}],
      posthog_distinct_id="user-1",
      posthog_options={"cookieless_mode": True},
  )

  handler = CallbackHandler(
      posthog_client, distinct_id="user-1", options={"cookieless_mode": True}
  )
  ```
- New `Client` and `AsyncPosthog` arguments for the AI lane:
  - `capture_ai_compression`: default none. `POSTHOG_CAPTURE_COMPRESSION` does not apply to it.
  - `capture_ai_max_queue_size`: default 1000
  - `capture_ai_timeout`: default 30 seconds
  - `capture_ai_max_event_bytes`: default 8 MiB plus 64 KiB. You can only lower it.
- The AI lane does not use `max_queue_size`, `timeout` or `capture_compression`. An invalid `capture_ai_*` value raises `ValueError` when you create the client.
- The module-level client has no AI lane settings. To change them, create a `Client`.
- `AsyncPosthog` has `capture_ai` and `capture_ai_immediate`.
- `AsyncPosthog` sends events with `requests` in a worker thread. It uses `httpx` only for feature flags and remote config.
- `AsyncPosthog` accepts `privacy_mode` and `enable_full_ai_capture`, with the same meaning as on `Client`. An AI integration given an `AsyncPosthog` client used to always truncate and redact media.

## MCP analytics

MCP events take options like other events, through an `event_options` argument and an `event_options` callback.

- The `PostHogMCP` capture methods (`capture_tool_call`, `capture_initialize`, `capture_tools_list`, `capture_missing_capability` and `capture_feedback`) and `McpAnalytics.capture`, on the handle `instrument()` returns, take an `event_options` argument. It sets options on that event. For a failed tool call, it also sets them on the `$exception` event.
- `MCPAnalyticsOptions(event_options=...)` sets options on every event `instrument()` captures automatically, including `$exception` events. It does not apply to `McpAnalytics.capture`. Like `event_properties`, it is a function of `(request, extra)` that returns a dict or `None`, and it can be sync or async.
- `PostHogMCP.capture()` is the core client's `capture`, so it takes `options`. The MCP capture methods take `event_options`.
- The SDK passes these options as the event's own options, so they win over context options and `super_options`.
- An event without an identity always sends `process_person_profile: False`, even when `event_options` sets it to `True`. Its distinct ID is the session ID, so person processing would create a person for each session. To process a person, identify the user with `identify` or pass `distinct_id`.
- An `event_options` value that is not a dict is logged and ignored. The event is still sent.

```python
from posthog.mcp import MCPAnalyticsOptions, PostHogMCP, instrument

instrument(
    server,
    posthog,
    MCPAnalyticsOptions(event_options=lambda request, extra: {"cookieless_mode": True}),
)

mcp_client = PostHogMCP("phc_...")
mcp_client.capture_tool_call(
    "search_docs",
    distinct_id="user-1",
    event_options={"process_person_profile": False},
)
```

`instrument()` captures `$mcp_tool_call` after the tool handler returns.
A context you open inside the handler has ended by then, so options you set in it with `set_context_option` do not reach `$mcp_tool_call`.
Events you capture inside that context still get them.
To set options on `$mcp_tool_call`, use the `event_options` callback.

MCP's `before_send` gets each event's `options` in an `options` key of `PostHogCaptureEvent`.
A hook that returns a new dict must copy them, or the event is sent without the anonymous `process_person_profile: False`.

## Batching and size limits

- An analytics event over 10 MiB is dropped and logged, because no capture request can carry it. 7.x dropped events over 900 KiB. Capture drops an event over its own per-event limit and reports it in `CaptureError.drops` as `event_too_big`.
- An AI event over `capture_ai_max_event_bytes` is dropped and logged.
- A batch stops before an event that would take it past 5 MiB. A larger event is sent alone.
- `flush()` and `shutdown()` drain the analytics and AI queues at the same time.

## OpenFeature provider

`openfeature-provider-posthog` 0.2.0 works with posthog 7.x and 8.x.
Older provider versions require posthog below 8.0, so upgrade the provider before or with posthog.
