---
title: "PostHog Python SDK 8.x (experimental)"
sidebar: Docs
showTitle: true
contentMaxWidthClass: max-w-full
platformLogo: python
tableOfContents:
  - value: "Getting started"
    url: getting-started
    depth: 0
  - value: "Setup"
    url: setup
    depth: 0
  - value: "Capture"
    url: capture
    depth: 0
  - value: "Identify"
    url: identify
    depth: 0
  - value: "Traces"
    url: traces
    depth: 0
  - value: "Alias"
    url: alias
    depth: 0
  - value: "Capture AI"
    url: capture-ai
    depth: 0
  - value: "Capture exception"
    url: capture-exception
    depth: 0
  - value: "Emails"
    url: emails
    depth: 0
  - value: "Flush"
    url: flush
    depth: 0
  - value: "Get feature flag"
    url: get-feature-flag
    depth: 0
  - value: "Get feature flag payload"
    url: get-feature-flag-payload
    depth: 0
  - value: "Get feature flag result"
    url: get-feature-flag-result
    depth: 0
  - value: "Get feature flags"
    url: get-feature-flags
    depth: 0
  - value: "Get feature flags and payloads"
    url: get-feature-flags-and-payloads
    depth: 0
  - value: "Evaluate flags"
    url: evaluate-flags
    depth: 0
  - value: "Group identify"
    url: group-identify
    depth: 0
  - value: "Is feature enabled"
    url: is-feature-enabled
    depth: 0
  - value: "Shutdown"
    url: shutdown
    depth: 0
  - value: "Flag definition loader"
    url: flag-definition-loader
    depth: 0
  - value: "SDK utils"
    url: sdk-utils
    depth: 0
  - value: "AsyncClient"
    url: asyncclient
    depth: 0
  - value: "Module functions"
    url: module-functions
    depth: 0
  - value: "Types"
    url: types
    depth: 0
---

## Getting started


The Python SDK captures events, evaluates feature flags and reports errors from Python 3.10 or later.

To send your first event, install the package, create a client, capture an event, and shut the client down, as the steps below show.

### Install

```bash
pip install posthog
```

### Initialize

Create one client for your app with your project token and host. Your project token and host are in your [project settings](https://app.posthog.com/project/settings). Keep the token in an environment variable rather than in code.

```python
from posthog import Posthog

posthog = Posthog("<ph_project_token>", host="<ph_client_api_host>")
```

### Capture an event

```python
posthog.capture("user_signed_up", distinct_id="user_123", properties={"plan": "free"})
```

`capture` queues the event and returns right away. A background thread sends it.

### Shut down

Call `shutdown()` before your process exits, so queued events are sent.

```python
posthog.shutdown()
```

In an asyncio app, use `AsyncPosthog` as an async context manager instead. See [AsyncClient](#asyncclient).

## Setup

### `Client(...)`

```python
Client(project_api_key: str, host=None, *, debug=False, max_queue_size=10000, send=True, on_error=None, flush_at=100, flush_interval=5.0, max_retries=3, sync_mode=False, timeout: float = 15, thread=1, poll_interval=30, personal_api_key=None, disabled=False, disable_geoip=True, is_server=True, historical_migration=False, feature_flags_request_timeout_seconds: float = 3, feature_flags_request_max_retries=1, super_properties=None, super_options=None, enable_exception_autocapture=False, log_captured_exceptions=False, project_root=None, privacy_mode=False, before_send=None, flag_fallback_cache_url=None, enable_local_evaluation=True, flag_definition_cache_provider: Optional[FlagDefinitionCacheProvider] = None, capture_exception_code_variables=False, code_variables_mask_patterns=None, code_variables_ignore_patterns=None, code_variables_mask_url_credentials=None, code_variables_detect_secrets=None, in_app_modules: list[str] | None = None, enable_exception_autocapture_rate_limiting=False, exception_autocapture_bucket_size=ExceptionCapture.DEFAULT_BUCKET_SIZE, exception_autocapture_refill_rate=ExceptionCapture.DEFAULT_REFILL_RATE, exception_autocapture_refill_interval_seconds=ExceptionCapture.DEFAULT_REFILL_INTERVAL_SECONDS, capture_compression: Optional[Union[CaptureCompression, str]] = None, capture_ai_compression: Optional[Union[CaptureCompression, str]] = None, capture_ai_max_queue_size: int = 1000, capture_ai_timeout: float = 30, capture_ai_max_event_bytes: int = AI_MAX_MSG_SIZE, secret_key=None, metrics: Optional[dict] = None, enable_full_ai_capture=False, capture_trace_context=False, traces: Optional[dict] = None, app_host: Optional[str] = None, email_request_timeout_seconds: float = 15, email_request_max_retries: int = 2, migrated_from: Optional[str] = None)
```

Initialize a new PostHog client instance.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`project_api_key` | `str` | PostHog project API key/token. | *required*
`host` |  | PostHog host. Defaults to the US ingestion endpoint when not set. App hosts such as `https://us.posthog.com` are mapped to the corresponding ingestion host. | `None`
`debug` |  | Enable verbose SDK logging and re-raise errors from public API methods. | `False`
`max_queue_size` |  | Maximum number of analytics events buffered before upload. AI events use `capture_ai_max_queue_size`. | `10000`
`send` |  | If False, queueing succeeds but events are not sent. | `True`
`on_error` |  | Optional callback `(error, batch)` invoked when an upload fails: by background consumers, or on the calling thread in `sync_mode`. Capture failures arrive as `CaptureError`. Without it, each failed batch logs one aggregate line. A capture that fails inside the callback logs that line instead of calling it again. Keep it short and non-blocking. Calling lifecycle methods directly is safe and deferred, but do not start another thread or task that calls `flush()`, `join()`, or `shutdown()` and then wait for it from the callback. | `None`
`flush_at` |  | Number of queued events that triggers a batch upload. | `100`
`flush_interval` |  | Maximum seconds a background consumer waits before flushing a partial batch. | `5.0`
`max_retries` |  | Number of upload retries. Values below 0 are treated as 0. | `3`
`sync_mode` |  | If True, send each event synchronously instead of using background worker threads. This blocks the calling thread; in asyncio applications such as FastAPI, use `AsyncPosthog` instead. | `False`
`timeout` | `float` | HTTP request timeout in seconds for event uploads. | `15`
`thread` |  | Number of background consumer threads. | `1`
`poll_interval` |  | Seconds between local feature flag definition refreshes. | `30`
`secret_key` |  | A Personal API Key or Project Secret API Key, used to authenticate local feature flag evaluation, remote config payloads, and decrypted flag payloads. For example, `posthog.Client(project_api_key, secret_key="phx_...")`. | `None`
`personal_api_key` |  | Deprecated alias for `secret_key`. Still honored for backwards compatibility; prefer `secret_key`, which also accepts a Project Secret API Key. | `None`
`disabled` |  | If True, disable captures and API requests. Useful in tests. | `False`
`disable_geoip` |  | Whether to disable server-side GeoIP enrichment. Defaults to True. Events get `$geoip_disable` only when the event, context tags and `super_properties` leave it unset. | `True`
`is_server` |  | Whether events are emitted from a server-side runtime. Defaults to True; set to False when using the SDK as a client/CLI so the device OS is attributed to the person normally. Events get `$is_server` only when the event, context tags and `super_properties` leave it unset. | `True`
`historical_migration` |  | Mark events as historical migration imports. | `False`
`feature_flags_request_timeout_seconds` | `float` | Timeout in seconds for feature flag and remote config requests. | `3`
`feature_flags_request_max_retries` |  | Number of retries for feature flag requests after network, transport, or timeout failures. Defaults to 1. Set to 0 to disable retries. | `1`
`app_host` | `Optional[str]` | Your PostHog app host, such as `https://posthog.example.com`. `emails` sends there. Needed only when `host` is not PostHog Cloud, for example a reverse proxy, which must not receive the secret key. | `None`
`email_request_timeout_seconds` | `float` | Timeout in seconds for each email send request. Defaults to 15. | `15`
`email_request_max_retries` | `int` | Number of retries for an email send after a network error, a rate limit, or an unavailable service. Defaults to 2. Set to 0 when a job runner already retries the send. | `2`
`migrated_from` | `Optional[str]` | The email provider you moved from, such as `"resend"`. `emails` sends it with every request. | `None`
`super_properties` |  | Properties for every captured event. They fill only keys the event leaves unset, before `before_send` runs. `$set`, `$set_once`, `$groups` and `$group_set` fill one level deep. An event's own properties and context tags override them, and `before_send` can change or remove them. | `None`
`super_options` |  | Capture options for every captured event, such as `{"cookieless_mode": True}`. They fill only options the event leaves unset, before `before_send` runs. An event's own `options` and context options override them, and `before_send` can change them. | `None`
`enable_exception_autocapture` |  | Automatically capture uncaught exceptions. | `False`
`log_captured_exceptions` |  | Also log exceptions captured by error tracking. | `False`
`project_root` |  | Root path used to determine in-app stack frames for captured exceptions. Defaults to the current working directory. | `None`
`privacy_mode` |  | For AI observability, capture usage metadata without prompt inputs or outputs. | `False`
`enable_full_ai_capture` |  | Capture full AI content in PostHog AI wrapper events: skips string truncation and passes media (base64/data URIs) through unredacted. `privacy_mode` always wins. Wrapper events use the AI capture endpoint whether or not this is set. Defaults to False. | `False`
`before_send` |  | Optional callback that can modify or drop events before upload. Return `None` to drop an event. Context tags, context options, `super_properties`, `super_options` and the values the SDK adds, such as `$is_server`, `$geoip_disable` and `$os`, fill in before it runs, so it can change or remove them. | `None`
`flag_fallback_cache_url` |  | Optional feature flag fallback cache URL, such as `memory://local/?ttl=300&size=10000` or a Redis URL. | `None`
`enable_local_evaluation` |  | Whether to poll feature flag definitions for local evaluation when a personal API key is configured. | `True`
`flag_definition_cache_provider` | `Optional[FlagDefinitionCacheProvider]` | Optional external cache provider for sharing feature flag definitions across workers. | `None`
`capture_exception_code_variables` |  | Capture local variable values on exception stack frames. | `False`
`capture_trace_context` |  | When OpenTelemetry is installed and a valid span is active at capture time, add its trace and span IDs as `$trace_id` and `$span_id` properties to events captured with `capture()` and `capture_ai()`, so they can be correlated with backend traces. They fill after context tags and `super_properties`, before `before_send`. A `$trace_id` or `$span_id` from the event, a context tag or `super_properties` stops the SDK adding either ID. Exception events (`capture_exception`) always attach these IDs regardless of this setting. Defaults to False. | `False`
`traces` | `Optional[dict]` | Config dict for distributed tracing: `service_name`, `service_version`, `environment`, `resource_attributes`, `flush_interval` (5 s), `max_queue_size` (2048), `max_export_batch_size` (512), `max_live_spans` (10000), `max_span_age` (3600 s), `max_attributes_per_span` (128), `max_events_per_span` (128), `max_attribute_value_length` (8192). `before_span_send` is a callable, or a list run in order, that receives each finished span as a dict (`trace_id`, `span_id` and `parent_span_id` are read-only) and returns it, edited, or `None` to drop it; a hook that raises drops the span. Tracing is off until this is provided. Spans export on a background timer even with `sync_mode`; serverless handlers should call `flush()` before returning. Defaults to None. | `None`
`code_variables_mask_patterns` |  | Variable-name patterns to mask when capturing code variables. | `None`
`code_variables_ignore_patterns` |  | Variable-name patterns to omit when capturing code variables. | `None`
`code_variables_mask_url_credentials` |  | Scrub credentials embedded in URLs/DSNs (e.g. `user:pass@host`) from captured code variables, regardless of the surrounding variable name. Defaults to True. | `None`
`code_variables_detect_secrets` |  | Last-resort entropy-based detection that redacts high-entropy secret-looking values (API keys, tokens, strong passwords) sitting in innocuously-named variables, after the name and URL checks. Skips structured ids (UUIDs, ObjectIds, hashes). Defaults to True. | `None`
`in_app_modules` | `list[str] \| None` | Module/package prefixes treated as in-app frames in captured exceptions. | `None`
`enable_exception_autocapture_rate_limiting` |  | Rate limit autocaptured exceptions client-side with a token bucket per exception type. Disabled by default. | `False`
`exception_autocapture_bucket_size` |  | Maximum burst of autocaptured exceptions allowed per exception type (token bucket size, clamped to 0-100). | `DEFAULT_BUCKET_SIZE`
`exception_autocapture_refill_rate` |  | Tokens restored per refill interval for each exception type's bucket. | `DEFAULT_REFILL_RATE`
`exception_autocapture_refill_interval_seconds` |  | Seconds between token refills for autocaptured exception rate limiting. | `DEFAULT_REFILL_INTERVAL_SECONDS`
`capture_compression` | `Optional[Union[CaptureCompression, str]]` | Request-body compression for `capture()` uploads. `CaptureCompression.GZIP` or `DEFLATE` (or the strings `"gzip"`/`"deflate"`). When omitted, the `POSTHOG_CAPTURE_COMPRESSION` env var is consulted, then no compression. | `None`
`capture_ai_compression` | `Optional[Union[CaptureCompression, str]]` | Request-body compression for `capture_ai()` uploads, set independently of `capture_compression`. Defaults to no compression, and the env var does not apply. `CaptureCompression.ZSTD` suits large AI payloads. | `None`
`capture_ai_max_queue_size` | `int` | Maximum number of AI events buffered before upload. Defaults to 1000, lower than `max_queue_size` because AI events are much larger. | `1000`
`capture_ai_timeout` | `float` | Seconds allowed for one AI upload request. Defaults to 30, longer than `timeout` because AI batches are much larger. | `30`
`capture_ai_max_event_bytes` | `int` | Largest serialized AI event the SDK sends; a larger one is dropped with an error log. Defaults to the AI endpoint's ceiling plus envelope headroom, and may only be lowered. | `AI_MAX_MSG_SIZE`

**Examples:**

```python
from posthog import Posthog

posthog = Posthog('<ph_project_api_key>', host='<ph_app_host>')
```

## Capture

### `Client.capture`

```python
capture(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Captures an event manually. [Learn about capture best practices](https://posthog.com/docs/product-analytics/capture-events)

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`event` | `str` | The event name to capture. | *required*
`distinct_id` |  | The distinct ID of the user. | 
`properties` |  | A dictionary of properties to include with the event. | 
`timestamp` |  | The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. | 
`uuid` |  | A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. | 
`groups` |  | A dictionary of group information. It merges into a `$groups` property and wins key by key. | 
`flags` |  | A FeatureFlagEvaluations snapshot from evaluate_flags(). The exact values from the snapshot are attached with no extra /flags request. | 
`send_feature_flags` |  | Deprecated. Prefer flags=... from evaluate_flags(). When truthy, evaluates flags during capture and attaches them to the event. | 
`disable_geoip` |  | Whether to disable GeoIP for this event. It wins over a `$geoip_disable` in context tags or `super_properties`. A `$geoip_disable` property in this call's `properties` wins over it. | 
`options` |  | Capture options for this event, such as `{"process_person_profile": False}`. Sent as given, for PostHog to validate. They override context options and `super_options`, which fill in before `before_send` runs. An option overrides its legacy `$` property, such as `$process_person_profile`. A `None` option counts as unset. | 

**Examples:**

```python
# Anonymous event
posthog.capture('some-anon-event')
```
```python
# Context usage
from posthog import identify_context, new_context
with new_context():
    identify_context('distinct_id_of_the_user')
    posthog.capture('user_signed_up')
    posthog.capture('user_logged_in')
    posthog.capture('some-custom-action', distinct_id='distinct_id_of_the_user')
```
```python
# Set event properties
posthog.capture(
    "user_signed_up",
    distinct_id="distinct_id_of_the_user",
    properties={
        "login_type": "email",
        "is_free_trial": "true"
    }
)
```
```python
# Page view event
posthog.capture('$pageview', distinct_id="distinct_id_of_the_user", properties={'$current_url': 'https://example.com'})
```

## Identify

### `Client.set`

```python
set(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

Set properties on a person profile.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` |  | The distinct ID of the user. | 
`properties` |  | A dictionary of properties to set. | 
`timestamp` |  | The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. | 
`uuid` |  | A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. | 
`disable_geoip` |  | Whether to disable GeoIP for this event. | 

**Examples:**

```python
# Set with distinct id
posthog.set(distinct_id='user123', properties={'name': 'Max Hedgehog'})
```

Note: This method will not raise exceptions. Errors are logged.

### `Client.set_once`

```python
set_once(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

Set properties on a person profile only if they haven't been set before.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` |  | The distinct ID of the user. | 
`properties` |  | A dictionary of properties to set once. | 
`timestamp` |  | The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. | 
`uuid` |  | A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. | 
`disable_geoip` |  | Whether to disable GeoIP for this event. | 

**Examples:**

```python
posthog.set_once(distinct_id='user123', properties={'initial_signup_date': '2024-01-01'})
```

Note: This method will not raise exceptions. Errors are logged.

## Traces

### `Client.start_span`

```python
start_span(name: str, *, kind: Optional[str] = None, attributes: Optional[Mapping[str, Any]] = None, parent: Union[Span, str, None] = None, tracestate: Optional[str] = None, start_time: Union[datetime, float, None] = None) -> Span
```

Start a span for distributed tracing. Alpha.

Returns a span handle. Use it as a context manager to make it the active
span for the block and end it on exit (recording a raised exception on
the way out); or call `end()` yourself for a span that cannot wrap a
block. Spans started inside the block nest under it automatically.
Always returns a usable handle, even when tracing is off, so calling
code never branches.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`name` | `str` | A low-cardinality operation name, e.g. `GET /users/:id`. Variable values belong in attributes, not the name. | *required*
`kind` | `Optional[str]` | `internal` (default), `server`, `client`, `producer` or `consumer`. | `None`
`attributes` | `Optional[Mapping[str, Any]]` | Initial attributes. | `None`
`parent` | `Union[Span, str, None]` | A span handle, or an inbound W3C `traceparent` header value to continue a remote trace. Defaults to the active span. A forked child starts with no active span; pass the parent span to continue a trace across a fork. | `None`
`tracestate` | `Optional[str]` | The inbound `tracestate` header accompanying a `traceparent` string `parent`; preserved and propagated. | `None`
`start_time` | `Union[datetime, float, None]` | A `datetime` or epoch seconds, to backdate the span. | `None`

**Examples:**

```python
posthog = Posthog("<ph_project_api_key>", traces={"service_name": "checkout-api"})

with posthog.start_span("POST /checkout", parent=request.headers.get("traceparent")) as span:
    span.set_attribute("plan", user.plan)
    with posthog.start_span("db.query", kind="client"):
        ...
    outgoing_headers = {"traceparent": span.traceparent()}
```

### `Client.get_active_span`

```python
get_active_span() -> Optional[Span]
```

The span that is active in the current context, or `None`. Alpha.

Only entering a span (`with posthog.start_span(...) as span:`) makes
it active; a span started manually is not. Use it to propagate the
trace to the next service: `span.traceparent()` is the header value.

**Examples:**

```python
span = posthog.get_active_span()
if span is not None:
    headers["traceparent"] = span.traceparent()
```

## Alias

### `Client.alias`

```python
alias(previous_id: ID_TYPES, distinct_id: Optional[str], timestamp: Optional[Union[datetime, str]] = None, uuid: Optional[str] = None, disable_geoip: Optional[bool] = None, options: Optional[Dict[str, Any]] = None) -> Optional[str]
```

Create an alias between two distinct IDs.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`previous_id` | `ID_TYPES` | The previous distinct ID. Required - the call is dropped with a warning if it is missing or empty. | *required*
`distinct_id` | `Optional[str]` | The new distinct ID to alias to. Required - the call is dropped with a warning if it is missing or empty. The context distinct ID is never used here. | *required*
`timestamp` | `Optional[Union[datetime, str]]` | The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. | `None`
`uuid` | `Optional[str]` | A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. | `None`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this event. | `None`
`options` | `Optional[Dict[str, Any]]` | Capture options for this event, sent as given. | `None`

**Examples:**

```python
posthog.alias(previous_id='distinct_id', distinct_id='alias_id')
```

Note: This method will not raise exceptions. Errors are logged.

## Capture AI

### `Client.capture_ai`

```python
capture_ai(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Capture an AI event on the dedicated AI capture endpoint.

Beta: the signature is stable; operational limits (per-event size
cap, batching, endpoint) may change without notice.

Takes the same arguments and returns the same value as `capture()`:
the event UUID, or None when the event was not admitted (disabled
client, or dropped by `before_send`). The event is queued on an
isolated AI lane with its own consumer pool and a higher per-event
size cap, posting to the dedicated AI ingestion endpoint. The payload
is sent as given — no redaction or truncation is applied here.

## Capture exception

### `Client.capture_exception`

```python
capture_exception(exception: Optional[ExceptionArg], *, level: Optional[str] = None, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Capture an exception for error tracking.

When OpenTelemetry is installed and a valid span is active, its trace and
span IDs are added as `$trace_id` and `$span_id` event properties,
unless the event, a context tag or `super_properties` sets either one.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`exception` | `Optional[ExceptionArg]` | The exception to capture. | *required*
`level` | `Optional[str]` | The `$exception_level`, such as `"warning"` or `"fatal"`. Defaults to `"error"`. An unknown value counts as unset. | `None`
`distinct_id` |  | The distinct ID of the user. | 
`properties` |  | A dictionary of additional properties. Reserved exception properties, such as `$exception_list` and `$exception_level`, are ignored. | 
`flags` |  | A `FeatureFlagEvaluations` snapshot from `evaluate_flags()`. Attaches those exact flag values to the captured `$exception` event. | 
`send_feature_flags` |  | Deprecated. Pass `flags` from `evaluate_flags()` instead. | 
`disable_geoip` |  | Whether to disable GeoIP for this event. | 

**Examples:**

```python
try:
    # Some code that might fail
    pass
except Exception as e:
    posthog.capture_exception(e, distinct_id='user_distinct_id', properties=additional_properties)
```

## Emails

### `Client.emails`

```python
emails: Emails
```

Send transactional email from PostHog email templates or from content
in your code, and read the status of each email sent with `get`.

Needs `secret_key`, a project secret API key with the `transactional_email:write`
scope. Sends go to `app_host`, or to the PostHog Cloud app host of
`host`. `email_request_timeout_seconds` and `email_request_max_retries`
configure each send. A disabled or shut-down client sends nothing. See
`posthog.emails` for setup.

**Examples:**

```python
client.emails.send(
    to="user@example.com",
    template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b",
    template_data={"name": "Ada"},
    distinct_id="user_123",
)
```

## Flush

### `Client.flush`

```python
flush(timeout_seconds: Optional[float] = 10) -> None
```

Attempt immediate delivery of queued events, ended spans and autocaptured metrics, keeping the client reusable.

For a client shared across requests or serverless invocations (including
the module-level client), call `flush()` before a short-lived invocation
returns. Use `shutdown()` instead only for final process cleanup or when
disposing of a client that will not be reused. Flush does not stop the
background workers or feature flag poller, and does not end open spans.

Returning does not guarantee server receipt: pending work can remain after
a timeout or span export failure, and failed events may be dropped by the
consumers. Calls made directly from SDK callbacks such as `on_error` are
deferred rather than blocking the worker that invoked the callback.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`timeout_seconds` | `Optional[float]` | Seconds to wait for event queues to drain, shared across event lanes. Defaults to 10 seconds. Pass `None` to wait without a time limit; this does not guarantee successful delivery. Queued spans flush concurrently. The span exporter waits up to this many seconds for an in-flight flush, then uses a fresh drain budget once it acquires the lock. If the lock wait times out, the in-flight flush is left to finish. At least one span request is attempted after acquiring the lock, even with no budget left; retriable failures are retried after backoff while budget remains, with one last attempt at the deadline. Each span request is bounded by the client's `timeout` in seconds, and the last request is not cut short. This is therefore not a strict wall-clock limit for the whole call. With `None`, a retriable span failure is left for a later flush rather than retried within this call. | `10`

**Examples:**

```python
posthog.capture('event_name', distinct_id='user_id')
# Before a serverless invocation returns; keep the shared client open.
posthog.flush(timeout_seconds=10)
```

## Get feature flag

### `Client.get_feature_flag`

```python
get_feature_flag(key: str, distinct_id: ID_TYPES, *, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, send_feature_flag_events: bool = True, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None) -> Optional[FlagValue]
```

Get multivariate feature flag value for a user.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key. | *required*
`distinct_id` | `ID_TYPES` | The distinct ID of the user. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`only_evaluate_locally` | `bool` | Whether to only evaluate locally. | `False`
`send_feature_flag_events` | `bool` | Whether to send feature flag events. | `True`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

**Examples:**

```python
enabled_variant = posthog.get_feature_flag('flag-key', 'distinct_id_of_your_user')
if enabled_variant == 'variant-key': # replace 'variant-key' with the key of your variant
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

## Get feature flag payload

### `Client.get_feature_flag_payload`

```python
get_feature_flag_payload(key: str, distinct_id: ID_TYPES, *, match_value: Optional[FlagValue] = None, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, send_feature_flag_events: bool = False, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None) -> Optional[object]
```

Get the payload for a feature flag.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key. | *required*
`distinct_id` | `ID_TYPES` | The distinct ID of the user. | *required*
`match_value` | `Optional[FlagValue]` | The specific flag value to get payload for. | `None`
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`only_evaluate_locally` | `bool` | Whether to only evaluate locally. | `False`
`send_feature_flag_events` | `bool` | Deprecated. Use get_feature_flag() instead if you need events. | `False`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

**Returns:**

Type | Description
---- | -----------
`Optional[object]` | The payload associated with the matched feature flag value, or None. This method returns the payload only, not the FeatureFlagResult wrapper used internally to compute it.

**Examples:**

```python
is_my_flag_enabled = posthog.feature_enabled('flag-key', 'distinct_id_of_your_user')

if is_my_flag_enabled:
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

## Get feature flag result

### `Client.get_feature_flag_result`

```python
get_feature_flag_result(key: str, distinct_id: ID_TYPES, *, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, send_feature_flag_events: bool = True, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None) -> Optional[FeatureFlagResult]
```

Get a FeatureFlagResult object which contains the flag result and payload for a key by evaluating locally or remotely
depending on whether local evaluation is enabled and the flag can be locally evaluated.
This also captures the `$feature_flag_called` event unless `send_feature_flag_events` is `False`.
When the flag was evaluated remotely and its config version 2 record is a complete experiment
split (an experiment rule with a rule ID and a variant key), it also captures the
`$experiment_exposure` event, once per distinct ID, groups, flag, rule and variant.

**Examples:**

```python
flag_result = posthog.get_feature_flag_result('flag-key', 'distinct_id_of_your_user')
if flag_result and flag_result.get_value() == 'variant-key':
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = flag_result.payload
```

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key. | *required*
`distinct_id` | `ID_TYPES` | The distinct ID of the user. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`only_evaluate_locally` | `bool` | Whether to only evaluate locally. | `False`
`send_feature_flag_events` | `bool` | Whether to send feature flag events. | `True`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

**Returns:**

Type | Description
---- | -----------
`Optional[FeatureFlagResult]` | Optional[FeatureFlagResult]: The feature flag result or None if disabled/not found.

## Get feature flags

### `Client.get_feature_variants`

```python
get_feature_variants(distinct_id: ID_TYPES, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, disable_geoip: Optional[bool] = None, flag_keys_to_evaluate: Optional[list[str]] = None, device_id: Optional[str] = None) -> dict[str, Union[bool, str]]
```

Get feature flag variants for a user.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `ID_TYPES` | The distinct ID of the user. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`flag_keys_to_evaluate` | `Optional[list[str]]` | A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

### `Client.get_all_flags`

```python
get_all_flags(distinct_id: ID_TYPES, *, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, disable_geoip: Optional[bool] = None, flag_keys_to_evaluate: Optional[list[str]] = None, device_id: Optional[str] = None) -> Optional[dict[str, Union[bool, str]]]
```

Get all feature flags for a user.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `ID_TYPES` | The distinct ID of the user. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`only_evaluate_locally` | `bool` | Whether to only evaluate locally. | `False`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`flag_keys_to_evaluate` | `Optional[list[str]]` | A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

**Examples:**

```python
posthog.get_all_flags('distinct_id_of_your_user')
```

## Get feature flags and payloads

### `Client.get_feature_flags_and_payloads`

```python
get_feature_flags_and_payloads(distinct_id: ID_TYPES, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, disable_geoip: Optional[bool] = None, flag_keys_to_evaluate: Optional[list[str]] = None, device_id: Optional[str] = None) -> FlagsAndPayloads
```

Get feature flags and payloads for a user.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `ID_TYPES` | The distinct ID of the user. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`flag_keys_to_evaluate` | `Optional[list[str]]` | A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

**Examples:**

```python
result = posthog.get_feature_flags_and_payloads('<distinct_id>')
```

### `Client.get_all_flags_and_payloads`

```python
get_all_flags_and_payloads(distinct_id: ID_TYPES, *, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, disable_geoip: Optional[bool] = None, flag_keys_to_evaluate: Optional[list[str]] = None, device_id: Optional[str] = None) -> FlagsAndPayloads
```

Get all feature flags and their payloads for a user.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `ID_TYPES` | The distinct ID of the user. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`only_evaluate_locally` | `bool` | Whether to only evaluate locally. | `False`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`flag_keys_to_evaluate` | `Optional[list[str]]` | A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

**Examples:**

```python
posthog.get_all_flags_and_payloads('distinct_id_of_your_user')
```

## Evaluate flags

### `Client.evaluate_flags`

```python
evaluate_flags(distinct_id: Optional[ID_TYPES] = None, *, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, disable_geoip: Optional[bool] = None, flag_keys: Optional[List[str]] = None, device_id: Optional[str] = None) -> FeatureFlagEvaluations
```

Evaluate all feature flags for a user in a single call and return a
`FeatureFlagEvaluations` snapshot. Branch on `.is_enabled()` /
`.get_flag()` and pass the same snapshot to `capture` via the
`flags` option so events carry the exact flag values the code branched on.

Prefer this over repeated `get_feature_flag()` calls and over
`capture(send_feature_flags=True)` — it consolidates flag evaluation into
a single `/flags` request per incoming request.

Local evaluation is transparent: when the poller resolves a flag, the
snapshot's `$feature_flag_called` events are tagged `locally_evaluated=True`
and reason `"Evaluated locally"`.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `Optional[ID_TYPES]` | The user's distinct ID. If `None`, falls back to the context distinct_id. If still unresolvable, returns an empty snapshot. | `None`
`groups` | `Optional[Mapping[str, Union[str, int]]]` | Mapping of group type to group key. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | Person properties to use for evaluation. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | Group properties keyed by group type. | `None`
`only_evaluate_locally` | `bool` | If True, never fall back to remote evaluation — flags that can't be evaluated locally are omitted from the snapshot and listed with a reason in its `unresolved_flags`. | `False`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup. | `None`
`flag_keys` | `Optional[List[str]]` | Optional list that scopes local evaluation, the underlying `/flags` request, and the returned snapshot. When omitted or `None`, all flags are evaluated. An empty list returns an empty snapshot without evaluating flags. A requested key absent from loaded local definitions is included in one remote fallback per `evaluate_flags` call unless `only_evaluate_locally` is True. If the server also does not know the key, it is omitted from the snapshot. | `None`
`device_id` | `Optional[str]` | Optional device ID override. If not provided, falls back to the context device_id (which may be set via tracing headers). Used by experience-continuity flags to match users across distinct_id changes. | `None`

**Returns:**

Type | Description
---- | -----------
`FeatureFlagEvaluations` | A `FeatureFlagEvaluations` snapshot.

**Examples:**

```python
flags = posthog.evaluate_flags(
    "user_123",
    person_properties={"plan": "enterprise"},
)
if flags.is_enabled("new-dashboard"):
    render_new_dashboard()
posthog.capture("page_viewed", distinct_id="user_123", flags=flags)
```

## Group identify

### `Client.group_identify`

```python
group_identify(group_type: str, group_key: str, properties: Optional[Dict[str, Any]] = None, timestamp: Optional[Union[datetime, str]] = None, uuid: Optional[Union[str, UUID]] = None, disable_geoip: Optional[bool] = None, distinct_id: Optional[ID_TYPES] = None, options: Optional[Dict[str, Any]] = None) -> Optional[str]
```

Identify a group and set its properties.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`group_type` | `str` | The type of group (e.g., 'company', 'team'). Required - the call is dropped with a warning if it is missing or empty. | *required*
`group_key` | `str` | The unique identifier for the group. Required - the call is dropped with a warning if it is missing or empty. | *required*
`properties` | `Optional[Dict[str, Any]]` | A dictionary of properties to set on the group. | `None`
`timestamp` | `Optional[Union[datetime, str]]` | The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. | `None`
`uuid` | `Optional[Union[str, UUID]]` | A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. | `None`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this event. | `None`
`distinct_id` | `Optional[ID_TYPES]` | The distinct ID of the user performing the action. Falls back to the context distinct ID, then to `$<group_type>_<group_key>`. | `None`
`options` | `Optional[Dict[str, Any]]` | Capture options for this event, sent as given. | `None`

**Examples:**

```python
posthog.group_identify('company', 'company_id_in_your_db', {
    'name': 'Awesome Inc.',
    'employees': 11
})
```

Note: This method will not raise exceptions. Errors are logged.

## Is feature enabled

### `Client.feature_enabled`

```python
feature_enabled(key: str, distinct_id: ID_TYPES, *, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, send_feature_flag_events: bool = True, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None) -> Optional[bool]
```

Check if a feature flag is enabled for a user.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key. | *required*
`distinct_id` | `ID_TYPES` | The distinct ID of the user. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`only_evaluate_locally` | `bool` | Whether to only evaluate locally. | `False`
`send_feature_flag_events` | `bool` | Whether to send feature flag events. | `True`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

**Examples:**

```python
is_my_flag_enabled = posthog.feature_enabled('flag-key', 'distinct_id_of_your_user')
if is_my_flag_enabled:
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

## Shutdown

### `Client.join`

```python
join() -> None
```

Attempt to process queued events and end the consumer threads. Do not use directly, call `shutdown()` instead.

Failed or undrainable events may be dropped and reported through logging
or `on_error`; returning does not guarantee server receipt. Lifecycle
cleanup is attempted once, and cleanup failures are logged without retry.

**Examples:**

```python
posthog.join()
```

### `Client.shutdown`

```python
shutdown() -> None
```

Attempt final delivery and permanently shut down the client.

Call this for final process cleanup or when disposing of a client that
will not be reused. It closes capture lanes, stops background workers and
the feature flag poller, and tears down integrations. Do not call it at the
end of each request or serverless invocation when the client is shared;
call `flush()` before returning instead. A shut-down client cannot be
reused; create a new client if needed.

Normally this method blocks until queued events have been attempted and
cleanup finishes. Failed or undrainable events may be dropped and
reported through logging or `on_error`; returning does not guarantee
server receipt. Event draining has no timeout. Queued spans get one final
flush with up to 30 seconds waiting for an in-flight flush, then a fresh
30-second drain budget; the last HTTP request is not cut short. Any spans
it cannot send are discarded with a warning, as are spans still open.
Lifecycle cleanup is attempted once, and cleanup failures
are logged without retry. When called directly from an SDK callback such as
`on_error`, shutdown is deferred to avoid blocking the worker that
invoked the callback. If the callback must coordinate a blocking
shutdown, have it signal an
application-owned thread and return before that thread calls shutdown.
Do not wait inside the callback for another thread or task that calls a
lifecycle method.

**Examples:**

```python
posthog.shutdown()
```

## Flag definition loader

### `Client.load_feature_flags`

```python
load_feature_flags()
```

Load feature flags for local evaluation.

**Examples:**

```python
posthog.load_feature_flags()
```

### `Client.feature_flag_definitions`

```python
feature_flag_definitions()
```

Return feature flag definitions loaded for local evaluation.

**Returns:**

Type | Description
---- | -----------
 | The currently loaded feature flag definitions, or `None` before local evaluation has loaded definitions.

## SDK utils

### `Client.queue`

```python
queue: Queue
```

The analytics lane's queue (kept for backwards compatibility).

### `Client.consumers`

```python
consumers: Optional[List[Consumer]]
```

Flat list of the lanes' consumers, analytics first (kept for backwards compatibility).

### `Client.new_context`

```python
new_context(fresh=False, capture_exceptions: Optional[bool] = None)
```

Create a new context for managing shared state. Learn more about [contexts](/docs/libraries/python#contexts).

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`fresh` |  | Whether to create a fresh context that doesn't inherit from parent. | `False`
`capture_exceptions` | `Optional[bool]` | Whether to automatically capture exceptions in this context. If omitted, defaults to this client's exception autocapture setting. | `None`

**Examples:**

```python
with client.new_context():
    client.identify_context('<distinct_id>')
    client.capture('event_name')
```

### `Client.scoped`

```python
scoped(fresh=False, capture_exceptions: Optional[bool] = None)
```

Decorator that creates a new context for the wrapped function using this client.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`fresh` |  | Whether to create a fresh context that doesn't inherit from parent. | `False`
`capture_exceptions` | `Optional[bool]` | Whether to automatically capture exceptions in this context. If omitted, defaults to this client's exception autocapture setting. | `None`

### `Client.tag`

```python
tag(name: str, value: Any) -> None
```

Add a tag to the current context.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`name` | `str` | The tag key. | *required*
`value` | `Any` | The tag value. | *required*

### `Client.get_tags`

```python
get_tags() -> Dict[str, Any]
```

Get all tags from the current context.

**Returns:**

Type | Description
---- | -----------
`Dict[str, Any]` | Dict of all tags in the current context.

### `Client.set_context_option`

```python
set_context_option(key: str, value: Any) -> None
```

Set a capture option for every event captured within the current context.

Context options fill options an event leaves unset, before
`before_send` runs, so the hook sees them. They override
`super_options`. An event's own `options` override them, and
`before_send` can change them.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The option name, such as `"process_person_profile"`. | *required*
`value` | `Any` | The option value, sent as given. | *required*

### `Client.get_context_options`

```python
get_context_options() -> Dict[str, Any]
```

Get all capture options from the current context.

**Returns:**

Type | Description
---- | -----------
`Dict[str, Any]` | Dict of all capture options in the current context.

### `Client.identify_context`

```python
identify_context(distinct_id: str) -> None
```

Identify the current context with a distinct ID.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `str` | The distinct ID to associate with the current context and its children. | *required*

### `Client.set_context_session`

```python
set_context_session(session_id: str) -> None
```

Set the session ID for the current context.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`session_id` | `str` | The session ID to associate with the current context and its children. | *required*

### `Client.set_context_device_id`

```python
set_context_device_id(device_id: str) -> None
```

Set the device ID for the current context.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`device_id` | `str` | The device ID to associate with the current context and its children. | *required*

### `Client.feature_flags`

```python
feature_flags
```

Get the local evaluation feature flags.

### `Client.get_feature_payloads`

```python
get_feature_payloads(distinct_id: ID_TYPES, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, disable_geoip: Optional[bool] = None, flag_keys_to_evaluate: Optional[list[str]] = None, device_id: Optional[str] = None) -> dict[str, Optional[str]]
```

Get feature flag payloads for a user, preserving valid serialized JSON.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `ID_TYPES` | The distinct ID of the user. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`flag_keys_to_evaluate` | `Optional[list[str]]` | A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

**Examples:**

```python
payloads = posthog.get_feature_payloads('<distinct_id>')
```

### `Client.get_flags_decision`

```python
get_flags_decision(distinct_id: Optional[ID_TYPES] = None, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, disable_geoip: Optional[bool] = None, flag_keys_to_evaluate: Optional[list[str]] = None, device_id: Optional[str] = None) -> FlagsResponse
```

Get feature flags decision.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `Optional[ID_TYPES]` | The distinct ID of the user. | `None`
`groups` | `Optional[Mapping[str, Union[str, int]]]` | A dictionary of group information. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | A dictionary of person properties. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | A dictionary of group properties. | `None`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP for this request. | `None`
`flag_keys_to_evaluate` | `Optional[list[str]]` | A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. | `None`
`device_id` | `Optional[str]` | The device ID for this request. | `None`

**Examples:**

```python
decision = posthog.get_flags_decision('user123')
```

### `Client.metrics`

```python
metrics: PostHogMetrics
```

The `posthog.metrics` API: a statsd-style pre-aggregating metrics client — alpha.

Samples fold into per-series aggregates in memory and flush as one OTLP
data point per series per window, so recording from hot paths is cheap.
Configure via the `metrics` client option; pending metrics flush on
`shutdown()`.

**Examples:**

```python
client = Posthog("<ph_project_api_key>", metrics={"service_name": "billing-worker"})
client.metrics.count("invoices.processed", 1, attributes={"plan": "pro"})
client.metrics.gauge("queue.depth", 42)
client.metrics.histogram("job.duration", 187, unit="ms")
```

### `Client.get_remote_config_payload`

```python
get_remote_config_payload(key: str)
```

Get the payload for a remote config feature flag.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The remote config feature flag key. | *required*

**Returns:**

Type | Description
---- | -----------
 | The payload associated with the feature flag, or `None` if the client is disabled, no personal API key is configured, or the request fails. Encrypted payloads are decrypted by PostHog before being returned.

<details class="note" open markdown="1">
<summary>Note</summary>

Requires `secret_key` for authentication.

</details>

### `Client.get_feature_flag_evaluation_runtime`

```python
get_feature_flag_evaluation_runtime(key: str) -> Optional[FeatureFlagEvaluationRuntime]
```

Return where a locally loaded feature flag is meant to be evaluated.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key. | *required*

**Returns:**

Type | Description
---- | -----------
`Optional[FeatureFlagEvaluationRuntime]` | The flag's evaluation runtime, or `None` when local evaluation has not loaded a definition for this key. A definition that carries no runtime reports `FeatureFlagEvaluationRuntime.ALL`, the default PostHog applies.

**Examples:**

```python
from posthog import FeatureFlagEvaluationRuntime

runtime = posthog.get_feature_flag_evaluation_runtime("my-flag")
if runtime is FeatureFlagEvaluationRuntime.SERVER:
    ...
```

### `Client.get_feature_flag_keys_by_evaluation_runtime`

```python
get_feature_flag_keys_by_evaluation_runtime(evaluation_runtime: Union[FeatureFlagEvaluationRuntime, str]) -> list[str]
```

Return the keys of locally loaded flags that a runtime can evaluate.

A flag set to `FeatureFlagEvaluationRuntime.ALL` suits either runtime,
so it is returned for `CLIENT` and for `SERVER`, and asking for
`ALL` returns every loaded flag. Use this to decide which flags to hand
to a browser when a backend serves flags to its own frontend.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`evaluation_runtime` | `Union[FeatureFlagEvaluationRuntime, str]` | The runtime to match, as a `FeatureFlagEvaluationRuntime` or its string value. | *required*

**Returns:**

Type | Description
---- | -----------
`list[str]` | The matching flag keys, in the order local evaluation loaded them. Empty when no definitions are loaded.

**Examples:**

```python
from posthog import FeatureFlagEvaluationRuntime

client_keys = posthog.get_feature_flag_keys_by_evaluation_runtime(
    FeatureFlagEvaluationRuntime.CLIENT
)
```

### `posthog.integrations.django.PosthogContextMiddleware`

```python
PosthogContextMiddleware(get_response)
```

Middleware to automatically track Django requests.

This middleware wraps all calls with a posthog context. It attempts to extract the following from the request:
- Session ID, (extracted from `X-POSTHOG-SESSION-ID`)
- Distinct ID, (extracted from `X-POSTHOG-DISTINCT-ID`, falling back to the authenticated request user ID)
- Authenticated user email as `email`
- Request URL as `$current_url`
- Request method as `$request_method`
- Request path as `$request_path`
- Forwarded IP address as `$ip`
- User agent as `$user_agent`

Configure the integration with these Django settings:
- `POSTHOG_MW_CLIENT`: client used for automatic exception capture, or the global client.
  This does not redirect events explicitly captured through other clients.
- `POSTHOG_MW_CAPTURE_EXCEPTIONS`: `False` enables context enrichment only;
  `True` enables automatic exception capture; `None` inherits the effective client's
  `enable_exception_autocapture`. When omitted, the legacy default is `True`.
- `POSTHOG_MW_REQUEST_FILTER`: a callback receiving the request. Returning `False`
  disables both enrichment and automatic exception capture for that request.
- `POSTHOG_MW_EXTRA_PROPERTIES`: a callback returning additional event properties.
- `POSTHOG_MW_CAPTURE_TRACING_HEADERS`: read PostHog identity and session headers
  as request-scoped analytics context (default: `True`). Explicit capture IDs and
  event properties take precedence. These values are never authentication or
  authorization. Authenticated Django user identity remains the fallback when no
  usable header ID is available or header capture is disabled.
- `POSTHOG_MW_TRUST_TRACING_HEADERS`: deprecated alias for
  `POSTHOG_MW_CAPTURE_TRACING_HEADERS`; the capture-named setting takes precedence.
- `POSTHOG_MW_PROPERTIES_MAP`: an optional callback transforming request properties.

The old `POSTHOG_MW_EXTRA_TAGS` and `POSTHOG_MW_TAG_MAP` settings remain aliases;
the property-named settings take precedence when both are configured.

Request properties are included on all events captured within the active context,
including when automatic exception capture is disabled.
See the context documentation for more information. The extracted distinct ID and session ID,
if found, are used to associate all events captured in the middleware context with the same distinct ID
and session as currently active on the frontend. See the documentation for `set_context_session`
and `identify_context` for more details.

This middleware is hybrid-capable: it supports both WSGI (sync) and ASGI (async) Django applications. The middleware
detects at initialization whether the next middleware in the chain is async or sync, and adapts its behavior accordingly.
This ensures compatibility with both pure sync and pure async middleware chains, as well as mixed chains in ASGI mode.

**Functions:**

Name | Description
---- | -----------
`aextract_properties` | Async version of extract_properties for use in async request handling.
`aextract_request_user` | Async version of extract_request_user for use in async request handling.
`aextract_tags` | Compatibility alias for `aextract_properties`.
`extract_properties` | Extract event properties and identity from a synchronous request.
`extract_request_user` | Extract user ID and email from request in sync context.
`extract_tags` | Compatibility alias for `extract_properties`.
`process_exception` | Process exceptions from views and downstream middleware.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`async_capable` |  | 
`capture_exceptions` | `Optional[bool]` | 
`capture_tracing_headers` |  | 
`client` |  | 
`extra_properties` |  | 
`extra_tags` |  | Compatibility alias for `extra_properties`.
`get_response` |  | 
`properties_map` |  | 
`request_filter` |  | 
`sync_capable` |  | 
`tag_map` |  | Compatibility alias for `properties_map`.
`trust_tracing_headers` | `bool` | Deprecated alias for `capture_tracing_headers`.

Initialize the middleware with Django's next handler.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`get_response` |  | The next middleware or view handler in Django's middleware chain. May be synchronous or asynchronous. | 

#### `posthog.integrations.django.PosthogContextMiddleware.aextract_properties`

```python
async aextract_properties(request)
```

Async version of extract_properties for use in async request handling.

Uses await request.auser() instead of request.user to avoid
SynchronousOnlyOperation in async context.

Follows Django's naming convention for async methods (auser, asave, etc.).

#### `posthog.integrations.django.PosthogContextMiddleware.aextract_request_user`

```python
async aextract_request_user(request)
```

Async version of extract_request_user for use in async request handling.

Uses await request.auser() instead of request.user to avoid
SynchronousOnlyOperation in async context.

Follows Django's naming convention for async methods (auser, asave, etc.).

#### `posthog.integrations.django.PosthogContextMiddleware.aextract_tags`

```python
async aextract_tags(request)
```

Compatibility alias for `aextract_properties`.

#### `posthog.integrations.django.PosthogContextMiddleware.async_capable`

```python
async_capable = True
```

#### `posthog.integrations.django.PosthogContextMiddleware.capture_exceptions`

```python
capture_exceptions: Optional[bool] = capture_exceptions if (capture_exceptions is None or isinstance(capture_exceptions, bool)) else True
```

#### `posthog.integrations.django.PosthogContextMiddleware.capture_tracing_headers`

```python
capture_tracing_headers = _resolve_capture_tracing_headers(capture_tracing_headers, trust_tracing_headers, canonical_name='POSTHOG_MW_CAPTURE_TRACING_HEADERS', deprecated_name='POSTHOG_MW_TRUST_TRACING_HEADERS')
```

#### `posthog.integrations.django.PosthogContextMiddleware.client`

```python
client = cast('Optional[Client]', settings.POSTHOG_MW_CLIENT)
```

#### `posthog.integrations.django.PosthogContextMiddleware.extra_properties`

```python
extra_properties = cast('Callable[[HttpRequest], Dict[str, Any]]', extra_properties) if callable(extra_properties) else None
```

#### `posthog.integrations.django.PosthogContextMiddleware.extra_tags`

```python
extra_tags
```

Compatibility alias for `extra_properties`.

#### `posthog.integrations.django.PosthogContextMiddleware.extract_properties`

```python
extract_properties(request)
```

Extract event properties and identity from a synchronous request.

#### `posthog.integrations.django.PosthogContextMiddleware.extract_request_user`

```python
extract_request_user(request)
```

Extract user ID and email from request in sync context.

#### `posthog.integrations.django.PosthogContextMiddleware.extract_tags`

```python
extract_tags(request)
```

Compatibility alias for `extract_properties`.

#### `posthog.integrations.django.PosthogContextMiddleware.get_response`

```python
get_response = get_response
```

#### `posthog.integrations.django.PosthogContextMiddleware.process_exception`

```python
process_exception(request, exception)
```

Process exceptions from views and downstream middleware.

Django calls this WHILE still inside the context created by __call__,
so request tags have already been extracted and set. This method just
needs to capture the exception directly.

Django converts view exceptions into responses before they propagate through
the middleware stack, so the context manager in __call__/__acall__ never sees them.

Note: Django's process_exception is always synchronous, even for async views.

#### `posthog.integrations.django.PosthogContextMiddleware.properties_map`

```python
properties_map = cast('Callable[[Dict[str, Any]], Dict[str, Any]]', properties_map) if callable(properties_map) else None
```

#### `posthog.integrations.django.PosthogContextMiddleware.request_filter`

```python
request_filter = cast('Optional[Callable[[HttpRequest], bool]]', settings.POSTHOG_MW_REQUEST_FILTER)
```

#### `posthog.integrations.django.PosthogContextMiddleware.sync_capable`

```python
sync_capable = True
```

#### `posthog.integrations.django.PosthogContextMiddleware.tag_map`

```python
tag_map
```

Compatibility alias for `properties_map`.

#### `posthog.integrations.django.PosthogContextMiddleware.trust_tracing_headers`

```python
trust_tracing_headers: bool
```

Deprecated alias for `capture_tracing_headers`.

### `posthog.integrations.flask.PosthogFlaskIntegration`

```python
PosthogFlaskIntegration(app: Optional[Flask] = None, *, client: Optional[Client] = None, capture_exceptions: Optional[bool] = None, request_filter: Optional[Callable[[Request], bool]] = None, extra_properties: Optional[Callable[[Request], Mapping[str, Any]]] = None, trust_tracing_headers: Optional[bool] = None, capture_tracing_headers: Optional[bool] = None) -> None
```

Add PostHog request context and exception tracking to a Flask app.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`app` | `Optional[Flask]` | An optional Flask application. If omitted, call `init_app` later (the Flask application-factory pattern). | `None`
`client` | `Optional[Client]` | Optional destination for automatically captured exceptions. The global client is used by default. Request context still enriches events captured through any client and does not redirect capture calls. | `None`
`capture_exceptions` | `Optional[bool]` | Capture exceptions that reach Flask's unhandled exception machinery. If omitted, inherit the effective client's `enable_exception_autocapture` setting. Pass `True` or `False` to override it. | `None`
`request_filter` | `Optional[Callable[[Request], bool]]` | Optional callback receiving Flask's request object. A false return value disables both context and exception capture for that request. | `None`
`extra_properties` | `Optional[Callable[[Request], Mapping[str, Any]]]` | Optional callback returning additional event properties. This is useful for application-specific metadata such as an authenticated user's role. Values should not contain secrets or request bodies. | `None`
`capture_tracing_headers` | `Optional[bool]` | Read PostHog distinct and session ID headers as request-scoped analytics context. Defaults to True. Explicit capture IDs and event properties take precedence. Never use these values for security. | `None`
`trust_tracing_headers` | `Optional[bool]` | Deprecated alias for `capture_tracing_headers`. The capture-named option takes precedence when both are supplied. | `None`

The integration intentionally does not capture exceptions handled by an
application error handler, expected HTTP exceptions, request or response
bodies, query strings, cookies, authorization headers, or arbitrary headers.
Call `capture_exception` explicitly from a custom error handler if a
handled exception should be reported.

**Functions:**

Name | Description
---- | -----------
`init_app` | Register the integration with a Flask application once.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`capture_exceptions` |  | 
`capture_tracing_headers` |  | 
`client` |  | 
`extra_properties` |  | 
`request_filter` |  | 
`trust_tracing_headers` | `bool` | Deprecated alias for `capture_tracing_headers`.

#### `posthog.integrations.flask.PosthogFlaskIntegration.capture_exceptions`

```python
capture_exceptions = capture_exceptions
```

#### `posthog.integrations.flask.PosthogFlaskIntegration.capture_tracing_headers`

```python
capture_tracing_headers = _resolve_capture_tracing_headers(capture_tracing_headers, trust_tracing_headers)
```

#### `posthog.integrations.flask.PosthogFlaskIntegration.client`

```python
client = client
```

#### `posthog.integrations.flask.PosthogFlaskIntegration.extra_properties`

```python
extra_properties = extra_properties
```

#### `posthog.integrations.flask.PosthogFlaskIntegration.init_app`

```python
init_app(app: Flask) -> None
```

Register the integration with a Flask application once.

#### `posthog.integrations.flask.PosthogFlaskIntegration.request_filter`

```python
request_filter = request_filter
```

#### `posthog.integrations.flask.PosthogFlaskIntegration.trust_tracing_headers`

```python
trust_tracing_headers: bool
```

Deprecated alias for `capture_tracing_headers`.

### `posthog.integrations.asgi.PosthogASGIMiddleware`

```python
PosthogASGIMiddleware(app: _ASGIApp, client: Optional[Client] = None, capture_exceptions: Optional[bool] = None, request_filter: Optional[_RequestFilter] = None, extra_properties: Optional[_ExtraProperties] = None, trust_tracing_headers: Optional[bool] = None, *, capture_tracing_headers: Optional[bool] = None) -> None
```

Add PostHog context and automatic exception capture to an ASGI app.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`app` | `_ASGIApp` | The downstream ASGI application. | *required*
`client` | `Optional[Client]` | Optional destination for automatically captured exceptions. The global client is used by default. Request context still enriches events captured through any client and does not redirect capture calls. | `None`
`capture_exceptions` | `Optional[bool]` | Capture exceptions escaping the downstream app. If omitted, inherit the effective client's `enable_exception_autocapture` setting. Pass `True` or `False` to override it. | `None`
`request_filter` | `Optional[_RequestFilter]` | Optional sync or async callback receiving the ASGI scope. Returning `False` bypasses all instrumentation for that scope. | `None`
`extra_properties` | `Optional[_ExtraProperties]` | Optional sync or async callback receiving the ASGI scope and returning additional event properties. | `None`
`capture_tracing_headers` | `Optional[bool]` | Read PostHog distinct and session ID headers as request-scoped analytics context. Defaults to True. Explicit capture IDs and event properties take precedence. Never use these values for security. | `None`
`trust_tracing_headers` | `Optional[bool]` | Deprecated alias for `capture_tracing_headers`. The capture-named option takes precedence when both are supplied. | `None`

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`app` |  | 
`capture_exceptions` |  | 
`capture_tracing_headers` |  | 
`client` |  | 
`extra_properties` |  | 
`request_filter` |  | 
`trust_tracing_headers` | `bool` | Deprecated alias for `capture_tracing_headers`.

#### `posthog.integrations.asgi.PosthogASGIMiddleware.app`

```python
app = app
```

#### `posthog.integrations.asgi.PosthogASGIMiddleware.capture_exceptions`

```python
capture_exceptions = capture_exceptions
```

#### `posthog.integrations.asgi.PosthogASGIMiddleware.capture_tracing_headers`

```python
capture_tracing_headers = _resolve_capture_tracing_headers(capture_tracing_headers, trust_tracing_headers)
```

#### `posthog.integrations.asgi.PosthogASGIMiddleware.client`

```python
client = client
```

#### `posthog.integrations.asgi.PosthogASGIMiddleware.extra_properties`

```python
extra_properties = extra_properties
```

#### `posthog.integrations.asgi.PosthogASGIMiddleware.request_filter`

```python
request_filter = request_filter
```

#### `posthog.integrations.asgi.PosthogASGIMiddleware.trust_tracing_headers`

```python
trust_tracing_headers: bool
```

Deprecated alias for `capture_tracing_headers`.

### `posthog.integrations.celery.PosthogCeleryIntegration`

```python
PosthogCeleryIntegration(client: Optional[Client] = None, capture_exceptions: bool = True, capture_task_lifecycle_events: bool = True, propagate_context: bool = True, task_filter: Optional[Callable[[Optional[str], dict[str, Any]], bool]] = None)
```

Celery integration that captures task lifecycle events and exceptions.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`client` | `Optional[Client]` | Optional `Client` instance. When provided, all events and exceptions are captured through this client rather than the global `posthog` module. Don't skip this if using a custom flag definition cache provider, and pass the custom `Client` instance here initialized with the custom provider so fork safety for that provider is handled correctly. | `None`
`capture_exceptions` | `bool` | Whether to capture task exceptions via `capture_exception` (default `True`). | `True`
`capture_task_lifecycle_events` | `bool` | Whether to emit lifecycle events of the task such as "started", "success", "failure" etc. (default `True`). | `True`
`propagate_context` | `bool` | Whether to propagate PostHog context (distinct ID, session ID, tags, context options) from the producer to the worker via task headers (default `True`). | `True`
`task_filter` | `Optional[Callable[[Optional[str], dict[str, Any]], bool]]` | Optional callback `(task_name, task_properties) -> bool` expected to return `False` if a given task should not be tracked. | `None`

**Functions:**

Name | Description
---- | -----------
`instrument` | Connect Celery signal handlers to capture task events and exceptions. Call this after initializing the PostHog client and this integration.
`shutdown` | Disconnect all signal handlers registered by `instrument()`, flush all pending events and cleanly shutdown the integration.
`uninstrument` | Disconnect Celery signal handlers and unregister exit cleanup.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`capture_exceptions` |  | 
`capture_task_lifecycle_events` |  | 
`client` |  | 
`propagate_context` |  | 
`task_filter` |  | 

#### `posthog.integrations.celery.PosthogCeleryIntegration.capture_exceptions`

```python
capture_exceptions = capture_exceptions
```

#### `posthog.integrations.celery.PosthogCeleryIntegration.capture_task_lifecycle_events`

```python
capture_task_lifecycle_events = capture_task_lifecycle_events
```

#### `posthog.integrations.celery.PosthogCeleryIntegration.client`

```python
client = client
```

#### `posthog.integrations.celery.PosthogCeleryIntegration.instrument`

```python
instrument() -> None
```

Connect Celery signal handlers to capture task events and exceptions.
Call this after initializing the PostHog client and this integration.

If Celery runs on a single host, reinstrumenting in worker children is
not strictly necessary because the PostHog client and this integration
are fork-safe. If Celery workers run across multiple hosts, each worker
process must initialize PostHog, this integration, and call
`instrument()`. Celery provides `worker_process_init` signal to help
with this.

#### `posthog.integrations.celery.PosthogCeleryIntegration.propagate_context`

```python
propagate_context = propagate_context
```

#### `posthog.integrations.celery.PosthogCeleryIntegration.shutdown`

```python
shutdown() -> None
```

Disconnect all signal handlers registered by `instrument()`, flush all pending events
and cleanly shutdown the integration.

`shutdown()` is also registered on `worker_process_shutdown` and `atexit` signals,
but there is no guarantee those will always be called, so we strongly recommend calling
it manually when the integration is no longer needed to avoid data loss.

#### `posthog.integrations.celery.PosthogCeleryIntegration.task_filter`

```python
task_filter = task_filter
```

#### `posthog.integrations.celery.PosthogCeleryIntegration.uninstrument`

```python
uninstrument() -> None
```

Disconnect Celery signal handlers and unregister exit cleanup.

Do not use directly, call `shutdown()` instead.

### `posthog.request.set_socket_options`

```python
set_socket_options(socket_options: Optional[SocketOptions]) -> None
```

Configure socket options for all SDK HTTP connections.

Call this during initialization, before making API requests. Pass `None`
to reset to the default socket behavior.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`socket_options` | `Optional[SocketOptions]` | A list of `(level, option, value)` tuples accepted by urllib3/`socket.setsockopt()`, or `None` to reset defaults. | *required*

<details class="example" open markdown="1">
<summary>Example</summary>

from posthog import set_socket_options
set_socket_options([(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)])

</details>

### `posthog.request.enable_keep_alive`

```python
enable_keep_alive() -> None
```

Enable TCP keepalive for SDK HTTP connections.

This helps prevent idle pooled connections from being dropped by network
infrastructure. Call during initialization, before making API requests.

### `posthog.request.disable_connection_reuse`

```python
disable_connection_reuse() -> None
```

Disable HTTP connection reuse for SDK requests.

Each request will create a fresh connection. This can avoid issues with
environments that terminate pooled connections, but adds per-request
overhead. Call during initialization, before making API requests.

## AsyncClient

Needs the `async` extra, `posthog[async]`.

### `AsyncClient(...)`

```python
AsyncClient(project_api_key: str, host: Optional[str] = None, *, debug: bool = False, max_queue_size: int = 10000, send: bool = True, on_error=None, flush_at: int = 100, flush_interval: float = 5.0, max_retries: int = 3, timeout: float = 15, thread: int = 1, disabled: bool = False, disable_geoip: bool = True, is_server: bool = True, historical_migration: bool = False, super_properties: Optional[dict[str, Any]] = None, super_options: Optional[dict[str, Any]] = None, before_send=None, log_captured_exceptions: bool = False, project_root: Optional[str] = None, capture_exception_code_variables: bool = False, code_variables_mask_patterns=None, code_variables_ignore_patterns=None, code_variables_mask_url_credentials=None, code_variables_detect_secrets=None, in_app_modules: Optional[list[str]] = None, capture_compression: Optional[Union[CaptureCompression, str]] = None, capture_ai_compression: Optional[Union[CaptureCompression, str]] = None, capture_ai_max_queue_size: int = 1000, capture_ai_timeout: float = 30, capture_ai_max_event_bytes: int = AI_MAX_MSG_SIZE, privacy_mode: bool = False, enable_full_ai_capture: bool = False, capture_trace_context: bool = False, secret_key: Optional[str] = None, personal_api_key: Optional[str] = None, feature_flags_request_timeout_seconds: float = 3, feature_flags_request_max_retries: int = 1, app_host: Optional[str] = None, email_request_timeout_seconds: float = 15, email_request_max_retries: int = 2, migrated_from: Optional[str] = None) -> None
```

### `AsyncClient.capture`

```python
capture(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Queue an event without blocking for network delivery.

### `AsyncClient.capture_immediate`

```python
async capture_immediate(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Capture one event and wait until its delivery attempt completes.

### `AsyncClient.set`

```python
set(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

### `AsyncClient.set_once`

```python
set_once(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

### `AsyncClient.alias`

```python
alias(previous_id: ID_TYPES, distinct_id: Optional[str], timestamp: Optional[Union[datetime, str]] = None, uuid: Optional[str] = None, disable_geoip: Optional[bool] = None, options: Optional[Dict[str, Any]] = None) -> Optional[str]
```

### `AsyncClient.capture_exception`

```python
capture_exception(exception: Optional[ExceptionArg] = None, *, level: Optional[str] = None, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Capture an exception. This method never raises, including in debug mode.

`level` sets `$exception_level` and defaults to `"error"`. An unknown
value counts as unset. Reserved exception properties in `properties`,
such as `$exception_list` and `$exception_level`, are ignored.

When OpenTelemetry is installed and a valid span is active, its trace and
span IDs are added as `$trace_id` and `$span_id` event properties,
unless the event, a context tag or `super_properties` sets either one.

### `AsyncClient.emails`

```python
emails: AsyncEmails
```

Send transactional email from PostHog email templates or from content
in your code, and read the status of each email sent with `get`.

Needs `secret_key`, a project secret API key with the `transactional_email:write`
scope, and `posthog[async]`. Sends go to `app_host`, or to the PostHog
Cloud app host of `host`. `email_request_timeout_seconds` and
`email_request_max_retries` configure each send. A disabled or shut-down
client sends nothing. See `posthog.emails` for setup.

**Examples:**

```python
await client.emails.send(
    to="user@example.com",
    template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b",
    distinct_id="user_123",
)
```

### `AsyncClient.flush`

```python
async flush(timeout_seconds: Optional[float] = 10) -> None
```

### `AsyncClient.evaluate_flags`

```python
async evaluate_flags(distinct_id: Optional[ID_TYPES] = None, *, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, disable_geoip: Optional[bool] = None, flag_keys: Optional[list[str]] = None, device_id: Optional[str] = None) -> FeatureFlagEvaluations
```

Evaluate flags remotely without blocking the event loop.

The returned snapshot has synchronous in-memory accessors. Pass it to
`capture(flags=...)` to attach the exact values used for branching.
This client does not evaluate flags locally, so the snapshot's
`unresolved_flags` is always empty.

### `AsyncClient.group_identify`

```python
group_identify(group_type: str, group_key: str, properties: Optional[Dict[str, Any]] = None, timestamp: Optional[Union[datetime, str]] = None, uuid: Optional[Union[str, UUID]] = None, disable_geoip: Optional[bool] = None, distinct_id: Optional[ID_TYPES] = None, options: Optional[Dict[str, Any]] = None) -> Optional[str]
```

### `AsyncClient.shutdown`

```python
async shutdown() -> None
```

### `AsyncClient.join`

```python
async join() -> None
```

### `AsyncClient.capture_ai`

```python
capture_ai(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Queue an AI event for the AI capture endpoint without blocking.

Takes the same arguments and returns the same value as `capture()`.
The event uses a separate queue with a larger per-event size cap. The
payload is sent as given, with no redaction or truncation.

### `AsyncClient.capture_ai_immediate`

```python
async capture_ai_immediate(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Capture one AI event and wait until its delivery attempt completes.

Uses the AI capture endpoint and the `capture_ai_*` settings.

### `AsyncClient.get_remote_config_payload`

```python
async get_remote_config_payload(key: str) -> Optional[Any]
```

Fetch and decrypt a remote-config payload without blocking the loop.

## Module functions

### `posthog.new_context`

```python
new_context(fresh: bool = False, capture_exceptions: Optional[bool] = None, client: Optional[Client] = None)
```

Create a new context scope that will be active for the duration of the with block.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`fresh` | `bool` | Whether to start with a fresh context (default: False) | `False`
`capture_exceptions` | `Optional[bool]` | Whether to capture exceptions raised within the context. If omitted, defaults to the relevant client's exception autocapture setting. | `None`
`client` | `Optional[Client]` | Optional Posthog client instance to use for this context (default: None) | `None`

**Examples:**

```python
from posthog import new_context, tag, capture
with new_context():
    tag("request_id", "123")
    capture("event_name", properties={"property": "value"})
```

### `posthog.scoped`

```python
scoped(fresh=False, capture_exceptions: Optional[bool] = None)
```

Decorator that creates a new context for the function.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`fresh` |  | Whether to start with a fresh context (default: False) | `False`
`capture_exceptions` | `Optional[bool]` | Whether to capture and track exceptions with posthog error tracking. If omitted, defaults to the global exception autocapture setting. | `None`

**Examples:**

```python
from posthog import scoped, tag, capture
@scoped()
def process_payment(payment_id):
    tag("payment_id", payment_id)
    capture("payment_started")
```

### `posthog.set_context_session`

```python
set_context_session(session_id: str)
```

Set the session ID for the current context.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`session_id` | `str` | The session ID to associate with the current context and its children | *required*

**Examples:**

```python
from posthog import set_context_session
set_context_session("session_123")
```

### `posthog.set_context_device_id`

```python
set_context_device_id(device_id: str)
```

Set the device ID for the current context, associating all feature flag requests
in this or child contexts with the given device ID.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`device_id` | `str` | The device ID to associate with the current context and its children | *required*

**Examples:**

```python
from posthog import set_context_device_id
set_context_device_id("device_123")
```

### `posthog.identify_context`

```python
identify_context(distinct_id: str)
```

Identify the current context with a distinct ID.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `str` | The distinct ID to associate with the current context and its children | *required*

**Examples:**

```python
from posthog import identify_context
identify_context("user_123")
```

### `posthog.set_capture_exception_code_variables_context`

```python
set_capture_exception_code_variables_context(enabled: bool)
```

Override code-variable capture for exceptions in the current context.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`enabled` | `bool` | Whether exceptions captured in this context should include local variable values from stack frames. | *required*

### `posthog.set_code_variables_mask_patterns_context`

```python
set_code_variables_mask_patterns_context(mask_patterns: list[str])
```

Override code-variable mask patterns for exceptions in the current context.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`mask_patterns` | `list[str]` | Variable-name patterns whose values should be replaced with `***` when code variables are captured. | *required*

### `posthog.set_code_variables_ignore_patterns_context`

```python
set_code_variables_ignore_patterns_context(ignore_patterns: list[str])
```

Override code-variable ignore patterns for exceptions in the current context.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`ignore_patterns` | `list[str]` | Variable-name patterns that should be omitted entirely when code variables are captured. | *required*

### `posthog.set_code_variables_mask_url_credentials_context`

```python
set_code_variables_mask_url_credentials_context(enabled: bool)
```

Whether to scrub credentials embedded in URLs/DSNs (e.g. user:pass@host) from
captured code variables for the current context.

### `posthog.set_code_variables_detect_secrets_context`

```python
set_code_variables_detect_secrets_context(enabled: bool)
```

Whether to apply entropy-based secret detection as a last-resort redaction of
high-entropy values (API keys, tokens, strong passwords) in captured code
variables for the current context.

### `posthog.tag`

```python
tag(name: str, value: Any)
```

Add a tag to the current context.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`name` | `str` | The tag key | *required*
`value` | `Any` | The tag value | *required*

**Examples:**

```python
from posthog import tag
tag("user_id", "123")
```

### `posthog.get_tags`

```python
get_tags() -> Dict[str, Any]
```

Get all tags from the current context.

**Returns:**

Type | Description
---- | -----------
`Dict[str, Any]` | Dict of all tags in the current context

### `posthog.set_context_option`

```python
set_context_option(key: str, value: Any) -> None
```

Set a capture option for every event captured within the current context.

Context options fill options an event leaves unset, before `before_send`
runs, so the hook sees them. They override `super_options`. An event's
own `options` override them, and `before_send` can change them.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The option name, such as `"process_person_profile"` | *required*
`value` | `Any` | The option value, sent as given | *required*

**Examples:**

```python
from posthog import new_context, set_context_option
with new_context():
    set_context_option("process_person_profile", False)
```

### `posthog.get_context_options`

```python
get_context_options() -> Dict[str, Any]
```

Get all capture options from the current context.

**Returns:**

Type | Description
---- | -----------
`Dict[str, Any]` | Dict of all capture options in the current context

### `posthog.capture`

```python
capture(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Capture anything a user does within your system.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`event` | `str` | The event name to specify the event | *required*
`**kwargs` | `Unpack[OptionalCaptureArgs]` | Optional arguments including: distinct_id: Unique identifier for the user properties: Dict of event properties timestamp: When the event occurred. UTC is preferred; non-UTC     datetimes and parseable ISO timestamp strings are converted to UTC. uuid: Unique identifier for this event. If omitted, one is generated     and returned. If provided, it must be a valid UUID string or     uuid.UUID instance; invalid values are ignored and replaced with     a newly generated UUID. groups: Dict of group types and IDs flags: A FeatureFlagEvaluations snapshot from evaluate_flags(). The     exact values from the snapshot are attached with no extra /flags     request. send_feature_flags: Deprecated. Prefer flags=... from     evaluate_flags(). When truthy, evaluates flags during capture and     attaches them to the event. disable_geoip: Whether to disable GeoIP lookup | `{}`

<details class="details" open markdown="1">
<summary>Details</summary>

Capture allows you to capture anything a user does within your system, which you can later use in PostHog to find patterns in usage, work out which features to improve or where people are giving up. A capture call requires an event name to specify the event. We recommend using [verb] [noun], like `movie played` or `movie updated` to easily identify what your events mean later on. Capture takes a number of optional arguments, which are defined by the `OptionalCaptureArgs` type.

</details>

**Examples:**

```python
# Context and capture usage
from posthog import new_context, identify_context, tag, capture
# Enter a new context (e.g. a request/response cycle, an instance of a background job, etc)
with new_context():
    # Associate this context with some user, by distinct_id
    identify_context('some user')

    # Capture an event, associated with the context-level distinct ID ('some user')
    capture('movie started')

    # Capture an event associated with some other user (overriding the context-level distinct ID)
    capture('movie joined', distinct_id='some-other-user')

    # Capture an event with some properties
    capture('movie played', properties={'movie_id': '123', 'category': 'romcom'})

    # Capture an event with some properties
    capture('purchase', properties={'product_id': '123', 'category': 'romcom'})
    # Capture an event with some associated group
    capture('purchase', groups={'company': 'id:5'})

    # Adding a tag to the current context will cause it to appear on all subsequent events
    tag('some-tag', 'some-value')

    capture('another-event') # Will be captured with `'some-tag': 'some-value'` in the properties dict
```
```python
# Set event properties
from posthog import capture
capture(
    "user_signed_up",
    distinct_id="distinct_id_of_the_user",
    properties={
        "login_type": "email",
        "is_free_trial": "true"
    }
)
```

### `posthog.capture_ai`

```python
capture_ai(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Capture an AI event on the dedicated AI capture endpoint.

Beta: the signature is stable; operational limits (per-event size cap,
batching, endpoint) may change without notice.

Takes the same arguments and returns the same value as `capture()`: the
event UUID, or None when the event was not admitted (disabled client, or
dropped by `before_send`). The event is delivered on an isolated queue
with its own consumer pool and a higher per-event size cap, posting to
the dedicated AI ingestion endpoint. The payload is sent as given — no
redaction or truncation is applied here.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`event` | `str` | The event name, normally one of the `$ai_*` event names. | *required*
`**kwargs` | `Unpack[OptionalCaptureArgs]` | Same optional arguments as `capture()`. | `{}`

**Examples:**

```python
from posthog import capture_ai

uuid = capture_ai(
    "$ai_generation",
    distinct_id="user_123",
    properties={"$ai_model": "gpt-5"},
)
```

### `posthog.set`

```python
set(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

Set properties on a user record.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`**kwargs` | `Unpack[OptionalSetArgs]` | Optional arguments including: distinct_id: Unique identifier for the user. Falls back to the     context distinct ID; if none exists, this call does nothing. properties: Dict of person properties to set. timestamp: When the properties were set. UTC is preferred; non-UTC     datetimes and parseable ISO timestamp strings are converted to UTC. uuid: Unique identifier for this operation. If omitted, one is     generated and returned. If provided, it must be a valid UUID     string or uuid.UUID instance; invalid values are ignored and     replaced with a newly generated UUID. disable_geoip: Whether to disable GeoIP lookup. | `{}`

<details class="details" open markdown="1">
<summary>Details</summary>

This will overwrite previous people property values. Generally operates similar to `capture`, with distinct_id being an optional argument, defaulting to the current context's distinct ID. If there is no context-level distinct ID, and no override distinct_id is passed, this function will do nothing. Context tags are folded into $set properties, so tagging the current context and then calling `set` will cause those tags to be set on the user (unlike capture, which causes them to just be set on the event).

</details>

**Examples:**

```python
# Set person properties
from posthog import set
set(distinct_id='distinct_id', properties={'name': 'Max Hedgehog'})
```

### `posthog.set_once`

```python
set_once(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

Set properties on a user record, only if they do not yet exist.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`**kwargs` | `Unpack[OptionalSetArgs]` | Optional arguments including: distinct_id: Unique identifier for the user. Falls back to the     context distinct ID; if none exists, this call does nothing. properties: Dict of person properties to set only once. timestamp: When the properties were set. UTC is preferred; non-UTC     datetimes and parseable ISO timestamp strings are converted to UTC. uuid: Unique identifier for this operation. If omitted, one is     generated and returned. If provided, it must be a valid UUID     string or uuid.UUID instance; invalid values are ignored and     replaced with a newly generated UUID. disable_geoip: Whether to disable GeoIP lookup. | `{}`

<details class="details" open markdown="1">
<summary>Details</summary>

This will not overwrite previous people property values, unlike `set`. Otherwise, operates in an identical manner to `set`.

</details>

**Examples:**

```python
# Set property once
from posthog import set_once
set_once(distinct_id='distinct_id', properties={'initial_url': '/blog'})

```

### `posthog.group_identify`

```python
group_identify(group_type: str, group_key: str, properties: Optional[Dict[str, Any]] = None, timestamp: Optional[Union[datetime.datetime, str]] = None, uuid: Optional[str] = None, disable_geoip: Optional[bool] = None, distinct_id: Optional[ID_TYPES] = None, options: Optional[Dict[str, Any]] = None) -> Optional[str]
```

Set properties on a group.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`group_type` | `str` | Type of your group. Required - the call is dropped with a warning if it is missing or empty. | *required*
`group_key` | `str` | Unique identifier of the group. Required - the call is dropped with a warning if it is missing or empty. | *required*
`properties` | `Optional[Dict[str, Any]]` | Properties to set on the group | `None`
`timestamp` | `Optional[Union[datetime, str]]` | Optional timestamp for the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. | `None`
`uuid` | `Optional[str]` | Optional UUID for the event | `None`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup | `None`
`distinct_id` | `Optional[ID_TYPES]` | Optional distinct ID of the user performing the action | `None`
`options` | `Optional[Dict[str, Any]]` | Optional capture options for the event, sent as given | `None`

**Examples:**

```python
# Group identify
from posthog import group_identify
group_identify('company', 'company_id_in_your_db', {
    'name': 'Awesome Inc.',
    'employees': 11
})
```

### `posthog.alias`

```python
alias(previous_id: ID_TYPES, distinct_id: str, timestamp: Optional[Union[datetime.datetime, str]] = None, uuid: Optional[str] = None, disable_geoip: Optional[bool] = None, options: Optional[Dict[str, Any]] = None) -> Optional[str]
```

Associate user behaviour before and after they e.g. register, login, or perform some other identifying action.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`previous_id` | `ID_TYPES` | The unique ID of the user before | *required*
`distinct_id` | `str` | The current unique id | *required*
`timestamp` | `Optional[Union[datetime, str]]` | Optional timestamp for the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. | `None`
`uuid` | `Optional[str]` | Optional UUID for the event | `None`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup | `None`
`options` | `Optional[Dict[str, Any]]` | Optional capture options for the event, sent as given | `None`

<details class="details" open markdown="1">
<summary>Details</summary>

To marry up whatever a user does before they sign up or log in with what they do after you need to make an alias call. This will allow you to answer questions like "Which marketing channels leads to users churning after a month?" or "What do users do on our website before signing up?". Particularly useful for associating user behaviour before and after they e.g. register, login, or perform some other identifying action.

</details>

**Examples:**

```python
# Alias user
from posthog import alias
alias(previous_id='distinct_id', distinct_id='alias_id')
```

### `posthog.capture_exception`

```python
capture_exception(exception: Optional[ExceptionArg] = None, *, level: Optional[str] = None, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

Capture exceptions that happen in your code.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`exception` | `Optional[ExceptionArg]` | The exception to capture. If not provided, the current exception is captured via `sys.exc_info()` | `None`
`level` | `Optional[str]` | The `$exception_level`, such as `"warning"` or `"fatal"`. Defaults to `"error"`. An unknown value counts as unset. | `None`
`**kwargs` | `Unpack[OptionalCaptureArgs]` | Optional capture arguments including distinct_id, properties, timestamp, uuid, groups, flags, send_feature_flags, disable_geoip, and options. Reserved exception properties in `properties`, such as `$exception_list` and `$exception_level`, are ignored. | `{}`

<details class="details" open markdown="1">
<summary>Details</summary>

Capture exception is idempotent - if it is called twice with the same exception instance, only a occurrence will be tracked in posthog. This is because, generally, contexts will cause exceptions to be captured automatically. However, to ensure you track an exception, if you catch and do not re-raise it, capturing it manually is recommended, unless you are certain it will have crossed a context boundary (e.g. by existing a `with posthog.new_context():` block already). If the passed exception was raised and caught, the captured stack trace will consist of every frame between where the exception was raised and the point at which it is captured (the "traceback"). If the passed exception was never raised, e.g. if you call `posthog.capture_exception(ValueError("Some Error"))`, the stack trace captured will be the full stack trace at the moment the exception was captured. Note that heavy use of contexts will lead to truncated stack traces, as the exception will be captured by the context entered most recently, which may not be the point you catch the exception for the final time in your code. It's recommended to use contexts sparingly, for this reason. `capture_exception` takes the same set of optional arguments as `capture`.

</details>

**Examples:**

```python
# Capture exception
from posthog import capture_exception
try:
    risky_operation()
except Exception as e:
    capture_exception(e)
```

### `posthog.feature_enabled`

```python
feature_enabled(key: str, distinct_id: ID_TYPES, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, send_feature_flag_events: bool = True, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None) -> Optional[bool]
```

Use feature flags to enable or disable features for users.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key | *required*
`distinct_id` | `ID_TYPES` | The user's distinct ID | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | Groups mapping | `None`
`person_properties` | `Optional[Dict[str, Any]]` | Person properties | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | Group properties | `None`
`only_evaluate_locally` | `bool` | Whether to evaluate only locally | `False`
`send_feature_flag_events` | `bool` | Whether to send feature flag events | `True`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup | `None`
`device_id` | `Optional[str]` | Optional device ID override for experience-continuity flags | `None`

<details class="details" open markdown="1">
<summary>Details</summary>

You can call `posthog.load_feature_flags()` before to make sure you're not doing unexpected requests.

</details>

**Examples:**

```python
# Boolean feature flag
from posthog import feature_enabled, get_feature_flag_payload
is_my_flag_enabled = feature_enabled('flag-key', 'distinct_id_of_your_user')
if is_my_flag_enabled:
    matched_flag_payload = get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

### `posthog.get_feature_flag`

```python
get_feature_flag(key: str, distinct_id: ID_TYPES, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, send_feature_flag_events: bool = True, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None) -> Optional[FlagValue]
```

Get feature flag variant for users. Used with experiments.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key | *required*
`distinct_id` | `ID_TYPES` | The user's distinct ID | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | Groups mapping from group type to group key | `None`
`person_properties` | `Optional[Dict[str, Any]]` | Person properties | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | Group properties in format { group_type_name: { group_properties } } | `None`
`only_evaluate_locally` | `bool` | Whether to evaluate only locally | `False`
`send_feature_flag_events` | `bool` | Whether to send feature flag events | `True`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup | `None`
`device_id` | `Optional[str]` | Optional device ID override for experience-continuity flags | `None`

<details class="details" open markdown="1">
<summary>Details</summary>

`groups` are a mapping from group type to group key. So, if you have a group type of "organization" and a group key of "5", you would pass groups={"organization": "5"}. `group_properties` take the format: { group_type_name: { group_properties } }. So, for example, if you have the group type "organization" and the group key "5", with the properties name, and employee count, you'll send these as: group_properties={"organization": {"name": "PostHog", "employees": 11}}.

</details>

**Examples:**

```python
# Multivariate feature flag
from posthog import get_feature_flag, get_feature_flag_payload
enabled_variant = get_feature_flag('flag-key', 'distinct_id_of_your_user')
if enabled_variant == 'variant-key':
    matched_flag_payload = get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

### `posthog.get_all_flags`

```python
get_all_flags(distinct_id: ID_TYPES, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None, flag_keys_to_evaluate: Optional[list[str]] = None) -> Optional[dict[str, FlagValue]]
```

Get all flags for a given user.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `ID_TYPES` | The user's distinct ID | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | Groups mapping | `None`
`person_properties` | `Optional[Dict[str, Any]]` | Person properties | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | Group properties | `None`
`only_evaluate_locally` | `bool` | Whether to evaluate only locally | `False`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup | `None`
`device_id` | `Optional[str]` | Optional device ID override for experience-continuity flags | `None`
`flag_keys_to_evaluate` | `Optional[list[str]]` | Optional list of flag keys to evaluate (evaluates all if None) | `None`

<details class="details" open markdown="1">
<summary>Details</summary>

Flags are key-value pairs where the key is the flag key and the value is the flag variant, or True, or False.

</details>

**Examples:**

```python
# All flags for user
from posthog import get_all_flags
get_all_flags('distinct_id_of_your_user')
```

### `posthog.get_feature_flag_result`

```python
get_feature_flag_result(key: str, distinct_id: ID_TYPES, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, send_feature_flag_events: bool = True, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None) -> Optional[FeatureFlagResult]
```

Get a FeatureFlagResult object which contains the flag result and payload.

This method evaluates a feature flag and returns a FeatureFlagResult object containing:
- enabled: Whether the flag is enabled
- variant: The variant value if the flag has variants
- payload: The payload associated with the flag (automatically deserialized from JSON)
- key: The flag key
- reason: Why the flag was enabled/disabled

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key. | *required*
`distinct_id` | `ID_TYPES` | The user's distinct ID. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | Mapping of group type to group key. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | Person properties to use for evaluation. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | Group properties keyed by group type. | `None`
`only_evaluate_locally` | `bool` | Whether to evaluate only locally. | `False`
`send_feature_flag_events` | `bool` | Whether to send a $feature_flag_called event. | `True`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup. | `None`
`device_id` | `Optional[str]` | Optional device ID override for experience-continuity flags. | `None`

Example:
```python
result = posthog.get_feature_flag_result('beta-feature', 'distinct_id')
if result and result.enabled:
    # Use the variant and payload
    print(f"Variant: {result.variant}")
    print(f"Payload: {result.payload}")
```

### `posthog.get_feature_flag_payload`

```python
get_feature_flag_payload(key: str, distinct_id: ID_TYPES, match_value: Optional[FlagValue] = None, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, send_feature_flag_events: bool = True, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None) -> Optional[object]
```

Get the payload associated with a feature flag value.

Deprecated for new code. Prefer `evaluate_flags()` and
`flags.get_flag_payload(key)` so flag evaluation happens once per request.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key. | *required*
`distinct_id` | `ID_TYPES` | The user's distinct ID. | *required*
`match_value` | `Optional[FlagValue]` | Optional flag value to use when selecting a payload. | `None`
`groups` | `Optional[Mapping[str, Union[str, int]]]` | Mapping of group type to group key. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | Person properties to use for evaluation. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | Group properties keyed by group type. | `None`
`only_evaluate_locally` | `bool` | Whether to evaluate only locally. | `False`
`send_feature_flag_events` | `bool` | Whether to send a $feature_flag_called event. | `True`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup. | `None`
`device_id` | `Optional[str]` | Optional device ID override for experience-continuity flags. | `None`

**Returns:**

Type | Description
---- | -----------
`Optional[object]` | The payload associated with the matched feature flag value, or None. This function returns the payload only, not the FeatureFlagResult wrapper used internally to compute it.

### `posthog.get_remote_config_payload`

```python
get_remote_config_payload(key: str)
```

Get the payload for a remote config feature flag.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The key of the feature flag | *required*

**Returns:**

Type | Description
---- | -----------
 | The payload associated with the feature flag. If payload is encrypted, the return value will be decrypted

<details class="note" open markdown="1">
<summary>Note</summary>

Requires secret_key to be set for authentication

</details>

### `posthog.get_all_flags_and_payloads`

```python
get_all_flags_and_payloads(distinct_id: ID_TYPES, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, disable_geoip: Optional[bool] = None, device_id: Optional[str] = None, flag_keys_to_evaluate: Optional[list[str]] = None) -> FlagsAndPayloads
```

Get all feature flag values and payloads for a user.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `ID_TYPES` | The user's distinct ID. | *required*
`groups` | `Optional[Mapping[str, Union[str, int]]]` | Mapping of group type to group key. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | Person properties to use for evaluation. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | Group properties keyed by group type. | `None`
`only_evaluate_locally` | `bool` | Whether to evaluate only locally. | `False`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup. | `None`
`device_id` | `Optional[str]` | Optional device ID override for experience-continuity flags. | `None`
`flag_keys_to_evaluate` | `Optional[list[str]]` | Optional list of flag keys to evaluate. Evaluates all flags when omitted. | `None`

**Returns:**

Type | Description
---- | -----------
`FlagsAndPayloads` | A dict with `featureFlags` and `featureFlagPayloads` entries.

### `posthog.evaluate_flags`

```python
evaluate_flags(distinct_id: Optional[ID_TYPES] = None, groups: Optional[Mapping[str, Union[str, int]]] = None, person_properties: Optional[Dict[str, Any]] = None, group_properties: Optional[Dict[str, Dict[str, Any]]] = None, only_evaluate_locally: bool = False, disable_geoip: Optional[bool] = None, flag_keys: Optional[list[str]] = None, device_id: Optional[str] = None) -> FeatureFlagEvaluations
```

Evaluate all feature flags for a user in a single call and return a
`FeatureFlagEvaluations` snapshot. Branch on `.is_enabled()` /
`.get_flag()` and pass the same snapshot to `capture()` via the
`flags` option so events carry the exact flag values the code branched on.

Prefer this over repeated `get_feature_flag()` calls and over
`capture(send_feature_flags=True)` — it consolidates flag evaluation into
a single `/flags` request per incoming request.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` | `Optional[ID_TYPES]` | The user's distinct ID. If `None`, falls back to the context distinct_id. If still unresolvable, returns an empty snapshot. | `None`
`groups` | `Optional[Mapping[str, Union[str, int]]]` | Mapping of group type to group key. | `None`
`person_properties` | `Optional[Dict[str, Any]]` | Person properties to use for evaluation. | `None`
`group_properties` | `Optional[Dict[str, Dict[str, Any]]]` | Group properties keyed by group type. | `None`
`only_evaluate_locally` | `bool` | If `True`, never fall back to remote evaluation and omit flags that cannot be evaluated locally. The snapshot's `unresolved_flags` lists them with a reason. | `False`
`disable_geoip` | `Optional[bool]` | Whether to disable GeoIP lookup. | `None`
`flag_keys` | `Optional[list[str]]` | Optional list that scopes local evaluation, the underlying `/flags` request, and the returned snapshot. When omitted or `None`, all flags are evaluated. An empty list returns an empty snapshot without evaluating flags. A requested key absent from loaded local definitions is included in one remote fallback per `evaluate_flags` call unless `only_evaluate_locally` is `True`. If the server also does not know the key, it is omitted from the snapshot. | `None`
`device_id` | `Optional[str]` | Optional device ID override. If not provided, falls back to the context device_id (which may be set via tracing headers). Used by experience-continuity flags to match users across distinct_id changes. | `None`

**Examples:**

```python
from posthog import evaluate_flags, capture
flags = evaluate_flags("user_123", person_properties={"plan": "enterprise"})
if flags.is_enabled("new-dashboard"):
    render_new_dashboard()
capture("page_viewed", distinct_id="user_123", flags=flags)
```

### `posthog.feature_flag_definitions`

```python
feature_flag_definitions()
```

Returns loaded feature flags.

<details class="details" open markdown="1">
<summary>Details</summary>

Returns loaded feature flags, if any. Helpful for debugging what flag information you have loaded.

</details>

**Examples:**

```python
from posthog import feature_flag_definitions
definitions = feature_flag_definitions()
```

### `posthog.get_feature_flag_evaluation_runtime`

```python
get_feature_flag_evaluation_runtime(key: str) -> Optional[FeatureFlagEvaluationRuntime]
```

Return where a locally loaded feature flag is meant to be evaluated.

<details class="details" open markdown="1">
<summary>Details</summary>

Reads the `evaluation_runtime` each flag definition carries, so no extra
request is made. Returns `None` when local evaluation has not loaded a
definition for this key. A definition that carries no runtime reports
`FeatureFlagEvaluationRuntime.ALL`, the default PostHog applies.

</details>

**Examples:**

```python
from posthog import FeatureFlagEvaluationRuntime, get_feature_flag_evaluation_runtime
runtime = get_feature_flag_evaluation_runtime("my-flag")
```

### `posthog.get_feature_flag_keys_by_evaluation_runtime`

```python
get_feature_flag_keys_by_evaluation_runtime(evaluation_runtime: Union[FeatureFlagEvaluationRuntime, str]) -> list[str]
```

Return the keys of locally loaded flags that a runtime can evaluate.

<details class="details" open markdown="1">
<summary>Details</summary>

A flag set to `FeatureFlagEvaluationRuntime.ALL` suits either runtime, so
it is returned for `CLIENT` and for `SERVER`. Use this to decide which
flags to hand to a browser when a backend serves flags to its own
frontend.

</details>

**Examples:**

```python
from posthog import FeatureFlagEvaluationRuntime, get_feature_flag_keys_by_evaluation_runtime
client_keys = get_feature_flag_keys_by_evaluation_runtime(FeatureFlagEvaluationRuntime.CLIENT)
```

### `posthog.load_feature_flags`

```python
load_feature_flags()
```

Load feature flag definitions from PostHog.

**Examples:**

```python
from posthog import load_feature_flags
load_feature_flags()
```

### `posthog.flush`

```python
flush(timeout_seconds: Optional[float] = 10) -> None
```

Attempt immediate delivery of queued events and ended spans, keeping the global client reusable.

Call `flush()` before a short-lived request or serverless invocation returns
when using the shared module-level client. Use `shutdown()` only for final
process cleanup or a client that will not be reused. Flush does not stop the
background workers or feature flag poller, and does not end open spans.

Returning does not guarantee server receipt: pending work can remain after a
timeout or span export failure, and failed events may be dropped by consumers.
Calls made directly from SDK callbacks such as `on_error` are deferred
rather than blocking the worker that invoked the callback.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`timeout_seconds` | `Optional[float]` | Seconds to wait for event queues to drain, shared across event lanes. Defaults to 10 seconds. Pass `None` to wait without a time limit; this does not guarantee successful delivery. Queued spans flush concurrently, waiting up to this many seconds for an in-flight flush before starting a fresh drain budget. A lock wait timeout leaves the in-flight flush to finish. At least one span request is attempted after acquiring the lock, even with no budget left; retriable failures are retried after backoff while budget remains, with one last attempt at the deadline. The last span request is not cut short and is bounded by the client's `timeout` in seconds, so this is not a strict wall-clock limit for the whole call. With `None`, a retriable span failure is left for a later flush rather than retried within this call. | `10`

**Examples:**

```python
import posthog
posthog.capture('event_name', distinct_id='user_id')
# Before a serverless invocation returns; keep the global client open.
posthog.flush(timeout_seconds=10)
```

### `posthog.join`

```python
join() -> None
```

Attempt to process queued events and stop the client's background workers. Use `shutdown()` directly in most cases.

Failed or undrainable events may be dropped and reported through logging or
`on_error`; returning does not guarantee server receipt. Lifecycle cleanup
is attempted once, and cleanup failures are logged without retry.

**Examples:**

```python
from posthog import join
join()
```

### `posthog.start_span`

```python
start_span(name: str, *, kind: Optional[str] = None, attributes: Optional[Mapping[str, Any]] = None, parent: Union[Span, str, None] = None, tracestate: Optional[str] = None, start_time: Union[datetime.datetime, float, None] = None) -> Span
```

Start a span for distributed tracing. Alpha.

Returns a span handle. Use it as a context manager to make it the active
span for the block and end it on exit (recording a raised exception on the
way out); or call `end()` yourself for a span that cannot wrap a block.
Spans started inside the block nest under it automatically. Always returns
a usable handle, even when tracing is off, so calling code never branches.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`name` | `str` | A low-cardinality operation name, e.g. `GET /users/:id`. Variable values belong in attributes, not the name. | *required*
`kind` | `Optional[str]` | `internal` (default), `server`, `client`, `producer` or `consumer`. | `None`
`attributes` | `Optional[Mapping[str, Any]]` | Initial attributes. | `None`
`parent` | `Union[Span, str, None]` | A span handle, or an inbound W3C `traceparent` header value to continue a remote trace. Defaults to the active span. | `None`
`tracestate` | `Optional[str]` | The inbound `tracestate` header accompanying a `traceparent` string `parent`; preserved and propagated. | `None`
`start_time` | `Union[datetime, float, None]` | A `datetime` or epoch seconds, to backdate the span. | `None`

**Examples:**

```python
import posthog
posthog.traces = {"service_name": "checkout-api"}

with posthog.start_span("POST /checkout", parent=request.headers.get("traceparent")) as span:
    span.set_attribute("plan", user.plan)
    with posthog.start_span("db.query", kind="client"):
        ...
    outgoing_headers = {"traceparent": span.traceparent()}
```

### `posthog.get_active_span`

```python
get_active_span() -> Optional[Span]
```

The span that is active in the current context, or `None`. Alpha.

Only entering a span (`with posthog.start_span(...) as span:`) makes it
active; a span started manually is not. Use it to propagate the trace to
the next service: `span.traceparent()` is the header value.

**Examples:**

```python
span = posthog.get_active_span()
if span is not None:
    headers["traceparent"] = span.traceparent()
```

### `posthog.shutdown`

```python
shutdown() -> None
```

Attempt final delivery and permanently shut down the global client.

Call this for final process cleanup, not at the end of each request or
serverless invocation that shares the module-level client. Use `flush()`
before returning from those invocations instead. Shutdown closes capture
lanes, stops background workers and the feature flag poller, and tears down
integrations. The global client is not recreated automatically after shutdown.

This normally blocks until queued events have been attempted and cleanup
finishes. Failed or undrainable events may be dropped and reported through
logging or `on_error`; returning does not guarantee server receipt. Event
draining has no timeout. Queued spans get a final flush with up to 30 seconds
waiting for an in-flight flush, then a fresh 30-second drain budget; the last
HTTP request is not cut short. Unsent queued spans and still-open spans are
discarded with a warning.
Lifecycle cleanup is attempted once, and cleanup failures are logged without
retry. Calls made directly from SDK callbacks such as `on_error` are deferred
to avoid deadlocking the worker. If blocking completion is required, signal an application-owned
thread, return from the callback, and call `shutdown()` from that thread.
Do not wait inside a callback for another thread or task calling a lifecycle
method.

**Examples:**

```python
from posthog import shutdown
shutdown()
```

### `posthog.setup`

```python
setup() -> Client
```

Create or return the global PostHog client configured by module settings.

Most applications should either instantiate `Posthog` directly or set
`posthog.api_key`/other module settings before calling top-level helpers.
`setup()` is called automatically by global APIs such as `capture()`.

**Returns:**

Type | Description
---- | -----------
`Client` | The global `Client` instance. If both `api_key` and `project_api_key` are missing or blank, the client is disabled and module-level calls become no-ops.

## Types

### `OptionalCaptureArgs`

Bases: `TypedDict`

Optional arguments for the capture method.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` |  | Unique identifier for the person associated with this event. If not set, the context distinct_id is used, if available, otherwise a UUID is generated, and the event is marked as personless. Setting context-level distinct_id's is recommended. | 
`properties` |  | Dictionary of properties to track with the event | 
`timestamp` |  | When the event occurred (defaults to current time). UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. | 
`uuid` |  | Unique identifier for this specific event. If not provided, one is generated. The event UUID is returned, so you can correlate it with actions in your app (like showing users an error ID if you capture an exception). If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. | 
`groups` |  | Group identifiers to associate with this event (format: {group_type: group_key}) | 
`flags` |  | A `FeatureFlagEvaluations` snapshot from `evaluate_flags()`. The exact flag values from the snapshot are attached to the event with no additional network call — prefer this over `send_feature_flags`. | 
`send_feature_flags` |  | Deprecated — prefer `flags` with a `FeatureFlagEvaluations` snapshot. Whether to include currently active feature flags in the event properties. Can be a boolean or a SendFeatureFlagsOptions object. Defaults to False. Fires a hidden `/flags` request on capture and may return different values than the ones the code branched on. | 
`disable_geoip` |  | Whether to disable GeoIP lookup for this event. Defaults to False. | 
`options` |  | Capture options for this event, such as `{"process_person_profile": False}`. Sent as given, for PostHog to validate. An option wins over its legacy `$` property, such as `$process_person_profile`. A value that is not a dict is logged and ignored. | 

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`disable_geoip` | `NotRequired[Optional[bool]]` | 
`distinct_id` | `NotRequired[Optional[ID_TYPES]]` | 
`flags` | `NotRequired[Optional[FeatureFlagEvaluations]]` | 
`groups` | `NotRequired[Optional[Dict[str, str]]]` | 
`options` | `NotRequired[Optional[Dict[str, Any]]]` | 
`properties` | `NotRequired[Optional[Dict[str, Any]]]` | 
`send_feature_flags` | `NotRequired[Optional[Union[bool, SendFeatureFlagsOptions]]]` | 
`timestamp` | `NotRequired[Optional[Union[datetime, str]]]` | 
`uuid` | `NotRequired[Optional[Union[str, UUID]]]` | 

#### `posthog.args.OptionalCaptureArgs.disable_geoip`

```python
disable_geoip: NotRequired[Optional[bool]]
```

#### `posthog.args.OptionalCaptureArgs.distinct_id`

```python
distinct_id: NotRequired[Optional[ID_TYPES]]
```

#### `posthog.args.OptionalCaptureArgs.flags`

```python
flags: NotRequired[Optional[FeatureFlagEvaluations]]
```

#### `posthog.args.OptionalCaptureArgs.groups`

```python
groups: NotRequired[Optional[Dict[str, str]]]
```

#### `posthog.args.OptionalCaptureArgs.options`

```python
options: NotRequired[Optional[Dict[str, Any]]]
```

#### `posthog.args.OptionalCaptureArgs.properties`

```python
properties: NotRequired[Optional[Dict[str, Any]]]
```

#### `posthog.args.OptionalCaptureArgs.send_feature_flags`

```python
send_feature_flags: NotRequired[Optional[Union[bool, SendFeatureFlagsOptions]]]
```

#### `posthog.args.OptionalCaptureArgs.timestamp`

```python
timestamp: NotRequired[Optional[Union[datetime, str]]]
```

#### `posthog.args.OptionalCaptureArgs.uuid`

```python
uuid: NotRequired[Optional[Union[str, UUID]]]
```

### `FeatureFlagEvaluations`

```python
FeatureFlagEvaluations(host: _FeatureFlagEvaluationsHost, distinct_id: str, flags: Dict[str, _EvaluatedFlagRecord], groups: Optional[Mapping[str, Union[str, int]]] = None, disable_geoip: Optional[bool] = None, request_id: Optional[str] = None, evaluated_at: Optional[int] = None, errors_while_computing: bool = False, quota_limited: bool = False, minimal_flag_called_events: bool = False, accessed: Optional[Set[str]] = None, unresolved_flags: Optional[Mapping[str, UnresolvedFlagReason]] = None) -> None
```

A point-in-time snapshot of feature flag evaluations for a single distinct_id.

Returned by `Client.evaluate_flags` — branch on `is_enabled` /
`get_flag` and pass the same object to `Client.capture` via the
`flags` option so the captured event carries the exact flag values the code
branched on.

```python
flags = posthog.evaluate_flags(distinct_id, person_properties={"plan": "enterprise"})
if flags.is_enabled("new-dashboard"):
    render_new_dashboard()
posthog.capture("page_viewed", distinct_id=distinct_id, flags=flags)
```

To narrow the set of flags that get attached to a captured event, use the in-memory
helpers `only` and `only_accessed`. To narrow the set of flags requested
from the server in the first place, pass `flag_keys` to `Client.evaluate_flags`.

Reading a flag sends `$feature_flag_called`, deduplicated as `is_enabled`
describes. When the flag was evaluated remotely and its config version 2 record is
a complete experiment split (an experiment rule with a rule ID and a variant key),
the read also sends `$experiment_exposure`. The SDK sends one exposure per
distinct ID, groups, flag, rule and variant. `send_feature_flag_events=False`
suppresses both events.

**Functions:**

Name | Description
---- | -----------
`get_flag` | Return the flag value. Fires `$feature_flag_called` on first access.
`get_flag_payload` | Return the payload associated with a flag.
`is_enabled` | Return whether the flag is enabled. Fires `$feature_flag_called` on the first access per (distinct_id, flag, value) tuple, deduped via the SDK's cache.
`only` | Return a filtered copy containing only flags with the given keys. Keys that are not present in the evaluation are dropped and logged as a warning.
`only_accessed` | Return a filtered copy containing only flags accessed via `is_enabled` or `get_flag` before this call.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`keys` | `List[str]` | Return the flag keys that are part of this evaluation.
`unresolved_flags` | `Mapping[str, UnresolvedFlagReason]` | Return a read-only mapping of flag key to the reason local evaluation left it without a value.

Internal — instances are created by the SDK via `Client.evaluate_flags()`.

#### `posthog.feature_flag_evaluations.FeatureFlagEvaluations.get_flag`

```python
get_flag(key: str, *, send_feature_flag_events: bool = True) -> Optional[FlagValue]
```

Return the flag value. Fires `$feature_flag_called` on first access.

Returns the variant string for multivariate flags, `True` for enabled flags
without a variant, `False` for disabled flags, and `None` for flags that
were not returned by the evaluation.

Pass `send_feature_flag_events=False` to skip the event for this read, as
with `is_enabled`.

#### `posthog.feature_flag_evaluations.FeatureFlagEvaluations.get_flag_payload`

```python
get_flag_payload(key: str) -> Optional[Any]
```

Return the payload associated with a flag.

Does not count as an access for `only_accessed` and does not fire any event.

#### `posthog.feature_flag_evaluations.FeatureFlagEvaluations.is_enabled`

```python
is_enabled(key: str, default_value: bool = False, *, send_feature_flag_events: bool = True) -> bool
```

Return whether the flag is enabled. Fires `$feature_flag_called` on the
first access per (distinct_id, flag, value) tuple, deduped via the SDK's cache.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The feature flag key. | *required*
`default_value` | `bool` | Returned when the flag has no value in this evaluation — the key was not part of the evaluated set, or the evaluation itself came back empty (failed `/flags` request, quota limit, or no resolvable `distinct_id`). Defaults to `False`. | `False`
`send_feature_flag_events` | `bool` | Whether this read sends `$feature_flag_called`. When `False`, no event is sent and the dedupe cache is left alone, but the key still counts as accessed for `only_accessed`. | `True`

A flag that has a value always wins over `default_value`, including a
disabled flag, which returns `False` even when `default_value=True`.

#### `posthog.feature_flag_evaluations.FeatureFlagEvaluations.keys`

```python
keys: List[str]
```

Return the flag keys that are part of this evaluation.

#### `posthog.feature_flag_evaluations.FeatureFlagEvaluations.only`

```python
only(keys: List[str]) -> FeatureFlagEvaluations
```

Return a filtered copy containing only flags with the given keys. Keys that
are not present in the evaluation are dropped and logged as a warning.

#### `posthog.feature_flag_evaluations.FeatureFlagEvaluations.only_accessed`

```python
only_accessed() -> FeatureFlagEvaluations
```

Return a filtered copy containing only flags accessed via `is_enabled`
or `get_flag` before this call.

Order-dependent: if nothing has been accessed yet, the returned snapshot is
empty. The method honors its name — pre-access if you want a populated result.

#### `posthog.feature_flag_evaluations.FeatureFlagEvaluations.unresolved_flags`

```python
unresolved_flags: Mapping[str, UnresolvedFlagReason]
```

Return a read-only mapping of flag key to the reason local evaluation
left it without a value.

A flag is listed when it has a loaded local definition, was in the requested
scope, was inconclusive locally, and no remote value filled the gap. Listed
flags are not part of `keys`, and reading one reports the
`local_evaluation_inconclusive` error instead of `flag_missing`. Reading
this mapping does not count as an access and does not fire any event.
Snapshots returned by `only` and `only_accessed`, and by
`AsyncClient.evaluate_flags()`, which evaluates only remotely, carry no
entries.

### `FeatureFlagResult`

```python
FeatureFlagResult(key: str, enabled: bool, variant: Optional[str], payload: Optional[Any], reason: Optional[str]) -> None
```

The result of calling a feature flag which includes the flag result, variant, and payload.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`key` | `str` | The unique identifier of the feature flag.
`enabled` | `bool` | Whether the feature flag is enabled for the current context.
`variant` | `Optional[str]` | The variant value if the flag is enabled and has variants, None otherwise.
`payload` | `Optional[Any]` | Additional data associated with the feature flag, if any.
`reason` | `Optional[str]` | A description of why the flag was enabled or disabled, if available.

**Functions:**

Name | Description
---- | -----------
`from_flag_details` | Create a FeatureFlagResult from a FeatureFlag object.
`from_value_and_payload` | Creates a FeatureFlagResult from a flag value and payload.
`get_value` | Returns the value of the flag. This is the variant if it exists, otherwise the enabled value. This is the value we report as `$feature_flag_response` in the `$feature_flag_called` event.

#### `posthog.types.FeatureFlagResult.enabled`

```python
enabled: bool
```

#### `posthog.types.FeatureFlagResult.from_flag_details`

```python
from_flag_details(details: Union[FeatureFlag, None], override_match_value: Optional[FlagValue] = None) -> FeatureFlagResult | None
```

Create a FeatureFlagResult from a FeatureFlag object.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`details` | `Union[FeatureFlag, None]` | The FeatureFlag object to convert. | *required*
`override_match_value` | `Optional[FlagValue]` | If provided, this value will be used to populate the enabled and variant fields instead of the values from the FeatureFlag. | `None`

**Returns:**

Type | Description
---- | -----------
`FeatureFlagResult \| None` | FeatureFlagResult | None: A new FeatureFlagResult instance, or None if details is None.

#### `posthog.types.FeatureFlagResult.from_value_and_payload`

```python
from_value_and_payload(key: str, value: Union[FlagValue, None], payload: Any) -> Union[FeatureFlagResult, None]
```

Creates a FeatureFlagResult from a flag value and payload.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`key` | `str` | The unique identifier of the feature flag. | *required*
`value` | `Union[FlagValue, None]` | The value of the flag (string variant or boolean). | *required*
`payload` | `Any` | Additional data associated with the feature flag. | *required*

**Returns:**

Type | Description
---- | -----------
`Union[FeatureFlagResult, None]` | Union[FeatureFlagResult, None]: A new FeatureFlagResult instance, or None if value is None.

#### `posthog.types.FeatureFlagResult.get_value`

```python
get_value() -> FlagValue
```

Returns the value of the flag. This is the variant if it exists, otherwise the enabled value.
This is the value we report as `$feature_flag_response` in the `$feature_flag_called` event.

**Returns:**

Name | Type | Description
---- | ---- | -----------
`FlagValue` | `FlagValue` | Either a string variant or boolean value representing the flag's state.

#### `posthog.types.FeatureFlagResult.key`

```python
key: str
```

#### `posthog.types.FeatureFlagResult.payload`

```python
payload: Optional[Any]
```

#### `posthog.types.FeatureFlagResult.reason`

```python
reason: Optional[str]
```

#### `posthog.types.FeatureFlagResult.variant`

```python
variant: Optional[str]
```

### `FeatureFlag`

```python
FeatureFlag(key: str, enabled: bool, variant: Optional[str], reason: Optional[FlagReason], metadata: Union[FlagMetadata, LegacyFlagMetadata]) -> None
```

Detailed feature flag evaluation returned by the flags API.

`enabled`, `variant` and `metadata.payload` are the value rendered the way
responses before `/flags?v=4` carried it, which the legacy getters return.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`key` | `str` | Feature flag key.
`enabled` | `bool` | Whether the flag is enabled for the evaluated user or group.
`variant` | `Optional[str]` | Variant key for multivariate flags, otherwise `None`.
`reason` | `Optional[FlagReason]` | Optional reason metadata explaining the result.
`metadata` | `Union[FlagMetadata, LegacyFlagMetadata]` | Payload and other metadata returned by the API.

**Functions:**

Name | Description
---- | -----------
`from_json` | Parse one flag record of a `/flags` response.
`from_value_and_payload` | Build a flag from its value and payload. A string value is a variant, so the flag is enabled.
`get_value` | Return the variant for a multivariate flag, otherwise whether the flag is enabled.

#### `posthog.types.FeatureFlag.enabled`

```python
enabled: bool
```

#### `posthog.types.FeatureFlag.from_json`

```python
from_json(resp: Any) -> FeatureFlag
```

Parse one flag record of a `/flags` response.

A record with a `value` member is a `/flags?v=4` record. A record
without one comes from an older server: its value is `variant`, or
`enabled` when there is no variant, and its config version is `1`.

#### `posthog.types.FeatureFlag.from_value_and_payload`

```python
from_value_and_payload(key: str, value: FlagValue, payload: Any) -> FeatureFlag
```

Build a flag from its value and payload. A string value is a variant, so the flag is enabled.

#### `posthog.types.FeatureFlag.get_value`

```python
get_value() -> FlagValue
```

Return the variant for a multivariate flag, otherwise whether the flag is enabled.

#### `posthog.types.FeatureFlag.key`

```python
key: str
```

#### `posthog.types.FeatureFlag.metadata`

```python
metadata: Union[FlagMetadata, LegacyFlagMetadata]
```

#### `posthog.types.FeatureFlag.reason`

```python
reason: Optional[FlagReason]
```

#### `posthog.types.FeatureFlag.variant`

```python
variant: Optional[str]
```

### `FlagsAndPayloads`

Bases: `TypedDict`

Feature flag values and payloads keyed by feature flag key.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`featureFlags` | `Optional[dict[str, FlagValue]]` | Each flag's value, keyed by flag key.
`featureFlagPayloads` | `Optional[dict[str, Any]]` | Each flag's payload, keyed by flag key.

#### `posthog.types.FlagsAndPayloads.featureFlagPayloads`

```python
featureFlagPayloads: Optional[dict[str, Any]]
```

#### `posthog.types.FlagsAndPayloads.featureFlags`

```python
featureFlags: Optional[dict[str, FlagValue]]
```

### `UnresolvedFlagReason`

Bases: `str`, `Enum`

Why local evaluation left a flag without a value.

Reported by `FeatureFlagEvaluations.unresolved_flags`. Inheriting from
`str` keeps the members directly comparable to their string values. When more
than one cause applies, the first reason in the order below is reported.

<details class="values" open markdown="1">
<summary>Values</summary>

EXPERIENCE_CONTINUITY: The flag has experience continuity enabled, which
    local evaluation never resolves. Change the flag to fix it.
UNSUPPORTED_DEFINITION: The flag uses something the local evaluator does not
    support, such as a static cohort, an unknown operator, a malformed value,
    a dependency with no loaded definition or a circular dependency. Change
    the flag or upgrade the SDK to fix it.
UNRESOLVED_DEPENDENCY: A flag this flag depends on has a loaded definition
    but was unresolved.
MISSING_CONTEXT: The call did not pass a property, group property or device
    id the flag needs, or passed it in an unusable form. Pass it to fix it.

</details>

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`EXPERIENCE_CONTINUITY` |  | 
`MISSING_CONTEXT` |  | 
`UNRESOLVED_DEPENDENCY` |  | 
`UNSUPPORTED_DEFINITION` |  | 

#### `posthog.types.UnresolvedFlagReason.EXPERIENCE_CONTINUITY`

```python
EXPERIENCE_CONTINUITY = 'experience_continuity'
```

#### `posthog.types.UnresolvedFlagReason.MISSING_CONTEXT`

```python
MISSING_CONTEXT = 'missing_context'
```

#### `posthog.types.UnresolvedFlagReason.UNRESOLVED_DEPENDENCY`

```python
UNRESOLVED_DEPENDENCY = 'unresolved_dependency'
```

#### `posthog.types.UnresolvedFlagReason.UNSUPPORTED_DEFINITION`

```python
UNSUPPORTED_DEFINITION = 'unsupported_definition'
```

### `InconclusiveMatchError`

```python
InconclusiveMatchError(*args, reason: Optional[UnresolvedFlagReason] = None)
```

Bases: `Exception`

Raised when local evaluation can't determine a flag's value. `reason` says
why, as reported by `FeatureFlagEvaluations.unresolved_flags`.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`reason` | `UnresolvedFlagReason` | Why local evaluation couldn't resolve the flag.

#### `posthog.feature_flags.InconclusiveMatchError.reason`

```python
reason: UnresolvedFlagReason = UnresolvedFlagReason.UNSUPPORTED_DEFINITION
```

### `RequiresServerEvaluation`

```python
RequiresServerEvaluation(*args, reason: Optional[UnresolvedFlagReason] = None)
```

Bases: `Exception`

Raised when feature flag evaluation requires server-side data that is not
available locally (e.g., static cohorts).

This error should propagate immediately to trigger API fallback, unlike
InconclusiveMatchError which allows trying other conditions.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`reason` | `UnresolvedFlagReason` | Why the flag needs server-side evaluation.

#### `posthog.feature_flags.RequiresServerEvaluation.reason`

```python
reason: UnresolvedFlagReason = UnresolvedFlagReason.UNSUPPORTED_DEFINITION
```

### `CaptureError`

```python
CaptureError(status: int | str, message: str, *, endpoint: str, retry_after: Optional[float] = None, request_id: Optional[str] = None, attempts: Optional[int] = None, retry_exhausted: Optional[list[str]] = None, drops: Optional[list[tuple[str, Optional[str]]]] = None, event_results: Optional[dict[str, CaptureEventResult]] = None)
```

Bases: `APIError`

A capture batch that was not fully delivered.

Passed to `on_error` with the batch. `status` is the HTTP status of the
last attempt, or `0` when no response arrived (`__cause__` holds the
transport error). A 2xx `status` means the request succeeded but some
events were dropped or ran out of retries.

`event_results` maps each event uuid to its last verdict from a 2xx
response. Events missing from it never got a verdict, because every
attempt that reached them failed as a whole request.

**Functions:**

Name | Description
---- | -----------
`verdict_summary` | Count the undelivered events by verdict and reason, for example `drop/llm_events_over_quota=2, retry/not_persisted=1`.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`attempts` |  | 
`drops` |  | 
`endpoint` |  | 
`event_results` |  | 
`message` |  | 
`request_id` |  | 
`retry_after` |  | 
`retry_exhausted` |  | 
`status` |  | 

#### `posthog.capture_send.CaptureError.attempts`

```python
attempts = attempts
```

#### `posthog.capture_send.CaptureError.drops`

```python
drops = drops or []
```

#### `posthog.capture_send.CaptureError.endpoint`

```python
endpoint = endpoint
```

#### `posthog.capture_send.CaptureError.event_results`

```python
event_results = event_results or {}
```

#### `posthog.capture_send.CaptureError.message`

```python
message = message
```

#### `posthog.capture_send.CaptureError.request_id`

```python
request_id = request_id
```

#### `posthog.capture_send.CaptureError.retry_after`

```python
retry_after = retry_after
```

#### `posthog.capture_send.CaptureError.retry_exhausted`

```python
retry_exhausted = retry_exhausted or []
```

#### `posthog.capture_send.CaptureError.status`

```python
status = status
```

#### `posthog.capture_send.CaptureError.verdict_summary`

```python
verdict_summary() -> str
```

Count the undelivered events by verdict and reason, for example
`drop/llm_events_over_quota=2, retry/not_persisted=1`.

Counts the events in `drops` and `retry_exhausted`: events the
server dropped, and events still pending retry after the last 2xx
response. A retry verdict followed by a failed request, for example a
503 on the last attempt, is not counted; `event_results` keeps it.
Empty when no event is counted.

### `Span`

A handle to a span.

Every method is safe to call on any handle, including after `end()` and
on the inert handles returned when tracing is off, so calling code never
branches on whether tracing is running. Entering a handle (`with span:`)
makes it the active span for the block and ends it on exit, recording an
`Exception` raised inside it. A `BaseException` that is not an
`Exception` (`GeneratorExit`, `CancelledError`, `KeyboardInterrupt`)
still ends the span but is not recorded as a failure.

**Functions:**

Name | Description
---- | -----------
`add_event` | Add a timestamped event to the span. Ignored after `end()`.
`end` | End the span and queue it for export. Idempotent.
`record_exception` | Record an exception as an `exception` event and mark the span `error`.
`set_attribute` | Set one attribute on the span; last write wins. Ignored after `end()`.
`set_attributes` | Set several attributes at once; last write wins per key. Ignored after `end()`.
`set_status` | Set the span status to `"ok"` or `"error"`. Ignored after `end()`.
`traceparent` | The W3C `traceparent` header value to propagate, or `None`.
`tracestate` | The W3C `tracestate` header value to propagate, or `None`.
`update_name` | Rename the span, for a name only known after it started. Ignored after `end()`.

#### `posthog.tracing.span.Span.add_event`

```python
add_event(name: str, attributes: Optional[Mapping[str, Any]] = None, timestamp: Union[datetime, float, None] = None) -> Span
```

Add a timestamped event to the span. Ignored after `end()`.

`timestamp` defaults to now; it accepts a `datetime`, or seconds
since the epoch as a `float` or `int`. Returns the span, so calls
chain.

**Examples:**

```python
span.add_event("cache.miss", {"key": "user:42"})
```

#### `posthog.tracing.span.Span.end`

```python
end(end_time: Union[datetime, float, None] = None) -> None
```

End the span and queue it for export. Idempotent.

`end_time` defaults to now; it accepts a `datetime`, or seconds
since the epoch as a `float` or `int`, and is never earlier than
the start. Later calls, and later mutations, no-op. `with span:`
ends the span on exit, so call this only for spans started manually.

**Examples:**

```python
span = posthog.start_span("job")
try:
    run_job()
finally:
    span.end()
```

#### `posthog.tracing.span.Span.record_exception`

```python
record_exception(exception: BaseException) -> Span
```

Record an exception as an `exception` event and mark the span `error`.

Ignored after `end()`. The event carries `exception.type`,
`exception.message` and, for a raised exception,
`exception.stacktrace` (its last `max_attribute_value_length`
characters, so a chained traceback keeps the outermost exception and
may lose its cause). Returns the span, so calls chain. Inside
`with span:` a raised `Exception` is recorded automatically, so
this is for exceptions that are caught and handled.

**Examples:**

```python
try:
    charge(card)
except PaymentError as e:
    span.record_exception(e)
```

#### `posthog.tracing.span.Span.set_attribute`

```python
set_attribute(key: str, value: Any) -> Span
```

Set one attribute on the span; last write wins. Ignored after `end()`.

Returns the span, so calls chain. Prefer primitive values: strings,
booleans, integers and floats keep their type. Lists and mappings are
sent as OTLP arrays and maps, but PostHog stores them as serialized
strings. `None` removes the key; anything else is stringified.

**Examples:**

```python
span.set_attribute("http.status_code", 200).set_attribute("cache.hit", True)
```

#### `posthog.tracing.span.Span.set_attributes`

```python
set_attributes(attributes: Mapping[str, Any]) -> Span
```

Set several attributes at once; last write wins per key. Ignored after `end()`.

Returns the span, so calls chain. Same encoding as `set_attribute()`.

**Examples:**

```python
span.set_attributes({"db.system": "postgresql", "db.rows": 12})
```

#### `posthog.tracing.span.Span.set_status`

```python
set_status(code: str, message: Optional[str] = None) -> Span
```

Set the span status to `"ok"` or `"error"`. Ignored after `end()`.

Unset by default. Any other `code` is ignored. `ok` is final for the
scoped form: an `Exception` raised inside `with span:` does not override
it. Returns the span, so calls chain.

**Examples:**

```python
span.set_status("error", "upstream timed out")
```

#### `posthog.tracing.span.Span.traceparent`

```python
traceparent() -> Optional[str]
```

The W3C `traceparent` header value to propagate, or `None`.

#### `posthog.tracing.span.Span.tracestate`

```python
tracestate() -> Optional[str]
```

The W3C `tracestate` header value to propagate, or `None`.

#### `posthog.tracing.span.Span.update_name`

```python
update_name(name: str) -> Span
```

Rename the span, for a name only known after it started. Ignored after `end()`.

Returns the span, so calls chain.

**Examples:**

```python
span = posthog.start_span("http.request")
span.update_name(f"{request.method} {route.pattern}")
```

### `Emails`

```python
Emails(secret_key: Optional[str], *, host: Optional[str] = None, timeout: float = 15, max_retries: int = 2, disabled: bool = False, session: Optional[requests.Session] = None, migrated_from: Optional[str] = None) -> None
```

Bases: `_EmailsBase`

Sends transactional email from PostHog email templates or from content in your code.

`host` is your PostHog app host. PostHog Cloud ingestion hosts map to the
app host, so `us.i.posthog.com` sends to `us.posthog.com`. Any other
host is used as given.

Each send retries network errors, rate limits and an unavailable service up
to `max_retries` times, with the same idempotency key. Set
`max_retries=0` when a job runner already retries the send. `timeout`
is in seconds, per request. Pass `session` to send through your own
`requests.Session`, for example a test double. `migrated_from`, such
as `"resend"`, tells PostHog which provider your sends came from.

**Functions:**

Name | Description
---- | -----------
`close` | Close the HTTP connections this client opened. A `session` you passed in stays open.
`get` | Read what happened to an email that `send` queued.
`send` | Queue one email, from a template in PostHog or from content in your code.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`disabled` |  | 
`host` |  | 
`max_retries` |  | 
`migrated_from` |  | 
`secret_key` |  | 
`timeout` |  | 

#### `posthog.emails.Emails.close`

```python
close() -> None
```

Close the HTTP connections this client opened. A `session` you passed in stays open.

#### `posthog.emails.Emails.disabled`

```python
disabled = disabled
```

#### `posthog.emails.Emails.get`

```python
get(message_id: Union[str, UUID]) -> EmailStatus
```

Read what happened to an email that `send` queued.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`message_id` | `Union[str, UUID]` | The `id` of the `SendEmailResult` that `send` returned. | *required*

**Returns:**

Type | Description
---- | -----------
`EmailStatus` | The email's latest status, for 30 days after the send.

**Raises:**

Type | Description
---- | -----------
`EmailError` | PostHog did not answer with a status. `code` is `not_found` for an unknown ID, an email older than 30 days, or for a few seconds after the send.
`ValueError` | `message_id` is not a message ID.

**Examples:**

```python
result = emails.send(to="user@example.com", template_id="...")
status = emails.get(result.id)
if status.status == "bounced":
    print(status.reason)
```

#### `posthog.emails.Emails.host`

```python
host = determine_app_host(host)
```

#### `posthog.emails.Emails.max_retries`

```python
max_retries = max_retries
```

#### `posthog.emails.Emails.migrated_from`

```python
migrated_from = migrated_from
```

#### `posthog.emails.Emails.secret_key`

```python
secret_key = secret_key.strip() if isinstance(secret_key, str) else None
```

#### `posthog.emails.Emails.send`

```python
send(**kwargs: Any) -> SendEmailResult
```

Queue one email, from a template in PostHog or from content in your code.

Pass exactly one form. The template form names a template made in
PostHog with `template_id`. The code form names the email with
`template_key` and passes `subject` and `html`, `text` or both:
see `CodeEmailArgs`.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`to` |  | One recipient email address. | 
`template_id` |  | The UUID of a template made transactional in PostHog. | 
`template_data` |  | Values for the template's `{{ variables.* }}` placeholders. Values are encoded the way capture encodes properties. | 
`distinct_id` |  | The recipient's distinct ID in your product. The email's events go to this person. Without it, they go to a person whose distinct ID is the email address. | 
`idempotency_key` |  | 1 to 255 visible ASCII characters. A send with a key that PostHog has already accepted queues nothing and returns the first send's result. Without it, the client makes a key that covers its own retries. Pass a key derived from what the email is for, such as `welcome:user_123`, to cover retries outside this call, too. | 
`max_retries` |  | Overrides the client's maximum number of retries for this send. `0` turns retries off. | 

**Returns:**

Type | Description
---- | -----------
`SendEmailResult` | The queued email. A result means PostHog queued the email, not that it was delivered.

**Raises:**

Type | Description
---- | -----------
`EmailError` | PostHog did not queue the email, or the client could not tell whether it did.
`(TypeError, ValueError)` | An argument has the wrong type or format.

**Examples:**

```python
emails.send(
    to="user@example.com",
    template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b",
    template_data={"name": "Ada"},
    distinct_id="user_123",
    idempotency_key="welcome:user_123",
)
emails.send(
    to="user@example.com",
    template_key="password-reset",
    subject="Reset your password",
    html=rendered_html,
    distinct_id="user_123",
)
```

#### `posthog.emails.Emails.timeout`

```python
timeout = timeout
```

### `EmailError`

```python
EmailError(*, name: str, code: str, message: str, status_code: Optional[int] = None, why: Optional[str] = None, fix: Optional[str] = None, docs_url: Optional[str] = None, field: Optional[str] = None, retry_after: Optional[float] = None) -> None
```

Bases: `Exception`

PostHog did not queue the email, or the client could not tell whether it did.

Branch on `code`, PostHog's stable error code, or on `name`, a coarse
category of the failure. The message never contains the secret key or the
request body.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`name` |  | A coarse category of the failure.
`code` |  | PostHog's stable error code.
`message` |  | What went wrong. It never contains the secret key or the request body.
`status_code` |  | The HTTP status of the response, or `None` when none arrived.
`why` |  | PostHog's explanation of the failure, when it gave one.
`fix` |  | What to change to fix it, when PostHog said.
`docs_url` |  | A docs page about the error, when PostHog gave one.
`field` |  | The request field the error is about, when there is one.
`retry_after` |  | Seconds to wait before retrying, when PostHog said.

#### `posthog.emails.EmailError.code`

```python
code = code
```

#### `posthog.emails.EmailError.docs_url`

```python
docs_url = docs_url
```

#### `posthog.emails.EmailError.field`

```python
field = field
```

#### `posthog.emails.EmailError.fix`

```python
fix = fix
```

#### `posthog.emails.EmailError.message`

```python
message = message
```

#### `posthog.emails.EmailError.name`

```python
name = name
```

#### `posthog.emails.EmailError.retry_after`

```python
retry_after = retry_after
```

#### `posthog.emails.EmailError.status_code`

```python
status_code = status_code
```

#### `posthog.emails.EmailError.why`

```python
why = why
```

### `SendEmailResult`

```python
SendEmailResult(id: str, replayed: bool = False, warnings: tuple[EmailWarning, ...] = ()) -> None
```

The queued email. `id` is its message ID.

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`id` | `str` | The email's message ID.
`replayed` | `bool` | `True` when the response says PostHog replayed an earlier send for the same `idempotency_key` instead of sending again.
`warnings` | `tuple[EmailWarning, ...]` | Warnings PostHog returned about the request.

#### `posthog.emails.SendEmailResult.id`

```python
id: str
```

#### `posthog.emails.SendEmailResult.replayed`

```python
replayed: bool = False
```

#### `posthog.emails.SendEmailResult.warnings`

```python
warnings: tuple[EmailWarning, ...] = ()
```

### `OptionalSetArgs`

Bases: `TypedDict`

Optional arguments for the set method.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`distinct_id` |  | Unique identifier for the user to set properties on. If not set, the context distinct_id is used, if available, otherwise this function does nothing. Setting context-level distinct_id's is recommended. | 
`properties` |  | Dictionary of properties to set on the person | 
`timestamp` |  | When the properties were set (defaults to current time). UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. | 
`uuid` |  | Unique identifier for this operation. If not provided, one is generated. This UUID is returned, so you can correlate it with actions in your app. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. | 
`disable_geoip` |  | Whether to disable GeoIP lookup for this operation. Defaults to False. | 
`options` |  | Capture options for this event, sent as given. See `OptionalCaptureArgs`. | 

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`disable_geoip` | `NotRequired[Optional[bool]]` | 
`distinct_id` | `NotRequired[Optional[ID_TYPES]]` | 
`options` | `NotRequired[Optional[Dict[str, Any]]]` | 
`properties` | `NotRequired[Optional[Dict[str, Any]]]` | 
`timestamp` | `NotRequired[Optional[Union[datetime, str]]]` | 
`uuid` | `NotRequired[Optional[Union[str, UUID]]]` | 

#### `posthog.args.OptionalSetArgs.disable_geoip`

```python
disable_geoip: NotRequired[Optional[bool]]
```

#### `posthog.args.OptionalSetArgs.distinct_id`

```python
distinct_id: NotRequired[Optional[ID_TYPES]]
```

#### `posthog.args.OptionalSetArgs.options`

```python
options: NotRequired[Optional[Dict[str, Any]]]
```

#### `posthog.args.OptionalSetArgs.properties`

```python
properties: NotRequired[Optional[Dict[str, Any]]]
```

#### `posthog.args.OptionalSetArgs.timestamp`

```python
timestamp: NotRequired[Optional[Union[datetime, str]]]
```

#### `posthog.args.OptionalSetArgs.uuid`

```python
uuid: NotRequired[Optional[Union[str, UUID]]]
```

### `CodeEmailArgs`

Bases: `_RecipientArgs`

A send of email content that your code rendered for one recipient, named by a key.

The content goes out as is. To send a React email, render it to HTML in
your app first.

An optional field passed as `None` counts as left out.

**Parameters:**

Name | Type | Description | Default
---- | ---- | ----------- | -------
`to` |  | One recipient email address. | 
`template_key` |  | Names the email, so its metrics, logs and status group under it. Letters, digits, `-` and `_`, at most 400. | 
`subject` |  | The subject line. | 
`html` |  | The HTML body. Give `html`, `text` or both. | 
`text` |  | The plain-text body. Without it, PostHog derives it from `html`. HTML with no text in it, such as a single image, goes out without a plain-text part. | 
`from_` |  | A verified sender, such as `"Acme <hello@example.com>"`. | 
`reply_to` |  | One address or a list of addresses. An empty list sends no reply-to. | 
`capture_engagement` |  | Capture the email's opens and clicks as events of the recipient. Defaults to `True`. A project whose admin turned engagement capture off captures nothing, whatever the send asks. A template made in PostHog follows the project's setting, so the template form doesn't take it. | 
`distinct_id` |  | The recipient's distinct ID in your product. | 
`idempotency_key` |  | 1 to 255 visible ASCII characters. Template sends and code sends share one set of keys: a key already used for another email, or for a `template_id` send, raises `idempotency_key_reused`. A retry with the same key returns the first send's result, also when its workflow was paused since. | 
`max_retries` |  | Overrides the client's maximum number of retries for this send. `0` turns retries off. | 

**Attributes:**

Name | Type | Description
---- | ---- | -----------
`capture_engagement` | `NotRequired[Optional[bool]]` | 
`distinct_id` | `NotRequired[Optional[str]]` | 
`from_` | `NotRequired[Optional[str]]` | 
`html` | `NotRequired[Optional[str]]` | 
`idempotency_key` | `NotRequired[Optional[str]]` | 
`max_retries` | `NotRequired[Optional[int]]` | 
`reply_to` | `NotRequired[Optional[Union[str, Sequence[str]]]]` | 
`subject` | `str` | 
`template_key` | `str` | 
`text` | `NotRequired[Optional[str]]` | 
`to` | `str` | 

#### `posthog.emails.CodeEmailArgs.capture_engagement`

```python
capture_engagement: NotRequired[Optional[bool]]
```

#### `posthog.emails.CodeEmailArgs.distinct_id`

```python
distinct_id: NotRequired[Optional[str]]
```

#### `posthog.emails.CodeEmailArgs.from_`

```python
from_: NotRequired[Optional[str]]
```

#### `posthog.emails.CodeEmailArgs.html`

```python
html: NotRequired[Optional[str]]
```

#### `posthog.emails.CodeEmailArgs.idempotency_key`

```python
idempotency_key: NotRequired[Optional[str]]
```

#### `posthog.emails.CodeEmailArgs.max_retries`

```python
max_retries: NotRequired[Optional[int]]
```

#### `posthog.emails.CodeEmailArgs.reply_to`

```python
reply_to: NotRequired[Optional[Union[str, Sequence[str]]]]
```

#### `posthog.emails.CodeEmailArgs.subject`

```python
subject: str
```

#### `posthog.emails.CodeEmailArgs.template_key`

```python
template_key: str
```

#### `posthog.emails.CodeEmailArgs.text`

```python
text: NotRequired[Optional[str]]
```

#### `posthog.emails.CodeEmailArgs.to`

```python
to: str
```
