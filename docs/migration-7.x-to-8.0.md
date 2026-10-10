# Migrating from posthog 7.x to 8.0

posthog 8.0 sends every event through capture v1 and removes the legacy capture path.
Most apps upgrade without code changes.
Read the checklist first, then the sections that apply to you.

## Checklist

You need to change code if your app does any of these:

- passes `Client` or `AsyncPosthog` constructor arguments by position after `host`
- calls `posthog.request.post` or `posthog.request.flags` with positional arguments after `path` or `host`
- sets `$exception_level` or another reserved exception property in `capture_exception(properties=...)`
- sets `capture_mode`, `POSTHOG_CAPTURE_MODE` or `gzip`
- imports `CaptureV1Error`, `posthog.capture_v1`, `request.batch_post`, `EVENTS_ENDPOINT` or `AI_EVENTS_ENDPOINT`
- sends events to a self-hosted PostHog that does not serve the capture v1 endpoints
- sends events through a proxy that redirects capture requests to another host
- reuses one event `uuid` for more than one event
- sets `$process_person_profile` to turn person processing on for events without a distinct ID
- passes strings such as `"true"` for `$cookieless_mode`, `$ignore_sent_at` or `$process_person_profile`
- expects `super_properties` to override properties passed to a single call, including `$set` and `$groups`
- sets `$is_server`, `$geoip_disable` or a system property such as `$os` and expects the SDK's value to win
- tests the AI integrations with a mock client and asserts on `capture`
- passes its own client object to the AI integrations
- sets `$lib` or `$lib_version`, or filters on `$lib = "posthog-python-mcp"`

## Endpoints

| Method | Endpoint |
| --- | --- |
| `capture`, `set`, `set_once`, `alias`, `group_identify`, `capture_exception` | `/i/v1/analytics/events` |
| `capture_ai`, `capture_ai_immediate`, and every built-in AI integration | `/i/v1/ai/events` |

If you send events to a self-hosted PostHog, check that it serves both endpoints before you upgrade.
An endpoint that is not served drops every event sent to it.

The SDK follows a `307` or `308` redirect only to the origin of `host` (same scheme, host and port), at most 5 times.
Any other redirect fails the batch and reaches `on_error`.
In 7.x the sync client followed redirects to any origin.
If a proxy redirects capture requests to another host, point `host` at the final host.

## SDK identity

PostHog sets `$lib` and `$lib_version` on every event from the `PostHog-Sdk-Info` request header, which is always `posthog-python/<version>`.
The SDK removes `$lib` and `$lib_version` from the properties it sends.
A value you set in a call, in `super_properties` or in `before_send` does not reach PostHog.

MCP instrumentation no longer relabels the client.
In 7.x, `posthog.mcp.instrument()` and `PostHogMCP` set the client's identity to `posthog-python-mcp`.
With the default client, or any client the app also used, every event and feature flag request from the app then reported `posthog-python-mcp`, not only the MCP events.
In 8.0, MCP events report `posthog-python` like all other events.
To find MCP traffic, filter on the `$mcp_*` events and properties instead of `$lib`.

## Removed options and APIs

| Removed | Use instead |
| --- | --- |
| `capture_mode`, `CaptureMode`, `POSTHOG_CAPTURE_MODE` | Nothing. Capture v1 is the only path. Setting `posthog.capture_mode` on the module has no effect. |
| `gzip=True` | `capture_compression=CaptureCompression.GZIP`. The default is no compression. `POSTHOG_CAPTURE_COMPRESSION` still works. |
| `CaptureV1Error` (`from posthog.capture_v1 import CaptureV1Error`) | `CaptureError` (`from posthog import CaptureError`). There is no alias. |
| `posthog.capture_v1` | `posthog.capture_event` and `posthog.capture_send` |
| `request.batch_post`, `async_batch_post`, `EVENTS_ENDPOINT`, `AI_EVENTS_ENDPOINT` | Nothing. Send events through a client. |
| The `gzip` parameter of `request.post` and `request.flags` | Nothing. The parameters after `path` in `post` and after `host` in `flags` are keyword-only, so a 7.x call that passes them by position raises `TypeError`. |
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
- Without `on_error`, the SDK logs one line per failed batch, for example `2 event(s) not persisted by /i/v1/analytics/events: ...`. The line never contains event content or the server's response text.
- The 7.x lines `error uploading: ...`, `async capture upload failed (...)` and `Immediate async capture failed (...)` are gone. Update any log-based alerts that match them.
- In `sync_mode`, a failed `capture()` calls `on_error` and returns `None`. With `debug=True` it calls `on_error` and then re-raises the error.
- If a capture inside `on_error` fails, the SDK logs the failure and does not call `on_error` again.
- The SDK retries `408`, `500`, `502`, `503` and `504` responses and network errors. Other error statuses fail the batch at once, including `429` and the other 5xx codes. 7.x also retried `429` and every 5xx. Capture itself does not return `429`; a proxy in front of it can.
- Retries start at 100 ms and double each attempt, up to 30 seconds. A `Retry-After` header sets a minimum wait, up to 30 seconds. The `Consumer` default is 3 retries, down from 10.

## Event uuids

- Each event needs its own uuid. Capture rejects a whole batch that contains the same uuid twice, so the other events in that batch are lost too.
- The SDK accepts a uuid with hyphens, 32 hex digits, `{...}` braces or a `urn:uuid:` prefix, in any case. It sends the lowercase hyphenated form and returns that form from `capture`.
- Any other value is replaced with a generated uuid, and the SDK logs one warning that names the rule, not the value. An empty string counts as unset, so the SDK generates a uuid without a warning. This applies to `AsyncPosthog` too.
- Generated uuids are UUIDv7.

## Session and window IDs

Capture v1 sends `$session_id` and `$window_id` as top-level event fields, not as properties.
The SDK moves them out of `properties` for you.

- A string is sent as given, including `""`.
- `None` counts as unset and is removed.
- Any other value is removed and not sent, because capture would reject the whole batch. 7.x sent it as a property. The SDK logs a warning for each one, with the key and the value's type but not the value.

## Exceptions

- `capture_exception` takes a `level=` argument on `Client`, `AsyncPosthog` and the module. It sets `$exception_level`, for example `"warning"` or `"fatal"`. The default is `"error"`, and an unknown value counts as unset.
- `capture_exception` ignores reserved exception properties in `properties`, such as `$exception_list`, `$exception_level` and `$exception_source`. In 7.x they overrode the SDK's values, with a `DeprecationWarning`. Use `level=` instead of `$exception_level`.
- `AsyncPosthog.capture_exception` now sends `$exception_level`.

## Event options

Capture v1 sends processing options in an `options` object, next to `properties`.

- `capture`, `capture_ai`, `set`, `set_once`, `alias`, `group_identify` and `capture_exception` take an `options` argument:

  ```python
  posthog.capture("signed_up", distinct_id="user-1", options={"cookieless_mode": True})
  ```

- `super_options`, a new `Client`, `AsyncPosthog` and module setting, sets options on every event, like `super_properties`.
- `set_context_option(key, value)` sets an option for the current context, like `tag()`. `get_context_options()` returns the options of the current context. Both exist on `Client` and as module functions. `AsyncPosthog` has no context methods, like it has no `tag()`, so call the module functions. They apply to every client in the context.
- Context tags and options reach `capture`, `capture_ai` and `capture_exception`. Context options also reach `set` and `set_once`. Neither reaches `alias` or `group_identify`. `super_properties` and `super_options` reach every method.
- Options are sent as given. PostHog validates them.
- An `options` value that is not a dict is logged as an error and ignored. The event is still sent.
- The legacy properties still work. They fill the matching option only when it is unset, and the SDK always removes them from `properties`. Their values are no longer converted, so pass `True` or `False`, not `"true"`.

  | Legacy property | Option |
  | --- | --- |
  | `$cookieless_mode` | `cookieless_mode` |
  | `$ignore_sent_at` | `disable_skew_correction` |
  | `$product_tour_id` | `product_tour_id` |
  | `$process_person_profile` | `process_person_profile` |

- An option set at any layer wins over its legacy property set at any layer. For example, `super_options={"cookieless_mode": True}` wins over an event's `$cookieless_mode: False`. When you move a default to options, move the per-event overrides of that key to options too.
- A `None` option counts as unset, so a later step can fill it.

Values apply in this order. Steps 2 to 4 fill only the options and properties that the steps before them left unset:

1. the `options` and `properties` of the call, then the call's `disable_geoip` argument, which fills `$geoip_disable`
2. context options and tags
3. `super_options` and `super_properties`
4. values the SDK sets: `$is_server` from `is_server`, `$geoip_disable` from the client's `disable_geoip` setting, system properties such as `$os` and `$python_version`, `options.process_person_profile = false` for events without a distinct ID, and `$release_id` from `POSTHOG_RELEASE_ID`
5. `before_send`, which sees the result of steps 1 to 4 and can change or remove any of it
6. legacy properties, which fill unset options and are then removed

A property set to `None` counts as set, so no later step fills it.
`$set`, `$set_once`, `$groups` and `$group_set` fill one level deep.
When the call and a later step both set one of them to a dict, the later step adds only the keys the call left out.
For example, `super_properties={"$set": {"plan": "free", "source": "web"}}` and a call with `properties={"$set": {"plan": "pro"}}` send `{"plan": "pro", "source": "web"}`.

These changes follow from this order:

- Properties passed to a call now override `super_properties`. `super_properties` can no longer change `$lib` or `$lib_version`.
- A `$is_server`, `$geoip_disable` or system property such as `$os` that you set in a call or a context tag now wins over the SDK's value. In 7.x the SDK overwrote it. `super_properties` win over the SDK's value too, so `super_properties={"$geoip_disable": False}` turns GeoIP lookup on for events, even with `disable_geoip=True`.
- A `disable_geoip` argument to a call belongs to that event, so it wins over context tags and `super_properties`. A `$geoip_disable` in the call's own `properties` still wins over it. `disable_geoip=False` now sends `$geoip_disable: false`. In 7.x it sent no `$geoip_disable` property.
- The `groups` argument merges into a `$groups` property of the call, and wins key by key. In 7.x it replaced the property. MCP events merge their identity's groups into a custom `$groups` property the same way.
- A `$set`, `$set_once`, `$groups` or `$group_set` in `super_properties` no longer replaces the whole value of the call. The two merge, and the call wins key by key.
- An event without a distinct ID gets `options.process_person_profile = false`. A `$process_person_profile: true` property no longer turns person processing back on. Set the option instead.
- To change an option in `before_send`, edit `options`. A legacy property that `before_send` adds does not replace an option that is already set.
- When the server enables minimal `$feature_flag_called` events, those events now keep `$window_id`, `$cookieless_mode`, `$ignore_sent_at` and `$product_tour_id`, so their options still apply.

## AI capture

- `capture_ai` and every built-in AI integration send to `/i/v1/ai/events`. The AI endpoint accepts events up to 8 MiB.
- `enable_full_ai_capture` now controls only content: string truncation and media redaction. It no longer chooses the endpoint. `_use_ai_lane` and `_enable_multimodal_capture` still work as aliases.
- The AI integrations call your client's `capture_ai`, and never `capture`. A custom client object must implement `capture_ai` and accept its `options` keyword argument. If the client has no `capture_ai`, the wrapper, callback handler or tracing processor raises `TypeError` when you create it. Before, such a client got `capture` calls.
- Tests that pass a `Mock` client to an AI integration must assert on `mock.capture_ai`, not `mock.capture`.
- When an AI integration or MCP falls back to a trace, run or session ID, it turns person processing off with a per-event option. This is the only option the integrations pass. It wins over `super_options`, context options and a `$process_person_profile: true` property. `before_send` can still change it.
- The AI integrations have no `options` argument. To set options on their events, use `set_context_option`, `super_options`, or a legacy property such as `$cookieless_mode` in `posthog_properties`.
- MCP's `PostHogCaptureEvent` has an `options` key.
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

## Batching and size limits

- An analytics event over 900 KiB is dropped and logged. This now applies in `sync_mode` too.
- An AI event over `capture_ai_max_event_bytes` is dropped and logged.
- A batch stops before an event that would take it past 5 MiB. A larger event is sent alone.
- `flush()` and `shutdown()` drain the analytics and AI queues at the same time.

## OpenFeature provider

`openfeature-provider-posthog` 0.2.0 works with posthog 7.x and 8.x.
Older provider versions require posthog below 8.0, so upgrade the provider before or with posthog.
