# Migrate the Python SDK from 7.x.x to 8.0.0

The Python SDK 8.x.x sends every event to the [capture v1](https://posthog.com/docs/api/capture-v1) endpoints, `/i/v1/analytics/events` and `/i/v1/ai/events`, and removes the legacy capture path. Most apps upgrade without code changes.

Use the top right copy page button to work through this with your agents.

You don't need to make every change on the day you upgrade your SDK. Required changes fall in these categories:

- **Before you upgrade**: you should make these changes before you upgrade your SDK to 8.x.x.
- **During your upgrade**: These are changes that you need to make in the same PR that upgrades your SDK version.
- **Follow ups**: These are changes that can come in follow up PRs because 8.x.x has limited backward compatibility for methods that are to be deprecated.

## Checklist

Read the checklist first, then the sections that apply to you.

**Before you upgrade**

- [ ] You send events to a self-hosted PostHog that doesn't serve the capture v1 endpoints → [Prepare your proxy](#prepare-your-proxy)
- [ ] You send events through your own proxy → [Prepare your proxy](#prepare-your-proxy)
- [ ] A proxy redirects capture requests to another host → [Prepare your proxy](#prepare-your-proxy)
- [ ] You pass a personal API key (`phx_...`) as the project token → [Use a project token](#use-a-project-token)
- [ ] You use `openfeature-provider-posthog` below 0.2.0 → [Update OpenFeature](#update-openfeature)

**During your upgrade**

- [ ] You pass `Client` or `AsyncPosthog` constructor arguments by position after `host` → [Fix removed APIs](#fix-removed-apis)
- [ ] You call `posthog.request.post` or `posthog.request.flags` with positional arguments after `path` or `host` → [Fix removed APIs](#fix-removed-apis)
- [ ] You set `capture_mode`, `POSTHOG_CAPTURE_MODE`, or `gzip` → [Fix removed APIs](#fix-removed-apis)
- [ ] You import `CaptureMode`, `posthog.capture_mode`, `CaptureV1Error`, `posthog.capture_v1`, `request.batch_post`, `EVENTS_ENDPOINT`, or `AI_EVENTS_ENDPOINT` → [Fix removed APIs](#fix-removed-apis)
- [ ] Your `before_send` hook returns a new dict, in the client or in MCP → [Copy options in before_send](#copy-options-in-before_send)
- [ ] You test the AI integrations with a mock client and assert on `capture` → [Route AI events](#route-ai-events)
- [ ] You pass your own client object to the AI integrations → [Route AI events](#route-ai-events)
- [ ] You call `alias()` without a `distinct_id` and rely on the context distinct ID → [Fix identity calls](#fix-identity-calls)
- [ ] You turn person processing off in bulk and call `alias`, `group_identify`, `set`, or `set_once` → [Fix identity calls](#fix-identity-calls)
- [ ] You reuse one event `uuid` for more than one event → [Check event IDs](#check-event-ids)
- [ ] You expect `super_properties` to override a call's properties, including `$set` and `$groups` → [Check property precedence](#check-property-precedence)
- [ ] You set `$is_server`, `$geoip_disable`, or a system property such as `$os`, and expect the SDK's value to win → [Check property precedence](#check-property-precedence)
- [ ] You set `$lib` or `$lib_version` yourself → [Check SDK identity](#check-sdk-identity)
- [ ] An insight or dashboard filters on `$lib = posthog-python-mcp` → [Check SDK identity](#check-sdk-identity)
- [ ] You read event content from the SDK's debug log, or alert on SDK log lines → [Update error handling](#update-error-handling)
- [ ] You set `$exception_level` or another reserved exception property in `capture_exception(properties=...)` → [Check exceptions](#check-exceptions)
- [ ] You run the Django, Flask, or ASGI integration on 7.68.x to 7.70.x without tracing headers turned on → [Check tracing headers](#check-tracing-headers)

**Follow ups**

- [ ] You set legacy option properties like `$process_person_profile` → [Move to event options](#move-to-event-options)
- [ ] You capture `$ai_` events with `capture()` → [Send AI events with capture_ai](#send-ai-events-with-capture_ai)
- [ ] You use `trust_tracing_headers` or `_use_ai_lane`, set `posthog.capture_mode`, or pass a `gzip=` keyword to `request.post` → [Rename deprecated settings](#rename-deprecated-settings)

## Before you upgrade

### Prepare your proxy

8.x.x sends events to these endpoints:

| Method | Endpoint |
| --- | --- |
| `capture`, `set`, `set_once`, `alias`, `group_identify`, `capture_exception`, `AsyncPosthog.capture_immediate` | `/i/v1/analytics/events` |
| `capture_ai`, `capture_ai_immediate`, and the AI wrapper integrations | `/i/v1/ai/events` |
| OpenTelemetry (`posthog.ai.otel`) | `/i/v0/ai/otel`, unchanged |

If you send events to a self-hosted PostHog, check that it serves both `/i/v1/analytics/events` and `/i/v1/ai/events` before you upgrade. Self-hosted PostHog serves the AI endpoint from a separate capture service, so check each route. An endpoint that isn't served drops every event sent to it.

If you send events through your own reverse proxy, update it before you upgrade:

- **Forward both routes.** Forward `/i/v1/analytics/events` and `/i/v1/ai/events` to PostHog.
- **Pass the headers through unchanged.** Pass `Authorization`, every `PostHog-*` header, `Content-Type`, `Content-Encoding`, and `User-Agent`. `Authorization` holds the project token, sent as `Bearer <token>`. PostHog answers `401` without `Authorization` and `400` without the others, and the batch fails.
- **Accept request bodies up to 20 MB.** AI requests are often larger than 1 MB, the default limit in nginx and ingress-nginx. A `413` from the proxy fails the batch.

PostHog's managed reverse proxy already does this. See the [proxy guides](https://posthog.com/docs/advanced/proxy).

The SDK follows a `307` or `308` redirect only to the origin of `host`, at most 5 times. The origin is the same scheme, host, and port.

Any other redirect fails the batch and reaches `on_error`. In 7.x.x the sync client followed redirects to any origin.

If a proxy redirects capture requests to another host, point `host` at the final host.

### Use a project token

A personal API key (`phx_...`) passed as the project token disables the client and logs one error, as an empty key does. Capture rejects personal API keys, and the SDK no longer sends one with every request.

### Update OpenFeature

`openfeature-provider-posthog` 0.2.0 works with `posthog` 7.x.x and 8.x.x. Older provider versions require `posthog` below 8.0.0, so upgrade the provider before or with `posthog`.

## During your upgrade

### Fix removed APIs

8.x.x removes these options and APIs:

| Removed | Use instead |
| --- | --- |
| `capture_mode`, `CaptureMode`, `POSTHOG_CAPTURE_MODE` | Nothing |
| `gzip=True` | `capture_compression` |
| `CaptureV1Error` | `CaptureError` |
| `posthog.capture_v1` | `posthog.capture_event` and `posthog.capture_send` |
| `request.batch_post`, `async_batch_post`, `EVENTS_ENDPOINT`, `AI_EVENTS_ENDPOINT` | Send events through a client |
| The `gzip` parameter of `request.post` and `request.flags` | Nothing |
| The `backoff` dependency | Your own requirements, if your code imports it |

Capture v1 is the only path. Setting `posthog.capture_mode` on the module has no effect.

For gzip, set `capture_compression=CaptureCompression.GZIP`. `capture_compression` defaults to no compression. `POSTHOG_CAPTURE_COMPRESSION` still works.

Replace `from posthog.capture_v1 import CaptureV1Error` with `from posthog import CaptureError`. There is no alias.

The parameters after `path` in `request.post` and after `host` in `request.flags` are keyword-only, so a 7.x.x call that passes them by position raises `TypeError`. A `gzip=` keyword raises no error. It's sent as a field in the request body.

The `posthoganalytics` package has the same changes. For example, import `CaptureError` from `posthoganalytics`.

Constructor arguments after `host` are keyword-only:

```python
# 7.x.x
Posthog("<api_key>", "https://us.i.posthog.com", True)

# 8.x.x
Posthog("<api_key>", host="https://us.i.posthog.com", debug=True)
```

### Copy options in before_send

Review your `before_send` hooks for the new event shape. Capture v1 sends processing options in an `options` object, next to `properties`.

To change an option, edit `options`. A hook that returns a new dict must copy `options`, or the event is sent without them.

That includes the `process_person_profile = false` the SDK sets for an event without a distinct ID. Without it, PostHog would create a person for the event's generated ID.

A hook that spreads the event into the new dict keeps `options`:

```python
def remove_email(event):
    properties = {key: value for key, value in event["properties"].items() if key != "email"}
    return {**event, "properties": properties}
```

The same applies to MCP's `before_send`. It gets each event's `options` in an `options` key of `PostHogCaptureEvent`.

An MCP hook that returns a new dict must copy them, or the event is sent without the anonymous `process_person_profile: False`.

### Route AI events

8.x.x routes AI events to their own endpoint:

- **The endpoint.** `capture_ai` and the AI wrapper integrations send to `/i/v1/ai/events`. The AI endpoint accepts events up to 8 MiB.
- **Event names.** `capture_ai` sends any event name to the AI endpoint. PostHog drops an event whose name doesn't start with `$ai_` as `misrouted_event`, and `on_error` reports it. The SDK writes only a debug log.
- **Custom clients.** The AI integrations call your client's `capture_ai`, and never `capture`. A custom client object must implement `capture_ai` and accept its `options` keyword argument.
- **Clients without `capture_ai`.** The wrapper, callback handler, or tracing processor logs one error when you create it and captures no AI events. Provider calls still work. Before, such a client got `capture` calls.
- **Mock clients.** Tests that pass a `Mock` client to an AI integration must assert on `mock.capture_ai`, not `mock.capture`.
- **OpenTelemetry.** The OpenTelemetry integration (`PostHogSpanProcessor`, `PostHogTraceExporter`) is unchanged. It still exports spans over OTLP to `/i/v0/ai/otel`.
- **Content capture.** `enable_full_ai_capture` now controls only content: string truncation and media redaction. It no longer chooses the endpoint.
- **Fallback IDs.** When an AI integration or MCP falls back to a trace, run, or session ID, it turns person processing off with a per-event option. This is the only option the integrations set on their own. It wins over your `posthog_options`, `options`, `super_options`, and context options. `before_send` can still change it.
- **Empty distinct IDs.** An empty or whitespace-only distinct ID passed to an AI integration, such as `posthog_distinct_id=""`, counts as missing. The integration falls back the same way and turns person processing off.
- **Empty distinct IDs in 7.x.** An empty ID gave non-streaming wrapper calls a random personless ID. Streaming calls, embeddings, and LangChain used the trace or run ID with person processing on. OpenAI Agents already treated an empty ID as missing. Every integration sent a whitespace-only ID as given, with person processing on.
- **Async capture.** `AsyncPosthog` has `capture_ai` and `capture_ai_immediate`. It sends events with `requests` in a worker thread, and uses `httpx` only for feature flags and remote config.
- **Async content settings.** `AsyncPosthog` accepts `privacy_mode` and `enable_full_ai_capture`, with the same meaning as on `Client`. An AI integration given an `AsyncPosthog` client used to always truncate and redact media.

`Client` and `AsyncPosthog` have new arguments for the AI lane:

| Argument | Default |
| --- | --- |
| `capture_ai_compression` | No compression |
| `capture_ai_max_queue_size` | 1000 |
| `capture_ai_timeout` | 30 seconds |
| `capture_ai_max_event_bytes` | 8 MiB plus 64 KiB |

`POSTHOG_CAPTURE_COMPRESSION` doesn't apply to `capture_ai_compression`. You can only lower `capture_ai_max_event_bytes`.

The AI lane doesn't use `max_queue_size`, `timeout`, or `capture_compression`. An invalid `capture_ai_*` value raises `ValueError` when you create the client.

The module-level client has no AI lane settings. To change them, create a `Client`.

### Fix identity calls

- **Empty IDs.** An empty or whitespace-only `distinct_id` counts as missing, so the event gets a generated personless distinct ID. 7.x.x sent a whitespace-only ID, and capture dropped the event.
- **Generated IDs.** Generated personless distinct IDs are UUIDv7. 7.x.x generated UUIDv4.
- **Group fallback.** `group_identify` uses the `distinct_id` you pass, then the context distinct ID, then `$<group_type>_<group_key>`. 7.x.x fell back to a random ID, which created a new person on every call.
- **Long group IDs.** If `$<group_type>_<group_key>` is over 200 bytes, pass `distinct_id`. PostHog drops an event whose distinct ID is over 200 bytes.
- **Long distinct IDs.** PostHog drops an event whose `distinct_id` is over 200 UTF-8 bytes (`distinct_id_too_large`), and `on_error` reports it. 7.x.x sent events to legacy capture, which shortened the ID to 200 characters and kept the event.
- **Group sessions.** `group_identify` no longer adds the context session ID. A session takes the person of its latest event, and this event's distinct ID isn't the session's user.
- **Alias.** `alias()` needs an explicit `distinct_id`. It no longer falls back to the context distinct ID, which can come from a request header the client controls. Without one, the SDK drops the call with a warning.
- **Context values.** Context tags and context options now apply to `alias` and `group_identify`, below the call's own values, as they do for `capture`.

`process_person_profile: False` reaches identity calls too. Where you set it decides which calls it reaches:

- **A context option, `super_options`, or a `$process_person_profile` entry in `super_properties`.** It reaches `alias`, `group_identify`, `set`, and `set_once`.
- **A `$process_person_profile` context tag.** It reaches only `alias` and `group_identify`, because `set` and `set_once` add context tags to the person properties they set.

Context tags and options are new on `alias` and `group_identify`. `super_properties` already reached them in 7.x.

PostHog drops the `$create_alias` and `$groupidentify` events when person processing is off, with an ingestion warning. They never merge a person or update a group.

PostHog keeps the event from `set` or `set_once` but ignores its person update. Once the person exists, events sent with `process_person_profile: False` are still linked to it and billed as identified events.

Capture accepts these events, so `on_error` doesn't report the drop. To keep them, pass the option on the call, which wins over context and `super_options`, or set it in `before_send`:

```python
posthog.alias(previous_id="anon-1", distinct_id="user-1", options={"process_person_profile": True})
posthog.group_identify("company", "acme", {"name": "Acme"}, options={"process_person_profile": True})
posthog.set(distinct_id="user-1", properties={"plan": "pro"}, options={"process_person_profile": True})
```

### Check event IDs

- **Duplicates.** Each event needs its own UUID. Capture keeps the first event for each UUID in a request and drops the later copies. `on_error` reports each dropped copy in `CaptureError.drops` as `(uuid, "duplicate_event_uuid")`. 7.x.x sent every copy. Older PostHog versions reject the whole batch with a 400 `duplicate_event_uuid` instead.
- **Accepted forms.** The SDK accepts a UUID with hyphens, 32 hex digits, `{...}` braces, or a `urn:uuid:` prefix, in any case. It sends the lowercase hyphenated form and returns that form from `capture`.
- **Other values.** The SDK replaces any other value with a generated UUID. It logs one warning that names the rule, not the value. An empty string counts as unset, so the SDK generates a UUID without a warning. This applies to `AsyncPosthog` too.
- **Generated UUIDs.** Generated UUIDs are UUIDv7.

### Check property precedence

An event's options and properties come from five steps, in this order. Steps 2 to 4 fill only the options and properties that the steps before them left unset:

1. The `options` and `properties` of the call, then the call's `disable_geoip` argument, which fills `$geoip_disable`.
2. Context options and tags.
3. `super_options` and `super_properties`.
4. Values the SDK sets:
   - `$lib` and `$lib_version` as a pair (see [Check SDK identity](#check-sdk-identity))
   - `$is_server` from `is_server`
   - `$geoip_disable` from the client's `disable_geoip` setting
   - System properties such as `$os` and `$python_version`
   - The `$feature/<key>` and `$active_feature_flags` properties from `flags` or `send_feature_flags`
   - `$trace_id` and `$span_id` from the active OpenTelemetry span (in `capture` and `capture_ai` only with `capture_trace_context=True`, and always in `capture_exception`)
   - `options.process_person_profile = false` for events without a distinct ID
   - `$release_id` from `POSTHOG_RELEASE_ID`
5. `before_send`, which sees the result of steps 1 to 4 and can change or remove any of it.

A property set to `None` counts as set, so no later step fills it. A `None` option counts as unset, so a later step can fill it.

`$lib` and `$lib_version` are the exception. They count as set only when they're non-empty strings (see [Check SDK identity](#check-sdk-identity)).

`$set`, `$set_once`, `$groups`, and `$group_set` fill one level deep. When the call and a later step both set one of them to a dict, the later step adds only the keys the call left out.

For example, `super_properties={"$set": {"plan": "free", "source": "web"}}` and a call with `properties={"$set": {"plan": "pro"}}` send `{"plan": "pro", "source": "web"}`.

These changes follow from this order:

- **Call properties.** Properties passed to a call now override `super_properties`.
- **SDK values.** A `$is_server`, `$geoip_disable`, `$lib`, `$lib_version`, or system property such as `$os` that you set in a call or a context tag now wins over the SDK's value. In 7.x.x the SDK overwrote it.
- **SDK values and `super_properties`.** `super_properties` win over the SDK's value too. So `super_properties={"$geoip_disable": False}` turns GeoIP lookup on for events, even with `disable_geoip=True`.
- **Feature flag properties.** Feature flag properties from `flags` or `send_feature_flags` still lose to the same key in the call's `properties`, a context tag, or `super_properties`, as in 7.x.
- **Trace IDs.** A `$trace_id` or `$span_id` in a context tag or `super_properties` now replaces the active OpenTelemetry span's IDs. In 7.x.x the span's IDs beat context tags.
- **Trace ID pairs.** Setting either key in the call's `properties`, a context tag, or `super_properties` stops the SDK adding both. In 7.x.x the SDK still added the other key.
- **The `disable_geoip` argument.** A `disable_geoip` argument to a call belongs to that event, so it wins over context tags and `super_properties`. A `$geoip_disable` in the call's own `properties` still wins over it.
- **`disable_geoip=False`.** It now sends `$geoip_disable: false`. In 7.x.x it sent no `$geoip_disable` property.
- **The `groups` argument.** It merges into a `$groups` property of the call, and wins key by key. In 7.x.x it replaced the property. MCP events merge their identity's groups into a custom `$groups` property the same way.
- **Merged dicts.** A `$set`, `$set_once`, `$groups`, or `$group_set` in `super_properties` no longer replaces the whole value of the call. The two merge, and the call wins key by key.
- **Events without a distinct ID.** An event without a distinct ID gets `options.process_person_profile = false`. To turn person processing on for it, set the `process_person_profile` option.
- **Minimal flag events.** When the server enables minimal `$feature_flag_called` events, those events now keep `$window_id`. Their options still apply, because the trim removes only properties.

### Check SDK identity

By default every event carries `$lib: "posthog-python"` and the installed package version in `$lib_version`. PostHog stores the values the event is sent with.

You can set them in a call's `properties`, a context tag, or `super_properties`. Like any other property, each one comes from the most specific place that sets it, so set both in the same place.

`$lib` and `$lib_version` count as set only when they're non-empty strings. `before_send` sees both and can change or remove them.

If you set `$lib` without `$lib_version`, the SDK adds its own version only when `$lib` is `posthog-python`. The SDK replaces a `$lib_version` that has no `$lib` with its own.

| You set | 8.x.x sends | 7.x.x sent |
| --- | --- | --- |
| `$lib` or `$lib_version` in a call or a context tag | Your value | The SDK's value |
| `super_properties={"$lib": "my-lib"}` | No `$lib_version` | The SDK version |
| `super_properties={"$lib_version": "1.0"}` without a `$lib` | The SDK's `$lib` and version | `1.0` |

If an event reaches PostHog without `$lib`, PostHog fills both from the SDK name and version your request sends. If `$lib` is that SDK name and `$lib_version` is missing, empty, or not a string, PostHog fills `$lib_version` the same way.

MCP analytics events from `posthog.mcp.instrument()` or `PostHogMCP` report `posthog-python-mcp` and the installed package version. The integration sets both in each event's own properties, so neither a context tag nor `super_properties` replaces them, but `before_send` can.

In 7.x.x the MCP integration relabeled the whole client, or the default client when `instrument()` got none. That covered the app's own events, the request headers, and feature flag requests.

In 8.x.x those report `posthog-python`, so one client can serve both.

Custom events from `PostHogMCP.capture()` now report `posthog-python`. In 7.x.x they reported `posthog-python-mcp`.

Custom events from `McpAnalytics.capture()`, the handle `instrument()` returns, still report `posthog-python-mcp`. If an insight or dashboard filters on `$lib = posthog-python-mcp`, check that it still finds the events you expect.

### Update error handling

Capture v1 returns a result for every event, even when the request succeeds. An event can be dropped inside a 2xx response, for example when it's over a product quota.

8.x.x reports those events as failures. When the whole request is over the billing limit, capture returns a `402` with no per-event results instead.

Every capture failure is a `CaptureError`, a subclass of `APIError`. It has `endpoint`, `request_id`, and `attempts`, and these fields:

| Field | Holds |
| --- | --- |
| `status` | The HTTP status, or `0` when the request never got a response |
| `drops` | `(uuid, details)` pairs for the events capture dropped in a 2xx response |
| `retry_exhausted` | The UUIDs still waiting for a retry after the last attempt |
| `event_results` | A `CaptureEventResult(result, details)` for each UUID that got a result in a 2xx response |
| `verdict_summary()` | Counts such as `drop/llm_events_over_quota=1, retry/not_persisted=1` |

When `status` is `0`, `__cause__` holds the network error. In 7.x.x, `on_error` received the raw `requests` exception.

`CaptureEventResult` comes from `posthog.capture_send`. An event missing from `event_results` never got a result.

`on_error` and the SDK's log lines change with it:

- **Every failure.** `on_error(error, batch)` receives every failure, including in `sync_mode` and from `AsyncPosthog.capture_immediate`.
- **Logs with `on_error`.** With `on_error` set, the SDK logs nothing for a failed batch.
- **Logs without `on_error`.** The SDK logs one line per failed batch, for example `2 event(s) not persisted by /i/v1/analytics/events: ...`. The line never contains event content or the server's response text. After a 2xx response it also counts the lost events by PostHog's reason, for example `drop/event_too_big=1`.
- **Removed log lines.** The 7.x.x lines `error uploading: ...`, `async capture upload failed (...)`, and `Immediate async capture failed (...)` are gone. In `sync_mode`, a failed request no longer logs the 7.x.x `Error in capture: ...` line, or the same line for another method. Update any log-based alerts that match these lines.
- **Failures in `sync_mode`.** A failed `capture()` calls `on_error` and returns `None`. With `debug=True` it calls `on_error` and then re-raises the error.
- **Retries in `sync_mode`.** The SDK retries a failed request like a background batch, in the calling thread. With the default `max_retries` of 3, a call makes up to 4 attempts. Each attempt can take up to `timeout` (`capture_ai_timeout` for AI events). 7.x.x made one attempt.
- **Blocking in `sync_mode`.** With the defaults, an analytics event whose requests keep timing out blocks the call for about a minute.
- **Captures inside `on_error`.** If a capture inside `on_error` fails, the SDK logs the failure and doesn't call `on_error` again.
- **Event content.** The SDK's own log lines no longer contain event content. The debug line for a queued event names the event and the lane. The invalid timestamp warning no longer includes the value. To inspect events, use `before_send`.
- **Hook errors.** When your `before_send` hook raises, the error line names the event and the exception type, not the exception message. The message can hold the data the hook was removing. The SDK logs the traceback at debug level.

Retries differ from 7.x.x in four ways:

| Behavior | 7.x.x | 8.x.x |
| --- | --- | --- |
| Statuses retried | `429` and every 5xx, but not `404` | `404`, `408`, `429`, `500`, `502`, `503`, `504`, and network errors |
| Connection tries in each attempt | Up to three | One |
| First retry wait | Up to 1 second | 100 ms |
| `Consumer` default retries | 10 | 3 |

In 8.x.x, any other error status fails the batch at once, including the other 5xx codes. In 7.x.x, a capture request could block for up to three times the `timeout` per attempt.

In 8.x.x the wait doubles each attempt, up to 30 seconds. Each wait varies randomly by up to 25%, so many clients don't retry at the same moment.

A `Retry-After` header sets a minimum wait, up to 30 seconds.

Capture itself doesn't return `429`, but a proxy in front of it can.

A `404` that lasts through every attempt can mean the PostHog deployment has no capture v1 route, for example an older self-hosted install.

### Check dropped events

Capture rejects a whole batch when one event can't be parsed. 8.x.x drops only the invalid event, so the rest of the batch is delivered.

| Event | 8.x.x | 7.x.x |
| --- | --- | --- |
| The name is missing, `None`, empty, or not a string | Dropped, with a warning | Sent |
| A `before_send` hook sets `distinct_id` to `None` or a non-string | Dropped, with a warning | Sent, and capture rejected the whole batch |
| The name is over 200 UTF-8 bytes | Dropped by PostHog as `event_name_too_long` | Sent, with no name limit |
| A `NaN` or infinite `float` value | Sent as `null`, as in 7.75.0 and later | Invalid JSON before 7.75.0, so capture rejected the whole batch |
| It can't be serialized to JSON, for example with a circular reference | Dropped and logged | Logged the same line |
| A property or option key JSON can't represent, such as a tuple | Key dropped without a log | Event dropped |
| A key is an `int`, finite `float`, `bool`, or `None` | Key converted to a string | Key converted to a string |
| A key is a `NaN` or infinite `float` | Event dropped | Event dropped |

The SDK logs one warning per reason per process for a dropped name or `distinct_id`. It doesn't convert the hook's `distinct_id` or make up an ID.

`on_error` reports an event that PostHog drops for its name. A `NaN` value in `$set` sets that person property to `null`. A circular reference logs `Error in capture: maximum recursion depth exceeded` with a long traceback.

Capture v1 sends `$session_id` and `$window_id` as top-level event fields, not as properties. The SDK moves them out of `properties` for you, by the value's type:

- **A string.** The SDK sends it as given, including `""`.
- **`None`.** It counts as unset, and the SDK removes it.
- **Any other value.** The SDK removes it and doesn't send it, because capture would reject the whole batch. 7.x.x sent it as a property. The SDK logs a warning for each one, with the key and the value's type but not the value.

8.x.x applies these size and batch rules:

- **Analytics events.** The SDK drops and logs an analytics event over 10 MiB, because no capture request can carry it. 7.x.x dropped events over 900 KiB.
- **Capture's own limit.** Capture drops an event over its own per-event limit and reports it in `CaptureError.drops` as `event_too_big`.
- **AI events.** The SDK drops and logs an AI event over `capture_ai_max_event_bytes`.
- **Batches.** A batch stops before an event that would take it past 5 MiB. A larger event is sent alone.
- **Queues.** `flush()` and `shutdown()` drain the analytics and AI queues at the same time.

### Check exceptions

- **The `level=` argument.** `capture_exception` takes a `level=` argument on `Client`, `AsyncPosthog`, and the module. It sets `$exception_level`, for example `"warning"` or `"fatal"`. The default is `"error"`, and an unknown value counts as unset.
- **Reserved properties.** `capture_exception` ignores reserved exception properties in `properties`, such as `$exception_list`, `$exception_level`, and `$exception_source`. In 7.x.x they overrode the SDK's values, with a `DeprecationWarning`. Use `level=` instead of `$exception_level`.
- **Async level.** `AsyncPosthog.capture_exception` now sends `$exception_level`.
- **Async trace IDs.** `AsyncPosthog.capture_exception` now adds `$trace_id` and `$span_id` from the active OpenTelemetry span, like `Client.capture_exception`. In 7.x.x it added them only with `capture_trace_context=True`.

### Check tracing headers

The Django, Flask, and ASGI integrations read the `X-PostHog-Distinct-Id` and `X-PostHog-Session-Id` headers by default, as in 7.71.0 and later. The headers only fill a missing distinct ID or session ID, and any client can set them, so never use them for authentication.

- **Turn it off.** Set `POSTHOG_MW_CAPTURE_TRACING_HEADERS = False` in Django, or pass `capture_tracing_headers=False` to the Flask or ASGI integration.
- **Upgrades from 7.68.x, 7.69.x, or 7.70.x.** If you never turned these headers on, they're now read.

## Follow ups

### Move to event options

Capture v1 sends processing options in an `options` object, next to `properties`. `capture`, `capture_ai`, `set`, `set_once`, `alias`, `group_identify`, and `capture_exception` take an `options` argument:

```python
posthog.capture("signed_up", distinct_id="user-1", options={"cookieless_mode": True})
```

The MCP capture methods take `event_options` instead (see [Add MCP event options](#add-mcp-event-options)). The AI integrations take `posthog_options` (see [Send AI events with capture_ai](#send-ai-events-with-capture_ai)).

- **Every event.** `super_options`, a new `Client`, `AsyncPosthog`, and module setting, sets options on every event, like `super_properties`.
- **One context.** `set_context_option(key, value)` sets an option for the current context, like `tag()`. `get_context_options()` returns the options of the current context. Both exist on `Client` and as module functions.
- **Async contexts.** `AsyncPosthog` has no context methods, like it has no `tag()`, so call the module functions. They apply to every client in the context.
- **Celery workers.** The Celery integration propagates context options to workers, like tags. A propagated `process_person_profile: True` turns person processing on for worker events without a distinct ID. Each of those events creates a person.
- **Celery eager mode.** In eager mode (`task_always_eager`), Celery publishes nothing, so the distinct ID, tags, and options don't reach the task's events. To carry them, pass the task's PostHog headers with `task.apply(headers=...)` or `task.apply_async(headers=...)`.
- **Methods reached.** Context tags and options reach `capture`, `capture_ai`, `capture_exception`, `alias`, and `group_identify`. Context options also reach `set` and `set_once`. `super_properties` and `super_options` reach every method.
- **Validation.** Options are sent as given. PostHog validates them. It drops an event with an option value it can't read, such as a list for `cookieless_mode`, as `invalid_options`, and `on_error` reports it.
- **Values that aren't a dict.** The SDK logs an `options` value that isn't a dict as an error and ignores it. It still sends the event.
- **Boolean options.** Pass `True` or `False` for `cookieless_mode`, `disable_skew_correction`, and `process_person_profile`. PostHog also reads common forms such as `"false"`, `"off"`, or `0`.

Each option replaces a legacy option property:

| Option | Replaces |
| --- | --- |
| `cookieless_mode` | `$cookieless_mode` |
| `disable_skew_correction` | `$ignore_sent_at` |
| `product_tour_id` | `$product_tour_id` |
| `process_person_profile` | `$process_person_profile` |

Legacy option properties still work, but an option overrides its legacy property. Use options.

These four options are the full set. PostHog ignores any other key in `options`.

Session and window IDs stay `$session_id` and `$window_id` properties (see [Check dropped events](#check-dropped-events)).

### Send AI events with capture_ai

Send `$ai_` events with `capture_ai`. `capture()` sends every event to `/i/v1/analytics/events`, including `$ai_` events.

PostHog accepts them there for now, but this is temporary.

The AI integrations take capture options:

- **Wrapper methods.** Every wrapper method that takes `posthog_properties` also takes `posthog_options`, for the events of that call. This covers the sync and async OpenAI, Anthropic, and Gemini wrappers, and the Claude Agent SDK `query()` and `PostHogClaudeSDKClient`. The name keeps it apart from the Claude Agent SDK's own `options`.
- **Gemini clients.** The Gemini clients take `posthog_options` as a default for every call. A call's `posthog_options` win key by key.
- **Handlers and processors.** LangChain's `CallbackHandler` and `PostHogMiddleware` take `options=` for every event. So do OpenAI Agents' `instrument()` and `PostHogTracingProcessor`, and the Claude Agent SDK's `instrument()` and `PostHogClaudeAgentProcessor`.
- **Run metadata.** LangChain also reads `posthog_options` from run metadata, like `posthog_properties`. Like properties, the handler's or processor's options win over a run's or a query's.
- **Failed runs.** The `$exception` event LangChain captures for a failed run gets the same options.
- **Precedence.** These are the event's own options, so they win over context options and `super_options`, which still fill the keys they leave unset. `before_send` can still change them.
- **Values that aren't a dict.** The SDK logs a value that isn't a dict as a warning and ignores it. The provider call still runs, and the provider never receives `posthog_options`.

Pass `posthog_options` to a wrapper call, or `options` to a handler:

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

### Add MCP event options

MCP events take options like other events, through an `event_options` argument and an `event_options` callback.

- **The argument.** The `PostHogMCP` capture methods and `McpAnalytics.capture`, on the handle `instrument()` returns, take an `event_options` argument. The capture methods are `capture_tool_call`, `capture_initialize`, `capture_tools_list`, `capture_missing_capability`, and `capture_feedback`.
- **What the argument sets.** It sets options on that event. For a failed tool call, it also sets them on the `$exception` event.
- **The callback.** `MCPAnalyticsOptions(event_options=...)` sets options on every event `instrument()` captures automatically, including `$exception` events. It doesn't apply to `McpAnalytics.capture`.
- **The callback's shape.** Like `event_properties`, it's a function of `(request, extra)` that returns a dict or `None`, and it can be sync or async.
- **The core `capture`.** `PostHogMCP.capture()` is the core client's `capture`, so it takes `options`. The MCP capture methods take `event_options`.
- **Precedence.** The SDK passes these options as the event's own options, so they win over context options and `super_options`.
- **Events without an identity.** An event without an identity always sends `process_person_profile: False`, even when `event_options` sets it to `True`. Its distinct ID is the session ID, so person processing would create a person for each session. To process a person, identify the user with `identify` or pass `distinct_id`.
- **Values that aren't a dict.** The SDK logs an `event_options` value that isn't a dict and ignores it. It still sends the event.

Set the callback in `instrument()`, or pass the argument to a capture method:

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

`instrument()` captures `$mcp_tool_call` after the tool handler returns. A context you open inside the handler has ended by then, so options you set in it with `set_context_option` don't reach `$mcp_tool_call`.

Events you capture inside that context still get them. To set options on `$mcp_tool_call`, use the `event_options` callback.

### Rename deprecated settings

These old names still work or have no effect in 8.x.x:

| Old | New |
| --- | --- |
| `trust_tracing_headers`, `POSTHOG_MW_TRUST_TRACING_HEADERS` | `capture_tracing_headers`, `POSTHOG_MW_CAPTURE_TRACING_HEADERS` |
| `_use_ai_lane`, `_enable_multimodal_capture` | `enable_full_ai_capture` |
| `posthog.capture_mode` | Nothing |
| A `gzip=` keyword on `request.post` or `request.flags` | Nothing |

The 7.68.x tracing header names still work, but are deprecated. `_use_ai_lane` and `_enable_multimodal_capture` still work as aliases.

Setting `posthog.capture_mode` on the module has no effect. A `gzip=` keyword raises no error, and it's sent as a field in the request body.
