# Migrating from posthog 7.x to 8.0

posthog 8.0 sends every event through capture v1 and removes the legacy capture path.
Most apps upgrade without code changes.
Read the checklist first, then the sections that apply to you.

## Checklist

You need to change code if your app does any of these:

- passes `Client` or `AsyncPosthog` constructor arguments by position after `host`
- sets `capture_mode`, `POSTHOG_CAPTURE_MODE` or `gzip`
- imports `CaptureV1Error`, `posthog.capture_v1`, `request.batch_post`, `EVENTS_ENDPOINT` or `AI_EVENTS_ENDPOINT`
- sends events to a self-hosted PostHog that does not serve the capture v1 endpoints
- reuses one event `uuid` for more than one event
- sets `$process_person_profile` to turn person processing on for events without a distinct ID
- passes strings such as `"true"` for `$cookieless_mode`, `$ignore_sent_at` or `$process_person_profile`
- expects `super_properties` to override properties passed to a single call
- tests the AI integrations with a mock client and asserts on `capture`
- passes its own client object to the AI integrations

## Endpoints

| Method | Endpoint |
| --- | --- |
| `capture`, `set`, `set_once`, `alias`, `group_identify`, `capture_exception` | `/i/v1/analytics/events` |
| `capture_ai`, `capture_ai_immediate`, and every built-in AI integration | `/i/v1/ai/events` |

If you send events to a self-hosted PostHog, check that it serves both endpoints before you upgrade.
An endpoint that is not served drops every event sent to it.

## Removed options and APIs

| Removed | Use instead |
| --- | --- |
| `capture_mode`, `CaptureMode`, `POSTHOG_CAPTURE_MODE` | Nothing. Capture v1 is the only path. |
| `gzip=True` | `capture_compression=CaptureCompression.GZIP`. The default is no compression. `POSTHOG_CAPTURE_COMPRESSION` still works. |
| `CaptureV1Error` (`from posthog.capture_v1 import CaptureV1Error`) | `CaptureError` (`from posthog import CaptureError`). There is no alias. |
| `posthog.capture_v1` | `posthog.capture_event` and `posthog.capture_send` |
| `request.batch_post`, `async_batch_post`, `EVENTS_ENDPOINT`, `AI_EVENTS_ENDPOINT` | Nothing. Send events through a client. |
| The `gzip` parameter of `request.post` and `request.flags` | Nothing. |
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
An event can be dropped, for example by billing limits or quotas, inside a 2xx response.
8.0 reports those events as failures.

- Every capture failure is a `CaptureError`. It has these fields:
  - `status`: the HTTP status, or `0` when the request never got a response
  - `endpoint`, `request_id` and `attempts`
  - `drops` and `retry_exhausted`: the uuids that were dropped or ran out of retries
  - `event_results`: a `CaptureEventResult(result, details)` for each uuid, from `posthog.capture_send`
  - `verdict_summary()`: counts such as `drop/billing=1, retry/not_persisted=1`
- `on_error(error, batch)` receives every failure, including in `sync_mode` and from `AsyncPosthog.capture_immediate`.
- Without `on_error`, the SDK logs one line per failed batch, for example `2 event(s) not persisted by /i/v1/analytics/events: ...`. The line never contains event content or the server's response text.
- In `sync_mode`, a failed `capture()` calls `on_error` and returns `None`.
- If a capture inside `on_error` fails, the SDK logs the failure and does not call `on_error` again.
- A `429` response is not retried.
- Retries start at 100 ms and double each attempt, up to 30 seconds. The `Consumer` default is 3 retries, down from 10.

## Event uuids

- Each event needs its own uuid. Capture rejects a whole batch that contains the same uuid twice, so the other events in that batch are lost too.
- The SDK accepts a uuid with hyphens, 32 hex digits, `{...}` braces or a `urn:uuid:` prefix, in any case. It sends the lowercase hyphenated form and returns that form from `capture`.
- Any other value is replaced with a generated uuid, and the SDK logs an error. This applies to `AsyncPosthog` too.
- Generated uuids are UUIDv7.

## Event options

Capture v1 sends processing options in an `options` object, next to `properties`.

- `capture`, `capture_ai`, `set`, `set_once`, `alias`, `group_identify` and `capture_exception` take an `options` argument:

  ```python
  posthog.capture("signed_up", distinct_id="user-1", options={"cookieless_mode": True})
  ```

- `super_options` sets options on every event, like `super_properties`.
- `set_context_option(key, value)` sets an option for the current context, like `tag()`.
- Options are sent as given. PostHog validates them.
- The legacy properties `$cookieless_mode`, `$ignore_sent_at`, `$product_tour_id` and `$process_person_profile` still work. They move into options after `before_send`. Their values are no longer converted, so pass `True` or `False`, not `"true"`.
- An option set at any layer wins over its legacy property set at any layer. For example, `super_options={"cookieless_mode": True}` wins over an event's `$cookieless_mode: False`. When you move a default to options, move the per-event overrides of that key to options too.
- A `None` option counts as unset.

Values apply in this order, and each layer overrides the ones before it:

1. options the SDK sets, such as turning off person processing for events without a distinct ID
2. `super_options` and `super_properties`
3. context options and tags
4. the `options` and `properties` of the call
5. `before_send`

Two changes follow from this order:

- Properties passed to a call now override `super_properties`. `super_properties` can no longer change `$lib`, `$lib_version` or `$geoip_disable`.
- An event without a distinct ID gets `options.process_person_profile = false`. A `$process_person_profile: true` property no longer turns person processing back on. Set the option instead.

## AI capture

- `capture_ai` and every built-in AI integration send to `/i/v1/ai/events`. The AI endpoint accepts events up to 8 MiB.
- `enable_full_ai_capture` now controls only content: string truncation and media redaction. It no longer chooses the endpoint. `_use_ai_lane` and `_enable_multimodal_capture` still work as aliases.
- The AI integrations call your client's `capture_ai`, and pass `options=`. A client object without `capture_ai` gets `capture` calls instead. A custom client must accept the `options` keyword argument.
- Tests that pass a `Mock` client to an AI integration must assert on `mock.capture_ai`, not `mock.capture`.
- When an AI integration or MCP falls back to a trace, run or session ID, it turns person processing off with a per-event option. A `$process_person_profile: true` property no longer turns it back on. `before_send` can still change it.
- MCP's `PostHogCaptureEvent` has an `options` key.
- New `Client` and `AsyncPosthog` arguments for the AI lane:
  - `capture_ai_compression`: default none. `POSTHOG_CAPTURE_COMPRESSION` does not apply to it.
  - `capture_ai_max_queue_size`: default 1000
  - `capture_ai_timeout`: default 30 seconds
  - `capture_ai_max_event_bytes`: default 8 MiB plus 64 KiB. You can only lower it.
- `AsyncPosthog` has `capture_ai` and `capture_ai_immediate`.
- `AsyncPosthog` accepts `privacy_mode` and `enable_full_ai_capture`, with the same meaning as on `Client`. An AI integration given an `AsyncPosthog` client used to always truncate and redact media.

## Batching and size limits

- An analytics event over 900 KiB is dropped and logged. This now applies in `sync_mode` too.
- An AI event over `capture_ai_max_event_bytes` is dropped and logged.
- A batch stops before an event that would take it past 5 MiB. A larger event is sent alone.
- `flush()` and `shutdown()` drain the analytics and AI queues at the same time.

## OpenFeature provider

`openfeature-provider-posthog` 0.2.0 works with posthog 7.x and 8.x.
Older provider versions require posthog below 8.0, so upgrade the provider before or with posthog.
