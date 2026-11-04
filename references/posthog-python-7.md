---
title: "PostHog Python SDK 7.x"
sidebar: Docs
showTitle: true
contentMaxWidthClass: max-w-full
platformLogo: python
tableOfContents:
  - value: "Getting started"
    url: getting-started
    depth: 0
  - value: "Traces"
    url: traces
    depth: 0
  - value: "Alias"
    url: alias
    depth: 0
  - value: "Capture"
    url: capture
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
  - value: "Identify"
    url: identify
    depth: 0
  - value: "Is feature enabled"
    url: is-feature-enabled
    depth: 0
  - value: "Setup"
    url: setup
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
  - value: "Configs"
    url: configs
    depth: 0
  - value: "Types"
    url: types
    depth: 0
---

## Getting started

The Python SDK captures events, evaluates feature flags and reports errors from Python 3.10 or later.

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

## Traces

### `Client.start_span`

Start a span for distributed tracing. Alpha.

Returns a span handle. Use it as a context manager to make it the active
span for the block and end it on exit (recording a raised exception on
the way out); or call `end()` yourself for a span that cannot wrap a
block. Spans started inside the block nest under it automatically.
Always returns a usable handle, even when tracing is off, so calling
code never branches.

```python
start_span(
    name: str,
    kind: Optional[str] = None,
    attributes: Optional[Mapping[str, Any]] = None,
    parent: Union[Span, str, None] = None,
    tracestate: Optional[str] = None,
    start_time: Union[datetime, float, None] = None,
) -> Span
```

<details>
<summary>Parameters (6)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required**<br />A low-cardinality operation name, e.g. `GET /users/:id`. Variable values belong in attributes, not the name. |
| `kind`<br />**Type:** `Optional[str]` · **Default:** `None`<br />`internal` (default), `server`, `client`, `producer` or `consumer`. |
| `attributes`<br />**Type:** `Optional[Mapping[str, Any]]` · **Default:** `None`<br />Initial attributes. |
| `parent`<br />**Type:** `Union[Span, str, None]` · **Default:** `None`<br />A span handle, or an inbound W3C `traceparent` header value to continue a remote trace. Defaults to the active span. A forked child starts with no active span; pass the parent span to continue a trace across a fork. |
| `tracestate`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The inbound `tracestate` header accompanying a `traceparent` string `parent`; preserved and propagated. |
| `start_time`<br />**Type:** `Union[datetime, float, None]` · **Default:** `None`<br />A `datetime` or epoch seconds, to backdate the span. |

</details>

**Returns** `Span`

**Correct example.**

```python
posthog = Posthog("<ph_project_api_key>", traces={"service_name": "checkout-api"})

with posthog.start_span("POST /checkout", parent=request.headers.get("traceparent")) as span:
    span.set_attribute("plan", user.plan)
    with posthog.start_span("db.query", kind="client"):
        ...
    outgoing_headers = {"traceparent": span.traceparent()}
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L2794)

### `Client.get_active_span`

The span that is active in the current context, or `None`. Alpha.

Only entering a span (`with posthog.start_span(...) as span:`) makes
it active; a span started manually is not. Use it to propagate the
trace to the next service: `span.traceparent()` is the header value.

```python
get_active_span() -> Optional[Span]
```

**Returns** `Optional[Span]`

**Correct example.**

```python
span = posthog.get_active_span()
if span is not None:
    headers["traceparent"] = span.traceparent()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L2853)

## Alias

### `Client.alias`

Create an alias between two distinct IDs.

Note: This method will not raise exceptions. Errors are logged.

```python
alias(
    previous_id: ID_TYPES,
    distinct_id: Optional[str],
    timestamp: Optional[Union[datetime, str]] = None,
    uuid: Optional[str] = None,
    disable_geoip: Optional[bool] = None,
) -> Optional[str]
```

<details>
<summary>Parameters (5)</summary>

| Parameter |
| --- |
| `previous_id`<br />**Type:** `ID_TYPES` · **Required**<br />The previous distinct ID. Required - the call is dropped with a warning if it is missing or empty. |
| `distinct_id`<br />**Type:** `Optional[str]` · **Required**<br />The new distinct ID to alias to. Falls back to the context distinct ID; the call is dropped with a warning if neither is available. |
| `timestamp`<br />**Type:** `Optional[Union[datetime, str]]` · **Default:** `None`<br />The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. |
| `uuid`<br />**Type:** `Optional[str]` · **Default:** `None`<br />A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this event. |

</details>

**Returns** `Optional[str]`

**Correct example.**

```python
posthog.alias(previous_id='distinct_id', distinct_id='alias_id')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L2168)

## Capture

### `Client.capture`

Captures an event manually. [Learn about capture best practices](https://posthog.com/docs/product-analytics/capture-events)

```python
capture(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

<details>
<summary>Parameters (9)</summary>

| Parameter |
| --- |
| `event`<br />**Type:** `str` · **Required**<br />The event name to capture. |
| `distinct_id`<br />**Type:** `NotRequired[Optional[ID_TYPES]]`<br />The distinct ID of the user. |
| `properties`<br />**Type:** `NotRequired[Optional[Dict[str, Any]]]`<br />A dictionary of properties to include with the event. |
| `timestamp`<br />**Type:** `NotRequired[Optional[Union[datetime, str]]]`<br />The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. |
| `uuid`<br />**Type:** `NotRequired[Optional[Union[str, UUID]]]`<br />A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. |
| `groups`<br />**Type:** `NotRequired[Optional[Dict[str, str]]]`<br />A dictionary of group information. |
| `flags`<br />**Type:** `NotRequired[Optional[FeatureFlagEvaluations]]`<br />A FeatureFlagEvaluations snapshot from evaluate_flags(). The exact values from the snapshot are attached with no extra /flags request. |
| `send_feature_flags`<br />**Type:** `NotRequired[Optional[Union[bool, SendFeatureFlagsOptions]]]`<br />Deprecated. Prefer flags=... from evaluate_flags(). When truthy, evaluates flags during capture and attaches them to the event. |
| `disable_geoip`<br />**Type:** `NotRequired[Optional[bool]]`<br />Whether to disable GeoIP for this event. |

</details>

**Returns** `Optional[str]`

**Correct example.**

```python
# Anonymous event
posthog.capture('some-anon-event')
```

**Correct example.**

```python
# Context usage
from posthog import identify_context, new_context
with new_context():
    identify_context('distinct_id_of_the_user')
    posthog.capture('user_signed_up')
    posthog.capture('user_logged_in')
    posthog.capture('some-custom-action', distinct_id='distinct_id_of_the_user')
```

**Correct example.**

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

**Correct example.**

```python
# Page view event
posthog.capture('$pageview', distinct_id="distinct_id_of_the_user", properties={'$current_url': 'https://example.com'})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1738)

## Capture AI

### `Client.capture_ai`

Capture an AI event on the dedicated AI capture endpoint.

Beta: the signature is stable; operational limits (per-event size
cap, batching, endpoint) may change without notice.

Takes the same arguments and returns the same value as `capture()`:
the event UUID, or None when the event was not admitted (disabled
client, or dropped by `before_send`). The event is queued on an
isolated AI lane with its own consumer pool and a higher per-event
size cap, posting to the dedicated AI ingestion endpoint. The payload
is sent as given — no redaction or truncation is applied here.

```python
capture_ai(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `event`<br />**Type:** `str` · **Required** |

</details>

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1797)

## Capture exception

### `Client.capture_exception`

Capture an exception for error tracking.

When OpenTelemetry is installed and a valid span is active, its trace and
span IDs are added as `$trace_id` and `$span_id` event properties.

```python
capture_exception(
    exception: Optional[ExceptionArg],
    **kwargs: Unpack[OptionalCaptureArgs],
) -> Optional[str]
```

<details>
<summary>Parameters (6)</summary>

| Parameter |
| --- |
| `exception`<br />**Type:** `Optional[ExceptionArg]` · **Required**<br />The exception to capture. |
| `distinct_id`<br />**Type:** `NotRequired[Optional[ID_TYPES]]`<br />The distinct ID of the user. |
| `properties`<br />**Type:** `NotRequired[Optional[Dict[str, Any]]]`<br />A dictionary of additional properties. Overriding reserved exception properties is deprecated and will stop working in the next major version. |
| `flags`<br />**Type:** `NotRequired[Optional[FeatureFlagEvaluations]]`<br />A `FeatureFlagEvaluations` snapshot from `evaluate_flags()`. Attaches those exact flag values to the captured `$exception` event. |
| `send_feature_flags`<br />**Type:** `NotRequired[Optional[Union[bool, SendFeatureFlagsOptions]]]`<br />Deprecated. Pass `flags` from `evaluate_flags()` instead. |
| `disable_geoip`<br />**Type:** `NotRequired[Optional[bool]]`<br />Whether to disable GeoIP for this event. |

</details>

**Returns** `Optional[str]`

**Correct example.**

```python
try:
    # Some code that might fail
    pass
except Exception as e:
    posthog.capture_exception(e, 'user_distinct_id', properties=additional_properties)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L2234)

## Emails

### `Client.emails`

Send transactional email from PostHog email templates, and read the
status of each email sent with `get`.

Needs `secret_key`, a project secret API key with the `hog_flow:write`
scope. Sends go to `app_host`, or to the PostHog Cloud app host of
`host`. `email_request_timeout_seconds` and `email_request_max_retries`
configure each send. A disabled or shut-down client sends nothing. See
`posthog.emails` for setup.

```python
Client.emails: Emails
```

**Correct example.**

```python
client.emails.send(
    to="user@example.com",
    template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b",
    template_data={"name": "Ada"},
    distinct_id="user_123",
)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1256)

## Flush

### `Client.flush`

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

```python
flush(timeout_seconds: Optional[float] = 10) -> None
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `timeout_seconds`<br />**Type:** `Optional[float]` · **Default:** `10`<br />Seconds to wait for event queues to drain, shared across event lanes. Defaults to 10 seconds. Pass `None` to wait without a time limit; this does not guarantee successful delivery. Queued spans flush concurrently. The span exporter waits up to this many seconds for an in-flight flush, then uses a fresh drain budget once it acquires the lock. If the lock wait times out, the in-flight flush is left to finish. At least one span request is attempted after acquiring the lock, even with no budget left; retriable failures are retried after backoff while budget remains, with one last attempt at the deadline. Each span request is bounded by the client's `timeout` in seconds, and the last request is not cut short. This is therefore not a strict wall-clock limit for the whole call. With `None`, a retriable span failure is left for a later flush rather than retried within this call. |

</details>

**Returns** `None`

**Correct example.**

```python
posthog.capture('event_name', distinct_id='user_id')
# Before a serverless invocation returns; keep the shared client open.
posthog.flush(timeout_seconds=10)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L2872)

## Get feature flag

### `Client.get_feature_flag`

Get multivariate feature flag value for a user.

```python
get_feature_flag(
    key: str,
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[FlagValue]
```

<details>
<summary>Parameters (9)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key. |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The distinct ID of the user. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to only evaluate locally. |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `True`<br />Whether to send feature flag events. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `Optional[FlagValue]`

**Correct example.**

```python
enabled_variant = posthog.get_feature_flag('flag-key', 'distinct_id_of_your_user')
if enabled_variant == 'variant-key': # replace 'variant-key' with the key of your variant
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L4324)

## Get feature flag payload

### `Client.get_feature_flag_payload`

Get the payload for a feature flag.

```python
get_feature_flag_payload(
    key: str,
    distinct_id: ID_TYPES,
    match_value: Optional[FlagValue] = None,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = False,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[object]
```

<details>
<summary>Parameters (10)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key. |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The distinct ID of the user. |
| `match_value`<br />**Type:** `Optional[FlagValue]` · **Default:** `None`<br />The specific flag value to get payload for. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to only evaluate locally. |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `False`<br />Deprecated. Use get_feature_flag() instead if you need events. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `Optional[object]`. The payload associated with the matched feature flag value, or None. This method returns the payload only, not the FeatureFlagResult wrapper used internally to compute it.

**Correct example.**

```python
is_my_flag_enabled = posthog.feature_enabled('flag-key', 'distinct_id_of_your_user')

if is_my_flag_enabled:
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L4424)

## Get feature flag result

### `Client.get_feature_flag_result`

Get a FeatureFlagResult object which contains the flag result and payload for a key by evaluating locally or remotely
depending on whether local evaluation is enabled and the flag can be locally evaluated.
This also captures the `$feature_flag_called` event unless `send_feature_flag_events` is `False`.

```python
get_feature_flag_result(
    key: str,
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[FeatureFlagResult]
```

<details>
<summary>Parameters (9)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key. |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The distinct ID of the user. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to only evaluate locally. |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `True`<br />Whether to send feature flag events. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `Optional[FeatureFlagResult]`. Optional[FeatureFlagResult]: The feature flag result or None if disabled/not found.

**Correct example.**

```python
flag_result = posthog.get_feature_flag_result('flag-key', 'distinct_id_of_your_user')
if flag_result and flag_result.get_value() == 'variant-key':
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = flag_result.payload
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L4269)

## Get feature flags

### `Client.get_feature_variants`

Get feature flag variants for a user.

```python
get_feature_variants(
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    disable_geoip: Optional[bool] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
    device_id: Optional[str] = None,
) -> dict[str, Union[bool, str]]
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The distinct ID of the user. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `flag_keys_to_evaluate`<br />**Type:** `Optional[list[str]]` · **Default:** `None`<br />A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `dict[str, Union[bool, str]]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1499)

### `Client.get_all_flags`

Get all feature flags for a user.

```python
get_all_flags(
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    disable_geoip: Optional[bool] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
    device_id: Optional[str] = None,
) -> Optional[dict[str, Union[bool, str]]]
```

<details>
<summary>Parameters (8)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The distinct ID of the user. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to only evaluate locally. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `flag_keys_to_evaluate`<br />**Type:** `Optional[list[str]]` · **Default:** `None`<br />A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `Optional[dict[str, Union[bool, str]]]`

**Correct example.**

```python
posthog.get_all_flags('distinct_id_of_your_user')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L4744)

## Get feature flags and payloads

### `Client.get_feature_flags_and_payloads`

Get feature flags and payloads for a user.

```python
get_feature_flags_and_payloads(
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    disable_geoip: Optional[bool] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
    device_id: Optional[str] = None,
) -> FlagsAndPayloads
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The distinct ID of the user. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `flag_keys_to_evaluate`<br />**Type:** `Optional[list[str]]` · **Default:** `None`<br />A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `FlagsAndPayloads`

**Correct example.**

```python
result = posthog.get_feature_flags_and_payloads('<distinct_id>')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1580)

### `Client.get_all_flags_and_payloads`

Get all feature flags and their payloads for a user.

```python
get_all_flags_and_payloads(
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    disable_geoip: Optional[bool] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
    device_id: Optional[str] = None,
) -> FlagsAndPayloads
```

<details>
<summary>Parameters (8)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The distinct ID of the user. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to only evaluate locally. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `flag_keys_to_evaluate`<br />**Type:** `Optional[list[str]]` · **Default:** `None`<br />A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `FlagsAndPayloads`

**Correct example.**

```python
posthog.get_all_flags_and_payloads('distinct_id_of_your_user')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L4790)

## Evaluate flags

### `Client.evaluate_flags`

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

```python
evaluate_flags(
    distinct_id: Optional[ID_TYPES] = None,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    disable_geoip: Optional[bool] = None,
    flag_keys: Optional[List[str]] = None,
    device_id: Optional[str] = None,
) -> FeatureFlagEvaluations
```

<details>
<summary>Parameters (8)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `Optional[ID_TYPES]` · **Default:** `None`<br />The user's distinct ID. If `None`, falls back to the context distinct_id. If still unresolvable, returns an empty snapshot. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />Mapping of group type to group key. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />Person properties to use for evaluation. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />Group properties keyed by group type. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />If True, never fall back to remote evaluation — flags that can't be evaluated locally are omitted from the snapshot and listed with a reason in its `unresolved_flags`. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup. |
| `flag_keys`<br />**Type:** `Optional[List[str]]` · **Default:** `None`<br />Optional list that scopes local evaluation, the underlying `/flags` request, and the returned snapshot. When omitted or `None`, all flags are evaluated. An empty list returns an empty snapshot without evaluating flags. A requested key absent from loaded local definitions is included in one remote fallback per `evaluate_flags` call unless `only_evaluate_locally` is True. If the server also does not know the key, it is omitted from the snapshot. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional device ID override. If not provided, falls back to the context device_id (which may be set via tracing headers). Used by experience-continuity flags to match users across distinct_id changes. |

</details>

**Returns** `FeatureFlagEvaluations`. class:`FeatureFlagEvaluations` snapshot.

**Correct example.**

```python
flags = posthog.evaluate_flags(
    "user_123",
    person_properties={"plan": "enterprise"},
)
if flags.is_enabled("new-dashboard"):
    render_new_dashboard()
posthog.capture("page_viewed", distinct_id="user_123", flags=flags)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L4875)

## Group identify

### `Client.group_identify`

Identify a group and set its properties.

Note: This method will not raise exceptions. Errors are logged.

```python
group_identify(
    group_type: str,
    group_key: str,
    properties: Optional[Dict[str, Any]] = None,
    timestamp: Optional[Union[datetime, str]] = None,
    uuid: Optional[Union[str, UUID]] = None,
    disable_geoip: Optional[bool] = None,
    distinct_id: Optional[ID_TYPES] = None,
) -> Optional[str]
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `group_type`<br />**Type:** `str` · **Required**<br />The type of group (e.g., 'company', 'team'). Required - the call is dropped with a warning if it is missing or empty. |
| `group_key`<br />**Type:** `str` · **Required**<br />The unique identifier for the group. Required - the call is dropped with a warning if it is missing or empty. |
| `properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of properties to set on the group. |
| `timestamp`<br />**Type:** `Optional[Union[datetime, str]]` · **Default:** `None`<br />The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. |
| `uuid`<br />**Type:** `Optional[Union[str, UUID]]` · **Default:** `None`<br />A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this event. |
| `distinct_id`<br />**Type:** `Optional[ID_TYPES]` · **Default:** `None`<br />The distinct ID of the user performing the action. |

</details>

**Returns** `Optional[str]`

**Correct example.**

```python
posthog.group_identify('company', 'company_id_in_your_db', {
    'name': 'Awesome Inc.',
    'employees': 11
})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L2093)

## Identify

### `Client.set`

Set properties on a person profile.

Note: This method will not raise exceptions. Errors are logged.

```python
set(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

<details>
<summary>Parameters (5)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `NotRequired[Optional[ID_TYPES]]`<br />The distinct ID of the user. |
| `properties`<br />**Type:** `NotRequired[Optional[Dict[str, Any]]]`<br />A dictionary of properties to set. |
| `timestamp`<br />**Type:** `NotRequired[Optional[Union[datetime, str]]]`<br />The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. |
| `uuid`<br />**Type:** `NotRequired[Optional[Union[str, UUID]]]`<br />A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. |
| `disable_geoip`<br />**Type:** `NotRequired[Optional[bool]]`<br />Whether to disable GeoIP for this event. |

</details>

**Returns** `Optional[str]`

**Correct example.**

```python
# Set with distinct id
posthog.set(distinct_id='user123', properties={'name': 'Max Hedgehog'})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1995)

### `Client.set_once`

Set properties on a person profile only if they haven't been set before.

Note: This method will not raise exceptions. Errors are logged.

```python
set_once(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

<details>
<summary>Parameters (5)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `NotRequired[Optional[ID_TYPES]]`<br />The distinct ID of the user. |
| `properties`<br />**Type:** `NotRequired[Optional[Dict[str, Any]]]`<br />A dictionary of properties to set once. |
| `timestamp`<br />**Type:** `NotRequired[Optional[Union[datetime, str]]]`<br />The timestamp of the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. |
| `uuid`<br />**Type:** `NotRequired[Optional[Union[str, UUID]]]`<br />A unique identifier for the event. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. |
| `disable_geoip`<br />**Type:** `NotRequired[Optional[bool]]`<br />Whether to disable GeoIP for this event. |

</details>

**Returns** `Optional[str]`

**Correct example.**

```python
posthog.set_once(distinct_id='user123', properties={'initial_signup_date': '2024-01-01'})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L2045)

## Is feature enabled

### `Client.feature_enabled`

Check if a feature flag is enabled for a user.

```python
feature_enabled(
    key: str,
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[bool]
```

<details>
<summary>Parameters (9)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key. |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The distinct ID of the user. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to only evaluate locally. |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `True`<br />Whether to send feature flag events. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `Optional[bool]`

**Correct example.**

```python
is_my_flag_enabled = posthog.feature_enabled('flag-key', 'distinct_id_of_your_user')
if is_my_flag_enabled:
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L3982)

## Setup

### `Client(...)`

Initialize a new PostHog client instance.

```python
Client(
    project_api_key: str,
    host = None,
    debug = False,
    max_queue_size = 10000,
    send = True,
    on_error = None,
    flush_at = 100,
    flush_interval = 5.0,
    gzip = False,
    max_retries = 3,
    sync_mode = False,
    timeout: float = 15,
    thread = 1,
    poll_interval = 30,
    personal_api_key = None,
    disabled = False,
    disable_geoip = True,
    is_server = True,
    historical_migration = False,
    feature_flags_request_timeout_seconds: float = 3,
    feature_flags_request_max_retries = 1,
    super_properties = None,
    enable_exception_autocapture = False,
    log_captured_exceptions = False,
    project_root = None,
    privacy_mode = False,
    before_send = None,
    flag_fallback_cache_url = None,
    enable_local_evaluation = True,
    flag_definition_cache_provider: Optional[FlagDefinitionCacheProvider] = None,
    capture_exception_code_variables = False,
    code_variables_mask_patterns = None,
    code_variables_ignore_patterns = None,
    code_variables_mask_url_credentials = None,
    code_variables_detect_secrets = None,
    in_app_modules: list[str] | None = None,
    enable_exception_autocapture_rate_limiting = False,
    exception_autocapture_bucket_size = ExceptionCapture.DEFAULT_BUCKET_SIZE,
    exception_autocapture_refill_rate = ExceptionCapture.DEFAULT_REFILL_RATE,
    exception_autocapture_refill_interval_seconds = ExceptionCapture.DEFAULT_REFILL_INTERVAL_SECONDS,
    capture_mode: Optional[Union[CaptureMode, str]] = None,
    capture_compression: Optional[Union[CaptureCompression, str]] = None,
    secret_key = None,
    metrics: Optional[dict] = None,
    enable_full_ai_capture = False,
    capture_trace_context = False,
    traces: Optional[dict] = None,
    app_host: Optional[str] = None,
    email_request_timeout_seconds: float = 15,
    email_request_max_retries: int = 2,
)
```

<details>
<summary>Parameters (50)</summary>

| Parameter |
| --- |
| `project_api_key`<br />**Type:** `str` · **Required**<br />PostHog project API key/token. |
| `host`<br />**Default:** `None`<br />PostHog host. Defaults to the US ingestion endpoint when not set. App hosts such as `https://us.posthog.com` are mapped to the corresponding ingestion host. |
| `debug`<br />**Default:** `False`<br />Enable verbose SDK logging and re-raise errors from public API methods. |
| `max_queue_size`<br />**Default:** `10000`<br />Maximum number of events buffered before upload. |
| `send`<br />**Default:** `True`<br />If False, queueing succeeds but events are not sent. |
| `on_error`<br />**Default:** `None`<br />Optional callback invoked by background consumers when an upload fails. Keep it short and non-blocking. Calling lifecycle methods directly is safe and deferred, but do not start another thread or task that calls `flush()`, `join()`, or `shutdown()` and then wait for it from the callback. |
| `flush_at`<br />**Default:** `100`<br />Number of queued events that triggers a batch upload. |
| `flush_interval`<br />**Default:** `5.0`<br />Maximum seconds a background consumer waits before flushing a partial batch. |
| `gzip`<br />**Default:** `False`<br />Whether to gzip event upload payloads. |
| `max_retries`<br />**Default:** `3`<br />Number of upload retries. Values below 0 are treated as 0. |
| `sync_mode`<br />**Default:** `False`<br />If True, send each event synchronously instead of using background worker threads. This blocks the calling thread; in asyncio applications such as FastAPI, use `AsyncPosthog` instead. |
| `timeout`<br />**Type:** `float` · **Default:** `15`<br />HTTP request timeout in seconds for event uploads. |
| `thread`<br />**Default:** `1`<br />Number of background consumer threads. |
| `poll_interval`<br />**Default:** `30`<br />Seconds between local feature flag definition refreshes. |
| `personal_api_key`<br />**Default:** `None`<br />Deprecated alias for `secret_key`. Still honored for backwards compatibility; prefer `secret_key`, which also accepts a Project Secret API Key. |
| `disabled`<br />**Default:** `False`<br />If True, disable captures and API requests. Useful in tests. |
| `disable_geoip`<br />**Default:** `True`<br />Whether to disable server-side GeoIP enrichment. Defaults to True. |
| `is_server`<br />**Default:** `True`<br />Whether events are emitted from a server-side runtime. Defaults to True; set to False when using the SDK as a client/CLI so the device OS is attributed to the person normally. |
| `historical_migration`<br />**Default:** `False`<br />Mark events as historical migration imports. |
| `feature_flags_request_timeout_seconds`<br />**Type:** `float` · **Default:** `3`<br />Timeout in seconds for feature flag and remote config requests. |
| `feature_flags_request_max_retries`<br />**Default:** `1`<br />Number of retries for feature flag requests after network, transport, or timeout failures. Defaults to 1. Set to 0 to disable retries. |
| `super_properties`<br />**Default:** `None`<br />Properties merged into every captured event. |
| `enable_exception_autocapture`<br />**Default:** `False`<br />Automatically capture uncaught exceptions. |
| `log_captured_exceptions`<br />**Default:** `False`<br />Also log exceptions captured by error tracking. |
| `project_root`<br />**Default:** `None`<br />Root path used to determine in-app stack frames for captured exceptions. Defaults to the current working directory. |
| `privacy_mode`<br />**Default:** `False`<br />For AI observability, capture usage metadata without prompt inputs or outputs. |
| `before_send`<br />**Default:** `None`<br />Optional callback that can modify or drop events before upload. Return `None` to drop an event. |
| `flag_fallback_cache_url`<br />**Default:** `None`<br />Optional feature flag fallback cache URL, such as `memory://local/?ttl=300&size=10000` or a Redis URL. |
| `enable_local_evaluation`<br />**Default:** `True`<br />Whether to poll feature flag definitions for local evaluation when a personal API key is configured. |
| `flag_definition_cache_provider`<br />**Type:** `Optional[FlagDefinitionCacheProvider]` · **Default:** `None`<br />Optional external cache provider for sharing feature flag definitions across workers. |
| `capture_exception_code_variables`<br />**Default:** `False`<br />Capture local variable values on exception stack frames. |
| `code_variables_mask_patterns`<br />**Default:** `None`<br />Variable-name patterns to mask when capturing code variables. |
| `code_variables_ignore_patterns`<br />**Default:** `None`<br />Variable-name patterns to omit when capturing code variables. |
| `code_variables_mask_url_credentials`<br />**Default:** `None`<br />Scrub credentials embedded in URLs/DSNs (e.g. `user:pass@host`) from captured code variables, regardless of the surrounding variable name. Defaults to True. |
| `code_variables_detect_secrets`<br />**Default:** `None`<br />Last-resort entropy-based detection that redacts high-entropy secret-looking values (API keys, tokens, strong passwords) sitting in innocuously-named variables, after the name and URL checks. Skips structured ids (UUIDs, ObjectIds, hashes). Defaults to True. |
| `in_app_modules`<br />**Type:** `list[str]` \| `None` · **Default:** `None`<br />Module/package prefixes treated as in-app frames in captured exceptions. |
| `enable_exception_autocapture_rate_limiting`<br />**Default:** `False`<br />Rate limit autocaptured exceptions client-side with a token bucket per exception type. Disabled by default. |
| `exception_autocapture_bucket_size`<br />**Default:** `ExceptionCapture.DEFAULT_BUCKET_SIZE`<br />Maximum burst of autocaptured exceptions allowed per exception type (token bucket size, clamped to 0-100). |
| `exception_autocapture_refill_rate`<br />**Default:** `ExceptionCapture.DEFAULT_REFILL_RATE`<br />Tokens restored per refill interval for each exception type's bucket. |
| `exception_autocapture_refill_interval_seconds`<br />**Default:** `ExceptionCapture.DEFAULT_REFILL_INTERVAL_SECONDS`<br />Seconds between token refills for autocaptured exception rate limiting. |
| `capture_mode`<br />**Type:** `Optional[Union[CaptureMode, str]]` · **Default:** `None`<br />Capture wire protocol to use. Defaults to `CaptureMode.V0` (legacy `/batch/`). Set `CaptureMode.V1` (or pass the string `"v1"`) to opt into `/i/v1/analytics/events`. When omitted, the `POSTHOG_CAPTURE_MODE` env var is consulted, then `V0`. |
| `capture_compression`<br />**Type:** `Optional[Union[CaptureCompression, str]]` · **Default:** `None`<br />Request-body compression for capture-v1 uploads (ignored in V0, which uses `gzip`). `CaptureCompression.GZIP` or `DEFLATE` (or the strings `"gzip"`/`"deflate"`). When omitted, the `POSTHOG_CAPTURE_COMPRESSION` env var is consulted, then the legacy `gzip` flag, then no compression. |
| `secret_key`<br />**Default:** `None`<br />A Personal API Key or Project Secret API Key, used to authenticate local feature flag evaluation, remote config payloads, and decrypted flag payloads. Example:<br />`posthog.Client(project_api_key, secret_key="phx_...")` |
| `metrics`<br />**Type:** `Optional[dict]` · **Default:** `None` |
| `enable_full_ai_capture`<br />**Default:** `False`<br />Route PostHog AI wrapper events through the dedicated AI capture endpoint and capture full AI content: skips string truncation and passes media (base64/data URIs) through unredacted. `privacy_mode` always wins. Defaults to False. |
| `capture_trace_context`<br />**Default:** `False`<br />When OpenTelemetry is installed and a valid span is active at capture time, add its trace and span IDs as `$trace_id` and `$span_id` properties to events captured with `capture()` and `capture_ai()`, so they can be correlated with backend traces. Explicit `$trace_id`/`$span_id` values passed in `properties` win. Exception events (`capture_exception`) always attach these IDs regardless of this setting. Defaults to False. |
| `traces`<br />**Type:** `Optional[dict]` · **Default:** `None`<br />Config dict for distributed tracing: `service_name`, `service_version`, `environment`, `resource_attributes`, `flush_interval` (5 s), `max_queue_size` (2048), `max_export_batch_size` (512), `max_live_spans` (10000), `max_span_age` (3600 s), `max_attributes_per_span` (128), `max_events_per_span` (128), `max_attribute_value_length` (8192). `before_span_send` is a callable, or a list run in order, that receives each finished span as a dict (`trace_id`, `span_id` and `parent_span_id` are read-only) and returns it, edited, or `None` to drop it; a hook that raises drops the span. Tracing is off until this is provided. Spans export on a background timer even with `sync_mode`; serverless handlers should call `flush()` before returning. Defaults to None. |
| `app_host`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Your PostHog app host, such as `https://posthog.example.com`. `emails` sends there. Needed only when `host` is not PostHog Cloud, for example a reverse proxy, which must not receive the secret key. |
| `email_request_timeout_seconds`<br />**Type:** `float` · **Default:** `15`<br />Timeout in seconds for each email send request. Defaults to 15. |
| `email_request_max_retries`<br />**Type:** `int` · **Default:** `2`<br />Number of retries for an email send after a network error, a rate limit, or an unavailable service. Defaults to 2. Set to 0 when a job runner already retries the send. |

</details>

**Correct example.**

```python
from posthog import Posthog

posthog = Posthog('<ph_project_api_key>', host='<ph_app_host>')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L684)

## Shutdown

### `Client.join`

Attempt to process queued events and end the consumer threads. Do not use directly, call `shutdown()` instead.

Failed or undrainable events may be dropped and reported through logging
or `on_error`; returning does not guarantee server receipt. Lifecycle
cleanup is attempted once, and cleanup failures are logged without retry.

```python
join() -> None
```

**Returns** `None`

**Correct example.**

```python
posthog.join()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L3342)

### `Client.shutdown`

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

```python
shutdown() -> None
```

**Returns** `None`

**Correct example.**

```python
posthog.shutdown()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L3364)

## Flag definition loader

### `Client.load_feature_flags`

Load feature flags for local evaluation.

```python
load_feature_flags()
```

**Correct example.**

```python
posthog.load_feature_flags()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L3825)

### `Client.feature_flag_definitions`

Return feature flag definitions loaded for local evaluation.

```python
feature_flag_definitions()
```

**Returns** . The currently loaded feature flag definitions, or `None` before local evaluation has loaded definitions.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L5252)

## SDK utils

### Other

#### `Client.queue`

The analytics lane's queue (kept for backwards compatibility).

```python
Client.queue: Queue
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1293)

#### `Client.consumers`

Flat list of the lanes' consumers, analytics first (kept for backwards compatibility).

```python
Client.consumers: Optional[List[Consumer]]
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1298)

#### `Client.feature_flags`

Get the local evaluation feature flags.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1478)

#### `Client.metrics`

The `posthog.metrics` API: a statsd-style pre-aggregating metrics client — alpha.

Samples fold into per-series aggregates in memory and flush as one OTLP
data point per series per window, so recording from hot paths is cheap.
Configure via the `metrics` client option; pending metrics flush on
`shutdown()`.

```python
Client.metrics: PostHogMetrics
```

**Correct example.**

```python
client = Posthog("<ph_project_api_key>", metrics={"service_name": "billing-worker"})
client.metrics.count("invoices.processed", 1, attributes={"plan": "pro"})
client.metrics.gauge("queue.depth", 42)
client.metrics.histogram("job.duration", 187, unit="ms")
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L2718)

### Contexts

#### `Client.new_context`

Create a new context for managing shared state. Learn more about [contexts](/docs/libraries/python#contexts).

```python
new_context(fresh = False, capture_exceptions: Optional[bool] = None)
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `fresh`<br />**Default:** `False`<br />Whether to create a fresh context that doesn't inherit from parent. |
| `capture_exceptions`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to automatically capture exceptions in this context. If omitted, defaults to this client's exception autocapture setting. |

</details>

**Correct example.**

```python
with client.new_context():
    client.identify_context('<distinct_id>')
    client.capture('event_name')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1378)

#### `Client.scoped`

Decorator that creates a new context for the wrapped function using this client.

```python
scoped(fresh = False, capture_exceptions: Optional[bool] = None)
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `fresh`<br />**Default:** `False`<br />Whether to create a fresh context that doesn't inherit from parent. |
| `capture_exceptions`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to automatically capture exceptions in this context. If omitted, defaults to this client's exception autocapture setting. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1400)

#### `Client.tag`

Add a tag to the current context.

```python
tag(name: str, value: Any) -> None
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required**<br />The tag key. |
| `value`<br />**Type:** `Any` · **Required**<br />The tag value. |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1416)

#### `Client.get_tags`

Get all tags from the current context.

```python
get_tags() -> Dict[str, Any]
```

**Returns** `Dict[str, Any]`. Dict of all tags in the current context.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1429)

#### `Client.identify_context`

Identify the current context with a distinct ID.

```python
identify_context(distinct_id: str) -> None
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `str` · **Required**<br />The distinct ID to associate with the current context and its children. |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1441)

#### `Client.set_context_session`

Set the session ID for the current context.

```python
set_context_session(session_id: str) -> None
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `session_id`<br />**Type:** `str` · **Required**<br />The session ID to associate with the current context and its children. |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1453)

#### `Client.set_context_device_id`

Set the device ID for the current context.

```python
set_context_device_id(device_id: str) -> None
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `device_id`<br />**Type:** `str` · **Required**<br />The device ID to associate with the current context and its children. |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1465)

### Feature flags

#### `Client.get_feature_payloads`

Get feature flag payloads for a user, preserving valid serialized JSON.

```python
get_feature_payloads(
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    disable_geoip: Optional[bool] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
    device_id: Optional[str] = None,
) -> dict[str, Optional[str]]
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The distinct ID of the user. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `flag_keys_to_evaluate`<br />**Type:** `Optional[list[str]]` · **Default:** `None`<br />A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `dict[str, Optional[str]]`

**Correct example.**

```python
payloads = posthog.get_feature_payloads('<distinct_id>')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1535)

#### `Client.get_flags_decision`

Get feature flags decision.

```python
get_flags_decision(
    distinct_id: Optional[ID_TYPES] = None,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    disable_geoip: Optional[bool] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
    device_id: Optional[str] = None,
) -> FlagsResponse
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `Optional[ID_TYPES]` · **Default:** `None`<br />The distinct ID of the user. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />A dictionary of group information. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />A dictionary of person properties. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />A dictionary of group properties. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP for this request. |
| `flag_keys_to_evaluate`<br />**Type:** `Optional[list[str]]` · **Default:** `None`<br />A list of specific flag keys to evaluate. If provided, only these flags will be evaluated, improving performance. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The device ID for this request. |

</details>

**Returns** `FlagsResponse`

**Correct example.**

```python
decision = posthog.get_flags_decision('user123')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L1628)

#### `Client.get_remote_config_payload`

Get the payload for a remote config feature flag.

```python
get_remote_config_payload(key: str)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The remote config feature flag key. |

</details>

**Returns** . The payload associated with the feature flag, or `None` if the client is disabled, no personal API key is configured, or the request fails. Encrypted payloads are decrypted by PostHog before being returned.

**Note.** Requires `secret_key` for authentication.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L4670)

#### `Client.get_feature_flag_evaluation_runtime`

Return where a locally loaded feature flag is meant to be evaluated.

```python
get_feature_flag_evaluation_runtime(key: str) -> Optional[FeatureFlagEvaluationRuntime]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key. |

</details>

**Returns** `Optional[FeatureFlagEvaluationRuntime]`. The flag's evaluation runtime, or `None` when local evaluation has not loaded a definition for this key. A definition that carries no runtime reports `FeatureFlagEvaluationRuntime.ALL`, the default PostHog applies.

**Correct example.**

```python
from posthog import FeatureFlagEvaluationRuntime

runtime = posthog.get_feature_flag_evaluation_runtime("my-flag")
if runtime is FeatureFlagEvaluationRuntime.SERVER:
    ...
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L5264)

#### `Client.get_feature_flag_keys_by_evaluation_runtime`

Return the keys of locally loaded flags that a runtime can evaluate.

A flag set to `FeatureFlagEvaluationRuntime.ALL` suits either runtime,
so it is returned for `CLIENT` and for `SERVER`, and asking for
`ALL` returns every loaded flag. Use this to decide which flags to hand
to a browser when a backend serves flags to its own frontend.

```python
get_feature_flag_keys_by_evaluation_runtime(
    evaluation_runtime: Union[FeatureFlagEvaluationRuntime, str],
) -> list[str]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `evaluation_runtime`<br />**Type:** `Union[FeatureFlagEvaluationRuntime, str]` · **Required**<br />The runtime to match, as a `FeatureFlagEvaluationRuntime` or its string value. |

</details>

**Returns** `list[str]`. The matching flag keys, in the order local evaluation loaded them. Empty when no definitions are loaded.

**Correct example.**

```python
from posthog import FeatureFlagEvaluationRuntime

client_keys = posthog.get_feature_flag_keys_by_evaluation_runtime(
    FeatureFlagEvaluationRuntime.CLIENT
)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/client.py#L5298)

### Framework integrations

#### `posthog.integrations.django.PosthogContextMiddleware`

**Available in:** Django apps

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

```python
PosthogContextMiddleware(get_response)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `get_response`<br />**Required**<br />The next middleware or view handler in Django's middleware chain. May be synchronous or asynchronous. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L31)

#### `PosthogContextMiddleware.trust_tracing_headers`

**Available in:** Django apps

Deprecated alias for `capture_tracing_headers`.

```python
PosthogContextMiddleware.trust_tracing_headers: bool
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L158)

#### `PosthogContextMiddleware.extra_tags`

**Available in:** Django apps

Compatibility alias for `extra_properties`.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L167)

#### `PosthogContextMiddleware.tag_map`

**Available in:** Django apps

Compatibility alias for `properties_map`.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L176)

#### `PosthogContextMiddleware.extract_tags`

**Available in:** Django apps

Compatibility alias for `extract_properties`.

```python
extract_tags(request)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `request`<br />**Required** |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L184)

#### `PosthogContextMiddleware.extract_properties`

**Available in:** Django apps

Extract event properties and identity from a synchronous request.

```python
extract_properties(request)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `request`<br />**Required** |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L189)

#### `PosthogContextMiddleware.extract_request_user`

**Available in:** Django apps

Extract user ID and email from request in sync context.

```python
extract_request_user(request)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `request`<br />**Required** |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L250)

#### `PosthogContextMiddleware.aextract_tags`

**Available in:** Django apps

Compatibility alias for `aextract_properties`.

```python
async aextract_tags(request)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `request`<br />**Required** |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L256)

#### `PosthogContextMiddleware.aextract_properties`

**Available in:** Django apps

Async version of extract_properties for use in async request handling.

Uses await request.auser() instead of request.user to avoid
SynchronousOnlyOperation in async context.

Follows Django's naming convention for async methods (auser, asave, etc.).

```python
async aextract_properties(request)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `request`<br />**Required** |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L261)

#### `PosthogContextMiddleware.aextract_request_user`

**Available in:** Django apps

Async version of extract_request_user for use in async request handling.

Uses await request.auser() instead of request.user to avoid
SynchronousOnlyOperation in async context.

Follows Django's naming convention for async methods (auser, asave, etc.).

```python
async aextract_request_user(request)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `request`<br />**Required** |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L274)

#### `PosthogContextMiddleware.process_exception`

**Available in:** Django apps

Process exceptions from views and downstream middleware.

Django calls this WHILE still inside the context created by __call__,
so request tags have already been extracted and set. This method just
needs to capture the exception directly.

Django converts view exceptions into responses before they propagate through
the middleware stack, so the context manager in __call__/__acall__ never sees them.

Note: Django's process_exception is always synchronous, even for async views.

```python
process_exception(request, exception)
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `request`<br />**Required** |
| `exception`<br />**Required** |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/django.py#L376)

#### `posthog.integrations.flask.PosthogFlaskIntegration`

**Available in:** Flask apps

Add PostHog request context and exception tracking to a Flask app.

The integration intentionally does not capture exceptions handled by an
application error handler, expected HTTP exceptions, request or response
bodies, query strings, cookies, authorization headers, or arbitrary headers.
Call `capture_exception` explicitly from a custom error handler if a
handled exception should be reported.

```python
PosthogFlaskIntegration(
    app: Optional[Flask] = None,
    client: Optional[Client] = None,
    capture_exceptions: Optional[bool] = None,
    request_filter: Optional[Callable[[Request], bool]] = None,
    extra_properties: Optional[Callable[[Request], Mapping[str, Any]]] = None,
    trust_tracing_headers: Optional[bool] = None,
    capture_tracing_headers: Optional[bool] = None,
)
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `app`<br />**Type:** `Optional[Flask]` · **Default:** `None`<br />An optional Flask application. If omitted, call `init_app` later (the Flask application-factory pattern). |
| `client`<br />**Type:** `Optional[Client]` · **Default:** `None`<br />Optional destination for automatically captured exceptions. The global client is used by default. Request context still enriches events captured through any client and does not redirect capture calls. |
| `capture_exceptions`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Capture exceptions that reach Flask's unhandled exception machinery. If omitted, inherit the effective client's `enable_exception_autocapture` setting. Pass `True` or `False` to override it. |
| `request_filter`<br />**Type:** `Optional[Callable[[Request], bool]]` · **Default:** `None`<br />Optional callback receiving Flask's request object. A false return value disables both context and exception capture for that request. |
| `extra_properties`<br />**Type:** `Optional[Callable[[Request], Mapping[str, Any]]]` · **Default:** `None`<br />Optional callback returning additional event properties. This is useful for application-specific metadata such as an authenticated user's role. Values should not contain secrets or request bodies. |
| `trust_tracing_headers`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Deprecated alias for `capture_tracing_headers`. The capture-named option takes precedence when both are supplied. |
| `capture_tracing_headers`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Read PostHog distinct and session ID headers as request-scoped analytics context. Defaults to True. Explicit capture IDs and event properties take precedence. Never use these values for security. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/flask.py#L91)

#### `PosthogFlaskIntegration.trust_tracing_headers`

**Available in:** Flask apps

Deprecated alias for `capture_tracing_headers`.

```python
PosthogFlaskIntegration.trust_tracing_headers: bool
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/flask.py#L146)

#### `PosthogFlaskIntegration.init_app`

**Available in:** Flask apps

Register the integration with a Flask application once.

```python
init_app(app: Flask) -> None
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `app`<br />**Type:** `Flask` · **Required** |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/flask.py#L154)

#### `posthog.integrations.asgi.PosthogASGIMiddleware`

**Available in:** ASGI apps, such as FastAPI and Starlette

Add PostHog context and automatic exception capture to an ASGI app.

```python
PosthogASGIMiddleware(
    app: _ASGIApp,
    client: Optional[Client] = None,
    capture_exceptions: Optional[bool] = None,
    request_filter: Optional[_RequestFilter] = None,
    extra_properties: Optional[_ExtraProperties] = None,
    trust_tracing_headers: Optional[bool] = None,
    capture_tracing_headers: Optional[bool] = None,
)
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `app`<br />**Type:** `_ASGIApp` · **Required**<br />The downstream ASGI application. |
| `client`<br />**Type:** `Optional[Client]` · **Default:** `None`<br />Optional destination for automatically captured exceptions. The global client is used by default. Request context still enriches events captured through any client and does not redirect capture calls. |
| `capture_exceptions`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Capture exceptions escaping the downstream app. If omitted, inherit the effective client's `enable_exception_autocapture` setting. Pass `True` or `False` to override it. |
| `request_filter`<br />**Type:** `Optional[_RequestFilter]` · **Default:** `None`<br />Optional sync or async callback receiving the ASGI scope. Returning `False` bypasses all instrumentation for that scope. |
| `extra_properties`<br />**Type:** `Optional[_ExtraProperties]` · **Default:** `None`<br />Optional sync or async callback receiving the ASGI scope and returning additional event properties. |
| `trust_tracing_headers`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Deprecated alias for `capture_tracing_headers`. The capture-named option takes precedence when both are supplied. |
| `capture_tracing_headers`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Read PostHog distinct and session ID headers as request-scoped analytics context. Defaults to True. Explicit capture IDs and event properties take precedence. Never use these values for security. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/asgi.py#L182)

#### `PosthogASGIMiddleware.trust_tracing_headers`

**Available in:** ASGI apps, such as FastAPI and Starlette

Deprecated alias for `capture_tracing_headers`.

```python
PosthogASGIMiddleware.trust_tracing_headers: bool
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/asgi.py#L225)

#### `posthog.integrations.celery.PosthogCeleryIntegration`

**Available in:** Celery workers

Celery integration that captures task lifecycle events and exceptions.

```python
PosthogCeleryIntegration(
    client: Optional[Client] = None,
    capture_exceptions: bool = True,
    capture_task_lifecycle_events: bool = True,
    propagate_context: bool = True,
    task_filter: Optional[Callable[[Optional[str], dict[str, Any]], bool]] = None,
)
```

<details>
<summary>Parameters (5)</summary>

| Parameter |
| --- |
| `client`<br />**Type:** `Optional[Client]` · **Default:** `None`<br />Optional `Client` instance. When provided, all events and exceptions are captured through this client rather than the global `posthog` module. Don't skip this if using a custom flag definition cache provider, and pass the custom `Client` instance here initialized with the custom provider so fork safety for that provider is handled correctly. |
| `capture_exceptions`<br />**Type:** `bool` · **Default:** `True`<br />Whether to capture task exceptions via `capture_exception` (default `True`). |
| `capture_task_lifecycle_events`<br />**Type:** `bool` · **Default:** `True`<br />Whether to emit lifecycle events of the task such as "started", "success", "failure" etc. (default `True`). |
| `propagate_context`<br />**Type:** `bool` · **Default:** `True`<br />Whether to propagate PostHog context (distinct ID, session ID, tags) from the producer to the worker via task headers (default `True`). |
| `task_filter`<br />**Type:** `Optional[Callable[[Optional[str], dict[str, Any]], bool]]` · **Default:** `None`<br />Optional callback `(task_name, task_properties) -> bool` expected to return `False` if a given task should not be tracked. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/celery.py#L89)

#### `PosthogCeleryIntegration.instrument`

**Available in:** Celery workers

Connect Celery signal handlers to capture task events and exceptions.
Call this after initializing the PostHog client and this integration.

If Celery runs on a single host, reinstrumenting in worker children is
not strictly necessary because the PostHog client and this integration
are fork-safe. If Celery workers run across multiple hosts, each worker
process must initialize PostHog, this integration, and call
`instrument()`. Celery provides `worker_process_init` signal to help
with this.

```python
instrument() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/celery.py#L129)

#### `PosthogCeleryIntegration.uninstrument`

**Available in:** Celery workers

Disconnect Celery signal handlers and unregister exit cleanup.

Do not use directly, call `shutdown()` instead.

```python
uninstrument() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/celery.py#L182)

#### `PosthogCeleryIntegration.shutdown`

**Available in:** Celery workers

Disconnect all signal handlers registered by `instrument()`, flush all pending events
and cleanly shutdown the integration.

`shutdown()` is also registered on `worker_process_shutdown` and `atexit` signals,
but there is no guarantee those will always be called, so we strongly recommend calling
it manually when the integration is no longer needed to avoid data loss.

```python
shutdown() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/celery.py#L190)

#### `posthog.integrations.drf.create_exception_handler`

**Available in:** Django REST framework apps

Create a PostHog-instrumented DRF exception handler.

When `POSTHOG_MW_REQUEST_FILTER` is configured, it is also applied to the
underlying Django request before capture so handled errors cannot bypass the
middleware's per-request exclusion.

The delegated handler is called first. A `None` response is never
captured here because DRF will re-raise that exception, allowing Django's
middleware to capture it as unhandled.

```python
create_exception_handler(
    handler: Optional[Callable[[Exception, Mapping[str, Any]], Any]] = None,
    client: Optional[Client] = None,
    capture_exceptions: Optional[bool] = None,
    capture_4xx: bool = False,
    exception_filter: Optional[Callable[[Exception, Any, Mapping[str, Any]], bool]] = None,
) -> Callable[[Exception, Mapping[str, Any]], Any]
```

<details>
<summary>Parameters (5)</summary>

| Parameter |
| --- |
| `handler`<br />**Type:** `Optional[Callable[[Exception, Mapping[str, Any]], Any]]` · **Default:** `None`<br />Handler to delegate to. Defaults to DRF's standard exception handler. Its return value and raised exceptions are preserved. |
| `client`<br />**Type:** `Optional[Client]` · **Default:** `None`<br />Optional PostHog client. Client precedence is this argument, the legacy `POSTHOG_DRF_CLIENT` alias, `POSTHOG_MW_CLIENT`, then the global PostHog client. |
| `capture_exceptions`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to capture handled DRF exceptions. An explicit value takes precedence over `POSTHOG_MW_CAPTURE_EXCEPTIONS`. A boolean setting is used directly; an explicit `None` setting inherits the effective client's `enable_exception_autocapture`. An omitted or malformed setting preserves the legacy `True` default. |
| `capture_4xx`<br />**Type:** `bool` · **Default:** `False`<br />Also capture handled 4xx responses. Disabled by default to avoid reporting expected API errors. |
| `exception_filter`<br />**Type:** `Optional[Callable[[Exception, Any, Mapping[str, Any]], bool]]` · **Default:** `None`<br />Optional final filter called with `(exception, response, context)`. Returning `False` suppresses capture. |

</details>

**Returns** `Callable[[Exception, Mapping[str, Any]], Any]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/drf.py#L140)

#### `posthog.integrations.otel.PostHogContextSpanProcessor`

**Available in:** the `otel` extra, `posthog[otel]`

Add PostHog request IDs to application-owned OpenTelemetry SDK spans.

Add this processor to the application's existing TracerProvider before
starting requests. It snapshots child span context at span start, without
changing exporters, sampling or providers. Request integrations must also
enrich an incoming span that started before request context was available.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/otel.py#L20)

#### `PostHogContextSpanProcessor.on_start`

**Available in:** the `otel` extra, `posthog[otel]`

```python
on_start(span: Span, parent_context: Optional[Context] = None) -> None
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `span`<br />**Type:** `Span` · **Required** |
| `parent_context`<br />**Type:** `Optional[Context]` · **Default:** `None` |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/otel.py#L29)

#### `posthog.integrations.otel.enrich_current_span`

**Available in:** the `otel` extra, `posthog[otel]`

Add request IDs to an existing recording span after request context is set.

Explicit span attributes take precedence. Missing OpenTelemetry support and
context lookup failures do not interrupt request handling.

```python
enrich_current_span() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/integrations/otel.py#L11)

### HTTP connection

#### `posthog.request.set_socket_options`

Configure socket options for all SDK HTTP connections.

Call this during initialization, before making API requests. Pass `None`
to reset to the default socket behavior.

```python
set_socket_options(socket_options: Optional[SocketOptions]) -> None
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `socket_options`<br />**Type:** `Optional[SocketOptions]` · **Required**<br />A list of `(level, option, value)` tuples accepted by urllib3/`socket.setsockopt()`, or `None` to reset defaults. |

</details>

**Returns** `None`

**Correct example.**

```python
from posthog import set_socket_options
set_socket_options([(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)])
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/request.py#L143)

#### `posthog.request.enable_keep_alive`

Enable TCP keepalive for SDK HTTP connections.

This helps prevent idle pooled connections from being dropped by network
infrastructure. Call during initialization, before making API requests.

```python
enable_keep_alive() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/request.py#L166)

#### `posthog.request.disable_connection_reuse`

Disable HTTP connection reuse for SDK requests.

Each request will create a fresh connection. This can avoid issues with
environments that terminate pooled connections, but adds per-request
overhead. Call during initialization, before making API requests.

```python
disable_connection_reuse() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/request.py#L176)

### AI wrapper clients

#### `posthog.ai.openai`

**Available in:** apps with the `openai` package

Exports `OpenAI`, `AsyncOpenAI`, `AsyncAzureOpenAI`, `AzureOpenAI`.

#### `posthog.ai.anthropic`

**Available in:** apps with the `anthropic` package

Exports `Anthropic`, `AsyncAnthropic`, `AnthropicBedrock`, `AnthropicVertex`, `AsyncAnthropicBedrock`, `AsyncAnthropicVertex`.

#### `posthog.ai.gemini`

**Available in:** apps with the Google Gen AI package

Exports `Client`, `AsyncClient`.

#### `posthog.ai.langchain`

**Available in:** the `langchain` extra, `posthog[langchain]`

Exports `CallbackHandler`.

#### `posthog.ai.openai_agents`

**Available in:** apps with the `openai-agents` package

Exports `TYPE_CHECKING`, `Any`, `Callable`, `Dict`, `Optional`, `Union`, `Trace`, `Client`, `PostHogTracingProcessor`.

#### `posthog.ai.claude_agent_sdk`

**Available in:** apps with the `claude-agent-sdk` package

Exports `TYPE_CHECKING`, `Any`, `Callable`, `Dict`, `Optional`, `Union`, `ClaudeAgentOptions`, `ResultMessage`, `Client`, `PostHogClaudeSDKClient`, `PostHogClaudeAgentProcessor`.

## AsyncClient

### `AsyncClient.alias`

**Available in:** the `async` extra, `posthog[async]`

```python
alias(
    previous_id: ID_TYPES,
    distinct_id: Optional[str],
    timestamp: Optional[Union[datetime, str]] = None,
    uuid: Optional[str] = None,
    disable_geoip: Optional[bool] = None,
) -> Optional[str]
```

<details>
<summary>Parameters (5)</summary>

| Parameter |
| --- |
| `previous_id`<br />**Type:** `ID_TYPES` · **Required** |
| `distinct_id`<br />**Type:** `Optional[str]` · **Required** |
| `timestamp`<br />**Type:** `Optional[Union[datetime, str]]` · **Default:** `None` |
| `uuid`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None` |

</details>

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L772)

### `AsyncClient.capture`

**Available in:** the `async` extra, `posthog[async]`

Queue an event without blocking for network delivery.

```python
capture(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `event`<br />**Type:** `str` · **Required** |

</details>

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L570)

### `AsyncClient.capture_immediate`

**Available in:** the `async` extra, `posthog[async]`

Capture one event and wait until its delivery attempt completes.

```python
async capture_immediate(
    event: str,
    **kwargs: Unpack[OptionalCaptureArgs],
) -> Optional[str]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `event`<br />**Type:** `str` · **Required** |

</details>

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L613)

### `AsyncClient.capture_exception`

**Available in:** the `async` extra, `posthog[async]`

Capture an exception. This method never raises, including in debug mode.

```python
capture_exception(
    exception: Optional[ExceptionArg] = None,
    **kwargs: Unpack[OptionalCaptureArgs],
) -> Optional[str]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `exception`<br />**Type:** `Optional[ExceptionArg]` · **Default:** `None` |

</details>

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L823)

### `AsyncClient.emails`

**Available in:** the `async` extra, `posthog[async]`

Send transactional email from PostHog email templates, and read the
status of each email sent with `get`.

Needs `secret_key`, a project secret API key with the `hog_flow:write`
scope, and `posthog[async]`. Sends go to `app_host`, or to the PostHog
Cloud app host of `host`. `email_request_timeout_seconds` and
`email_request_max_retries` configure each send. A disabled or shut-down
client sends nothing. See `posthog.emails` for setup.

```python
AsyncClient.emails: AsyncEmails
```

**Correct example.**

```python
await client.emails.send(
    to="user@example.com",
    template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b",
    distinct_id="user_123",
)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L268)

### `AsyncClient.flush`

**Available in:** the `async` extra, `posthog[async]`

```python
async flush(timeout_seconds: Optional[float] = 10) -> None
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `timeout_seconds`<br />**Type:** `Optional[float]` · **Default:** `10` |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L1204)

### `AsyncClient.evaluate_flags`

**Available in:** the `async` extra, `posthog[async]`

Evaluate flags remotely without blocking the event loop.

The returned snapshot has synchronous in-memory accessors. Pass it to
`capture(flags=...)` to attach the exact values used for branching.
This client does not evaluate flags locally, so the snapshot's
`unresolved_flags` is always empty.

```python
async evaluate_flags(
    distinct_id: Optional[ID_TYPES] = None,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    disable_geoip: Optional[bool] = None,
    flag_keys: Optional[list[str]] = None,
    device_id: Optional[str] = None,
) -> FeatureFlagEvaluations
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `Optional[ID_TYPES]` · **Default:** `None` |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None` |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None` |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None` |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None` |
| `flag_keys`<br />**Type:** `Optional[list[str]]` · **Default:** `None` |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None` |

</details>

**Returns** `FeatureFlagEvaluations`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L959)

### `AsyncClient.group_identify`

**Available in:** the `async` extra, `posthog[async]`

```python
group_identify(
    group_type: str,
    group_key: str,
    properties: Optional[Dict[str, Any]] = None,
    timestamp: Optional[Union[datetime, str]] = None,
    uuid: Optional[Union[str, UUID]] = None,
    disable_geoip: Optional[bool] = None,
    distinct_id: Optional[ID_TYPES] = None,
) -> Optional[str]
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `group_type`<br />**Type:** `str` · **Required** |
| `group_key`<br />**Type:** `str` · **Required** |
| `properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None` |
| `timestamp`<br />**Type:** `Optional[Union[datetime, str]]` · **Default:** `None` |
| `uuid`<br />**Type:** `Optional[Union[str, UUID]]` · **Default:** `None` |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None` |
| `distinct_id`<br />**Type:** `Optional[ID_TYPES]` · **Default:** `None` |

</details>

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L727)

### `AsyncClient.set`

**Available in:** the `async` extra, `posthog[async]`

```python
set(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L701)

### `AsyncClient.set_once`

**Available in:** the `async` extra, `posthog[async]`

```python
set_once(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L714)

### `AsyncClient(...)`

**Available in:** the `async` extra, `posthog[async]`

```python
AsyncClient(
    project_api_key: str,
    host: Optional[str] = None,
    debug: bool = False,
    max_queue_size: int = 10000,
    send: bool = True,
    on_error = None,
    flush_at: int = 100,
    flush_interval: float = 5.0,
    gzip: bool = False,
    max_retries: int = 3,
    timeout: float = 15,
    thread: int = 1,
    disabled: bool = False,
    disable_geoip: bool = True,
    is_server: bool = True,
    historical_migration: bool = False,
    super_properties: Optional[dict[str, Any]] = None,
    before_send = None,
    log_captured_exceptions: bool = False,
    project_root: Optional[str] = None,
    capture_exception_code_variables: bool = False,
    code_variables_mask_patterns = None,
    code_variables_ignore_patterns = None,
    code_variables_mask_url_credentials = None,
    code_variables_detect_secrets = None,
    in_app_modules: Optional[list[str]] = None,
    capture_mode: Optional[Union[CaptureMode, str]] = None,
    capture_compression: Optional[Union[CaptureCompression, str]] = None,
    capture_trace_context: bool = False,
    secret_key: Optional[str] = None,
    personal_api_key: Optional[str] = None,
    feature_flags_request_timeout_seconds: float = 3,
    feature_flags_request_max_retries: int = 1,
    app_host: Optional[str] = None,
    email_request_timeout_seconds: float = 15,
    email_request_max_retries: int = 2,
)
```

<details>
<summary>Parameters (36)</summary>

| Parameter |
| --- |
| `project_api_key`<br />**Type:** `str` · **Required** |
| `host`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `debug`<br />**Type:** `bool` · **Default:** `False` |
| `max_queue_size`<br />**Type:** `int` · **Default:** `10000` |
| `send`<br />**Type:** `bool` · **Default:** `True` |
| `on_error`<br />**Default:** `None` |
| `flush_at`<br />**Type:** `int` · **Default:** `100` |
| `flush_interval`<br />**Type:** `float` · **Default:** `5.0` |
| `gzip`<br />**Type:** `bool` · **Default:** `False` |
| `max_retries`<br />**Type:** `int` · **Default:** `3` |
| `timeout`<br />**Type:** `float` · **Default:** `15` |
| `thread`<br />**Type:** `int` · **Default:** `1` |
| `disabled`<br />**Type:** `bool` · **Default:** `False` |
| `disable_geoip`<br />**Type:** `bool` · **Default:** `True` |
| `is_server`<br />**Type:** `bool` · **Default:** `True` |
| `historical_migration`<br />**Type:** `bool` · **Default:** `False` |
| `super_properties`<br />**Type:** `Optional[dict[str, Any]]` · **Default:** `None` |
| `before_send`<br />**Default:** `None` |
| `log_captured_exceptions`<br />**Type:** `bool` · **Default:** `False` |
| `project_root`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `capture_exception_code_variables`<br />**Type:** `bool` · **Default:** `False` |
| `code_variables_mask_patterns`<br />**Default:** `None` |
| `code_variables_ignore_patterns`<br />**Default:** `None` |
| `code_variables_mask_url_credentials`<br />**Default:** `None` |
| `code_variables_detect_secrets`<br />**Default:** `None` |
| `in_app_modules`<br />**Type:** `Optional[list[str]]` · **Default:** `None` |
| `capture_mode`<br />**Type:** `Optional[Union[CaptureMode, str]]` · **Default:** `None` |
| `capture_compression`<br />**Type:** `Optional[Union[CaptureCompression, str]]` · **Default:** `None` |
| `capture_trace_context`<br />**Type:** `bool` · **Default:** `False` |
| `secret_key`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `personal_api_key`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `feature_flags_request_timeout_seconds`<br />**Type:** `float` · **Default:** `3` |
| `feature_flags_request_max_retries`<br />**Type:** `int` · **Default:** `1` |
| `app_host`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `email_request_timeout_seconds`<br />**Type:** `float` · **Default:** `15` |
| `email_request_max_retries`<br />**Type:** `int` · **Default:** `2` |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L102)

### `AsyncClient.shutdown`

**Available in:** the `async` extra, `posthog[async]`

```python
async shutdown() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L1235)

### `AsyncClient.join`

**Available in:** the `async` extra, `posthog[async]`

```python
async join() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L1305)

### `AsyncClient.get_remote_config_payload`

**Available in:** the `async` extra, `posthog[async]`

Fetch and decrypt a remote-config payload without blocking the loop.

```python
async get_remote_config_payload(key: str) -> Optional[Any]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required** |

</details>

**Returns** `Optional[Any]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/async_client.py#L1064)

## Module functions

### `posthog.start_span`

Start a span for distributed tracing. Alpha.

Returns a span handle. Use it as a context manager to make it the active
span for the block and end it on exit (recording a raised exception on the
way out); or call `end()` yourself for a span that cannot wrap a block.
Spans started inside the block nest under it automatically. Always returns
a usable handle, even when tracing is off, so calling code never branches.

```python
start_span(
    name: str,
    kind: Optional[str] = None,
    attributes: Optional[Mapping[str, Any]] = None,
    parent: Union[Span, str, None] = None,
    tracestate: Optional[str] = None,
    start_time: Union[datetime.datetime, float, None] = None,
) -> Span
```

<details>
<summary>Parameters (6)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required**<br />A low-cardinality operation name, e.g. `GET /users/:id`. Variable values belong in attributes, not the name. |
| `kind`<br />**Type:** `Optional[str]` · **Default:** `None`<br />`internal` (default), `server`, `client`, `producer` or `consumer`. |
| `attributes`<br />**Type:** `Optional[Mapping[str, Any]]` · **Default:** `None`<br />Initial attributes. |
| `parent`<br />**Type:** `Union[Span, str, None]` · **Default:** `None`<br />A span handle, or an inbound W3C `traceparent` header value to continue a remote trace. Defaults to the active span. |
| `tracestate`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The inbound `tracestate` header accompanying a `traceparent` string `parent`; preserved and propagated. |
| `start_time`<br />**Type:** `Union[datetime.datetime, float, None]` · **Default:** `None`<br />A `datetime` or epoch seconds, to backdate the span. |

</details>

**Returns** `Span`

**Correct example.**

```python
import posthog
posthog.traces = {"service_name": "checkout-api"}

with posthog.start_span("POST /checkout", parent=request.headers.get("traceparent")) as span:
    span.set_attribute("plan", user.plan)
    with posthog.start_span("db.query", kind="client"):
        ...
    outgoing_headers = {"traceparent": span.traceparent()}
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1284)

### `posthog.get_active_span`

The span that is active in the current context, or `None`. Alpha.

Only entering a span (`with posthog.start_span(...) as span:`) makes it
active; a span started manually is not. Use it to propagate the trace to
the next service: `span.traceparent()` is the header value.

```python
get_active_span() -> Optional[Span]
```

**Returns** `Optional[Span]`

**Correct example.**

```python
span = posthog.get_active_span()
if span is not None:
    headers["traceparent"] = span.traceparent()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1339)

### `posthog.alias`

Associate user behaviour before and after they e.g. register, login, or perform some other identifying action.

```python
alias(
    previous_id: ID_TYPES,
    distinct_id: str,
    timestamp: Optional[Union[datetime.datetime, str]] = None,
    uuid: Optional[str] = None,
    disable_geoip: Optional[bool] = None,
) -> Optional[str]
```

<details>
<summary>Parameters (5)</summary>

| Parameter |
| --- |
| `previous_id`<br />**Type:** `ID_TYPES` · **Required**<br />The unique ID of the user before |
| `distinct_id`<br />**Type:** `str` · **Required**<br />The current unique id |
| `timestamp`<br />**Type:** `Optional[Union[datetime.datetime, str]]` · **Default:** `None`<br />Optional timestamp for the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. |
| `uuid`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional UUID for the event |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup |

</details>

**Returns** `Optional[str]`

**Details.** To marry up whatever a user does before they sign up or log in with what they do after you need to make an alias call. This will allow you to answer questions like "Which marketing channels leads to users churning after a month?" or "What do users do on our website before signing up?". Particularly useful for associating user behaviour before and after they e.g. register, login, or perform some other identifying action.

**Correct example.**

```python
# Alias user
from posthog import alias
alias(previous_id='distinct_id', distinct_id='alias_id')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L685)

### `posthog.capture`

Capture anything a user does within your system.

```python
capture(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `event`<br />**Type:** `str` · **Required**<br />The event name to specify the event |
| `**kwargs`<br />Optional arguments including: distinct_id: Unique identifier for the user properties: Dict of event properties timestamp: When the event occurred. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. uuid: Unique identifier for this event. If omitted, one is generated and returned. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. groups: Dict of group types and IDs flags: A FeatureFlagEvaluations snapshot from evaluate_flags(). The exact values from the snapshot are attached with no extra /flags request. send_feature_flags: Deprecated. Prefer flags=... from evaluate_flags(). When truthy, evaluates flags during capture and attaches them to the event. disable_geoip: Whether to disable GeoIP lookup |

</details>

**Returns** `Optional[str]`

**Details.** Capture allows you to capture anything a user does within your system, which you can later use in PostHog to find patterns in usage, work out which features to improve or where people are giving up. A capture call requires an event name to specify the event. We recommend using [verb] [noun], like `movie played` or `movie updated` to easily identify what your events mean later on. Capture takes a number of optional arguments, which are defined by the `OptionalCaptureArgs` type.

**Correct example.**

```python
# Context and capture usage
from posthog import new_context, identify_context, tag_context, capture
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
    tag_context('some-tag', 'some-value')

    capture('another-event') # Will be captured with `'some-tag': 'some-value'` in the properties dict
```

**Correct example.**

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

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L466)

### `posthog.capture_ai`

Capture an AI event on the dedicated AI capture endpoint.

Beta: the signature is stable; operational limits (per-event size cap,
batching, endpoint) may change without notice.

Takes the same arguments and returns the same value as `capture()`: the
event UUID, or None when the event was not admitted (disabled client, or
dropped by `before_send`). The event is delivered on an isolated queue
with its own consumer pool and a higher per-event size cap, posting to
the dedicated AI ingestion endpoint. The payload is sent as given — no
redaction or truncation is applied here.

```python
capture_ai(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `event`<br />**Type:** `str` · **Required**<br />The event name, normally one of the `$ai_*` event names. |
| `**kwargs`<br />Same optional arguments as `capture()`. |

</details>

**Returns** `Optional[str]`

**Correct example.**

```python
from posthog import capture_ai

uuid = capture_ai(
    "$ai_generation",
    distinct_id="user_123",
    properties={"$ai_model": "gpt-5"},
)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L539)

### `posthog.capture_exception`

Capture exceptions that happen in your code.

```python
capture_exception(
    exception: Optional[ExceptionArg] = None,
    **kwargs: Unpack[OptionalCaptureArgs],
) -> Optional[str]
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `exception`<br />**Type:** `Optional[ExceptionArg]` · **Default:** `None`<br />The exception to capture. If not provided, the current exception is captured via `sys.exc_info()` |
| `**kwargs`<br />Optional capture arguments including distinct_id, properties, timestamp, uuid, groups, flags, send_feature_flags, and disable_geoip. Overriding reserved exception properties through `properties` is deprecated and will stop working in the next major version. |

</details>

**Returns** `Optional[str]`

**Details.** Capture exception is idempotent - if it is called twice with the same exception instance, only a occurrence will be tracked in posthog. This is because, generally, contexts will cause exceptions to be captured automatically. However, to ensure you track an exception, if you catch and do not re-raise it, capturing it manually is recommended, unless you are certain it will have crossed a context boundary (e.g. by existing a `with posthog.new_context():` block already). If the passed exception was raised and caught, the captured stack trace will consist of every frame between where the exception was raised and the point at which it is captured (the "traceback"). If the passed exception was never raised, e.g. if you call `posthog.capture_exception(ValueError("Some Error"))`, the stack trace captured will be the full stack trace at the moment the exception was captured. Note that heavy use of contexts will lead to truncated stack traces, as the exception will be captured by the context entered most recently, which may not be the point you catch the exception for the final time in your code. It's recommended to use contexts sparingly, for this reason. `capture_exception` takes the same set of optional arguments as `capture`.

**Correct example.**

```python
# Capture exception
from posthog import capture_exception
try:
    risky_operation()
except Exception as e:
    capture_exception(e)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L725)

### `posthog.flush`

Attempt immediate delivery of queued events and ended spans, keeping the global client reusable.

Call `flush()` before a short-lived request or serverless invocation returns
when using the shared module-level client. Use `shutdown()` only for final
process cleanup or a client that will not be reused. Flush does not stop the
background workers or feature flag poller, and does not end open spans.

Returning does not guarantee server receipt: pending work can remain after a
timeout or span export failure, and failed events may be dropped by consumers.
Calls made directly from SDK callbacks such as `on_error` are deferred
rather than blocking the worker that invoked the callback.

```python
flush(timeout_seconds: Optional[float] = 10) -> None
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `timeout_seconds`<br />**Type:** `Optional[float]` · **Default:** `10`<br />Seconds to wait for event queues to drain, shared across event lanes. Defaults to 10 seconds. Pass `None` to wait without a time limit; this does not guarantee successful delivery. Queued spans flush concurrently, waiting up to this many seconds for an in-flight flush before starting a fresh drain budget. A lock wait timeout leaves the in-flight flush to finish. At least one span request is attempted after acquiring the lock, even with no budget left; retriable failures are retried after backoff while budget remains, with one last attempt at the deadline. The last span request is not cut short and is bounded by the client's `timeout` in seconds, so this is not a strict wall-clock limit for the whole call. With `None`, a retriable span failure is left for a later flush rather than retried within this call. |

</details>

**Returns** `None`

**Correct example.**

```python
import posthog
posthog.capture('event_name', distinct_id='user_id')
# Before a serverless invocation returns; keep the global client open.
posthog.flush(timeout_seconds=10)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1223)

### `posthog.get_feature_flag`

Get feature flag variant for users. Used with experiments.

```python
get_feature_flag(
    key: str,
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[FlagValue]
```

<details>
<summary>Parameters (9)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The user's distinct ID |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />Groups mapping from group type to group key |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />Person properties |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />Group properties in format { group_type_name: { group_properties } } |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to evaluate only locally |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `True`<br />Whether to send feature flag events |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional device ID override for experience-continuity flags |

</details>

**Returns** `Optional[FlagValue]`

**Details.** `groups` are a mapping from group type to group key. So, if you have a group type of "organization" and a group key of "5", you would pass groups={"organization": "5"}. `group_properties` take the format: { group_type_name: { group_properties } }. So, for example, if you have the group type "organization" and the group key "5", with the properties name, and employee count, you'll send these as: group_properties={"organization": {"name": "PostHog", "employees": 11}}.

**Correct example.**

```python
# Multivariate feature flag
from posthog import get_feature_flag, get_feature_flag_payload
enabled_variant = get_feature_flag('flag-key', 'distinct_id_of_your_user')
if enabled_variant == 'variant-key':
    matched_flag_payload = get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L809)

### `posthog.get_feature_flag_payload`

Get the payload associated with a feature flag value.

Deprecated for new code. Prefer `evaluate_flags()` and
`flags.get_flag_payload(key)` so flag evaluation happens once per request.

```python
get_feature_flag_payload(
    key: str,
    distinct_id: ID_TYPES,
    match_value: Optional[FlagValue] = None,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[object]
```

<details>
<summary>Parameters (10)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key. |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The user's distinct ID. |
| `match_value`<br />**Type:** `Optional[FlagValue]` · **Default:** `None`<br />Optional flag value to use when selecting a payload. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />Mapping of group type to group key. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />Person properties to use for evaluation. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />Group properties keyed by group type. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to evaluate only locally. |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `True`<br />Whether to send a $feature_flag_called event. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional device ID override for experience-continuity flags. |

</details>

**Returns** `Optional[object]`. The payload associated with the matched feature flag value, or None. This function returns the payload only, not the FeatureFlagResult wrapper used internally to compute it.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L965)

### `posthog.get_feature_flag_result`

Get a FeatureFlagResult object which contains the flag result and payload.

This method evaluates a feature flag and returns a FeatureFlagResult object containing:
- enabled: Whether the flag is enabled
- variant: The variant value if the flag has variants
- payload: The payload associated with the flag (automatically deserialized from JSON)
- key: The flag key
- reason: Why the flag was enabled/disabled

Example:
``python
result = posthog.get_feature_flag_result('beta-feature', 'distinct_id')
if result and result.enabled:
    # Use the variant and payload
    print(f"Variant: {result.variant}")
    print(f"Payload: {result.payload}")
``

```python
get_feature_flag_result(
    key: str,
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[FeatureFlagResult]
```

<details>
<summary>Parameters (9)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key. |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The user's distinct ID. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />Mapping of group type to group key. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />Person properties to use for evaluation. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />Group properties keyed by group type. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to evaluate only locally. |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `True`<br />Whether to send a $feature_flag_called event. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional device ID override for experience-continuity flags. |

</details>

**Returns** `Optional[FeatureFlagResult]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L908)

### `posthog.get_all_flags`

Get all flags for a given user.

```python
get_all_flags(
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
) -> Optional[dict[str, FlagValue]]
```

<details>
<summary>Parameters (8)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The user's distinct ID |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />Groups mapping |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />Person properties |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />Group properties |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to evaluate only locally |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional device ID override for experience-continuity flags |
| `flag_keys_to_evaluate`<br />**Type:** `Optional[list[str]]` · **Default:** `None`<br />Optional list of flag keys to evaluate (evaluates all if None) |

</details>

**Returns** `Optional[dict[str, FlagValue]]`

**Details.** Flags are key-value pairs where the key is the flag key and the value is the flag variant, or True, or False.

**Correct example.**

```python
# All flags for user
from posthog import get_all_flags
get_all_flags('distinct_id_of_your_user')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L861)

### `posthog.get_all_flags_and_payloads`

Get all feature flag values and payloads for a user.

```python
get_all_flags_and_payloads(
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
) -> FlagsAndPayloads
```

<details>
<summary>Parameters (8)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The user's distinct ID. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />Mapping of group type to group key. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />Person properties to use for evaluation. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />Group properties keyed by group type. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to evaluate only locally. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional device ID override for experience-continuity flags. |
| `flag_keys_to_evaluate`<br />**Type:** `Optional[list[str]]` · **Default:** `None`<br />Optional list of flag keys to evaluate. Evaluates all flags when omitted. |

</details>

**Returns** `FlagsAndPayloads`. A dict with `featureFlags` and `featureFlagPayloads` entries.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1037)

### `posthog.evaluate_flags`

Evaluate all feature flags for a user in a single call and return a
`FeatureFlagEvaluations` snapshot. Branch on `.is_enabled()` /
`.get_flag()` and pass the same snapshot to `capture()` via the
`flags` option so events carry the exact flag values the code branched on.

Prefer this over repeated `get_feature_flag()` calls and over
`capture(send_feature_flags=True)` — it consolidates flag evaluation into
a single `/flags` request per incoming request.

```python
evaluate_flags(
    distinct_id: Optional[ID_TYPES] = None,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    disable_geoip: Optional[bool] = None,
    flag_keys: Optional[list[str]] = None,
    device_id: Optional[str] = None,
) -> FeatureFlagEvaluations
```

<details>
<summary>Parameters (8)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `Optional[ID_TYPES]` · **Default:** `None`<br />The user's distinct ID. If `None`, falls back to the context distinct_id. If still unresolvable, returns an empty snapshot. |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />Mapping of group type to group key. |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />Person properties to use for evaluation. |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />Group properties keyed by group type. |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />If `True`, never fall back to remote evaluation and omit flags that cannot be evaluated locally. The snapshot's `unresolved_flags` lists them with a reason. |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup. |
| `flag_keys`<br />**Type:** `Optional[list[str]]` · **Default:** `None`<br />Optional list that scopes local evaluation, the underlying `/flags` request, and the returned snapshot. When omitted or `None`, all flags are evaluated. An empty list returns an empty snapshot without evaluating flags. A requested key absent from loaded local definitions is included in one remote fallback per `evaluate_flags` call unless `only_evaluate_locally` is `True`. If the server also does not know the key, it is omitted from the snapshot. |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional device ID override. If not provided, falls back to the context device_id (which may be set via tracing headers). Used by experience-continuity flags to match users across distinct_id changes. |

</details>

**Returns** `FeatureFlagEvaluations`

**Correct example.**

```python
from posthog import evaluate_flags, capture
flags = evaluate_flags("user_123", person_properties={"plan": "enterprise"})
if flags.is_enabled("new-dashboard"):
    render_new_dashboard()
capture("page_viewed", distinct_id="user_123", flags=flags)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1079)

### `posthog.group_identify`

Set properties on a group.

```python
group_identify(
    group_type: str,
    group_key: str,
    properties: Optional[Dict[str, Any]] = None,
    timestamp: Optional[Union[datetime.datetime, str]] = None,
    uuid: Optional[str] = None,
    disable_geoip: Optional[bool] = None,
    distinct_id: Optional[ID_TYPES] = None,
) -> Optional[str]
```

<details>
<summary>Parameters (7)</summary>

| Parameter |
| --- |
| `group_type`<br />**Type:** `str` · **Required**<br />Type of your group. Required - the call is dropped with a warning if it is missing or empty. |
| `group_key`<br />**Type:** `str` · **Required**<br />Unique identifier of the group. Required - the call is dropped with a warning if it is missing or empty. |
| `properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />Properties to set on the group |
| `timestamp`<br />**Type:** `Optional[Union[datetime.datetime, str]]` · **Default:** `None`<br />Optional timestamp for the event. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. |
| `uuid`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional UUID for the event |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup |
| `distinct_id`<br />**Type:** `Optional[ID_TYPES]` · **Default:** `None`<br />Optional distinct ID of the user performing the action |

</details>

**Returns** `Optional[str]`

**Correct example.**

```python
# Group identify
from posthog import group_identify
group_identify('company', 'company_id_in_your_db', {
    'name': 'Awesome Inc.',
    'employees': 11
})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L637)

### `posthog.set`

Set properties on a user record.

```python
set(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `**kwargs`<br />Optional arguments including: distinct_id: Unique identifier for the user. Falls back to the context distinct ID; if none exists, this call does nothing. properties: Dict of person properties to set. timestamp: When the properties were set. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. uuid: Unique identifier for this operation. If omitted, one is generated and returned. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. disable_geoip: Whether to disable GeoIP lookup. |

</details>

**Returns** `Optional[str]`

**Details.** This will overwrite previous people property values. Generally operates similar to `capture`, with distinct_id being an optional argument, defaulting to the current context's distinct ID. If there is no context-level distinct ID, and no override distinct_id is passed, this function will do nothing. Context tags are folded into $set properties, so tagging the current context and then calling `set` will cause those tags to be set on the user (unlike capture, which causes them to just be set on the event).

**Correct example.**

```python
# Set person properties
from posthog import set
set(distinct_id='distinct_id', properties={'name': 'Max Hedgehog'})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L573)

### `posthog.set_once`

Set properties on a user record, only if they do not yet exist.

```python
set_once(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `**kwargs`<br />Optional arguments including: distinct_id: Unique identifier for the user. Falls back to the context distinct ID; if none exists, this call does nothing. properties: Dict of person properties to set only once. timestamp: When the properties were set. UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. uuid: Unique identifier for this operation. If omitted, one is generated and returned. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. disable_geoip: Whether to disable GeoIP lookup. |

</details>

**Returns** `Optional[str]`

**Details.** This will not overwrite previous people property values, unlike `set`. Otherwise, operates in an identical manner to `set`.

**Correct example.**

```python
# Set property once
from posthog import set_once
set_once(distinct_id='distinct_id', properties={'initial_url': '/blog'})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L605)

### `posthog.feature_enabled`

Use feature flags to enable or disable features for users.

```python
feature_enabled(
    key: str,
    distinct_id: ID_TYPES,
    groups: Optional[Mapping[str, Union[str, int]]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[bool]
```

<details>
<summary>Parameters (9)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key |
| `distinct_id`<br />**Type:** `ID_TYPES` · **Required**<br />The user's distinct ID |
| `groups`<br />**Type:** `Optional[Mapping[str, Union[str, int]]]` · **Default:** `None`<br />Groups mapping |
| `person_properties`<br />**Type:** `Optional[Dict[str, Any]]` · **Default:** `None`<br />Person properties |
| `group_properties`<br />**Type:** `Optional[Dict[str, Dict[str, Any]]]` · **Default:** `None`<br />Group properties |
| `only_evaluate_locally`<br />**Type:** `bool` · **Default:** `False`<br />Whether to evaluate only locally |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `True`<br />Whether to send feature flag events |
| `disable_geoip`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to disable GeoIP lookup |
| `device_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Optional device ID override for experience-continuity flags |

</details>

**Returns** `Optional[bool]`

**Details.** You can call `posthog.load_feature_flags()` before to make sure you're not doing unexpected requests.

**Correct example.**

```python
# Boolean feature flag
from posthog import feature_enabled, get_feature_flag_payload
is_my_flag_enabled = feature_enabled('flag-key', 'distinct_id_of_your_user')
if is_my_flag_enabled:
    matched_flag_payload = get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L757)

### `posthog.setup`

Create or return the global PostHog client configured by module settings.

Most applications should either instantiate `Posthog` directly or set
`posthog.api_key`/other module settings before calling top-level helpers.
`setup()` is called automatically by global APIs such as `capture()`.

```python
setup() -> Client
```

**Returns** `Client`. The global `Client` instance. If both `api_key` and `project_api_key` are missing or blank, the client is disabled and module-level calls become no-ops.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1394)

### `posthog.join`

Attempt to process queued events and stop the client's background workers. Use `shutdown()` directly in most cases.

Failed or undrainable events may be dropped and reported through logging or
`on_error`; returning does not guarantee server receipt. Lifecycle cleanup
is attempted once, and cleanup failures are logged without retry.

```python
join() -> None
```

**Returns** `None`

**Correct example.**

```python
from posthog import join
join()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1265)

### `posthog.shutdown`

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

```python
shutdown() -> None
```

**Returns** `None`

**Correct example.**

```python
from posthog import shutdown
shutdown()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1359)

### `posthog.feature_flag_definitions`

Returns loaded feature flags.

```python
feature_flag_definitions()
```

**Details.** Returns loaded feature flags, if any. Helpful for debugging what flag information you have loaded.

**Correct example.**

```python
from posthog import feature_flag_definitions
definitions = feature_flag_definitions()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1142)

### `posthog.load_feature_flags`

Load feature flag definitions from PostHog.

```python
load_feature_flags()
```

**Correct example.**

```python
from posthog import load_feature_flags
load_feature_flags()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1208)

### `posthog.new_context`

Create a new context scope that will be active for the duration of the with block.

```python
new_context(
    fresh: bool = False,
    capture_exceptions: Optional[bool] = None,
    client: Optional[Client] = None,
)
```

<details>
<summary>Parameters (3)</summary>

| Parameter |
| --- |
| `fresh`<br />**Type:** `bool` · **Default:** `False`<br />Whether to start with a fresh context (default: False) |
| `capture_exceptions`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to capture exceptions raised within the context. If omitted, defaults to the relevant client's exception autocapture setting. |
| `client`<br />**Type:** `Optional[Client]` · **Default:** `None`<br />Optional Posthog client instance to use for this context (default: None) |

</details>

**Correct example.**

```python
from posthog import new_context, tag, capture
with new_context():
    tag("request_id", "123")
    capture("event_name", properties={"property": "value"})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L98)

### `posthog.scoped`

Decorator that creates a new context for the function.

```python
scoped(fresh = False, capture_exceptions: Optional[bool] = None)
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `fresh`<br />**Default:** `False`<br />Whether to start with a fresh context (default: False) |
| `capture_exceptions`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />Whether to capture and track exceptions with posthog error tracking. If omitted, defaults to the global exception autocapture setting. |

</details>

**Correct example.**

```python
from posthog import scoped, tag, capture
@scoped()
def process_payment(payment_id):
    tag("payment_id", payment_id)
    capture("payment_started")
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L127)

### `posthog.set_context_session`

Set the session ID for the current context.

```python
set_context_session(session_id: str)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `session_id`<br />**Type:** `str` · **Required**<br />The session ID to associate with the current context and its children |

</details>

**Correct example.**

```python
from posthog import set_context_session
set_context_session("session_123")
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L150)

### `posthog.set_context_device_id`

Set the device ID for the current context, associating all feature flag requests
in this or child contexts with the given device ID.

```python
set_context_device_id(device_id: str)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `device_id`<br />**Type:** `str` · **Required**<br />The device ID to associate with the current context and its children |

</details>

**Correct example.**

```python
from posthog import set_context_device_id
set_context_device_id("device_123")
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L169)

### `posthog.identify_context`

Identify the current context with a distinct ID.

```python
identify_context(distinct_id: str)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `distinct_id`<br />**Type:** `str` · **Required**<br />The distinct ID to associate with the current context and its children |

</details>

**Correct example.**

```python
from posthog import identify_context
identify_context("user_123")
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L189)

### `posthog.set_capture_exception_code_variables_context`

Override code-variable capture for exceptions in the current context.

```python
set_capture_exception_code_variables_context(enabled: bool)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `enabled`<br />**Type:** `bool` · **Required**<br />Whether exceptions captured in this context should include local variable values from stack frames. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L208)

### `posthog.set_code_variables_mask_patterns_context`

Override code-variable mask patterns for exceptions in the current context.

```python
set_code_variables_mask_patterns_context(mask_patterns: list[str])
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `mask_patterns`<br />**Type:** `list[str]` · **Required**<br />Variable-name patterns whose values should be replaced with `***` when code variables are captured. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L222)

### `posthog.set_code_variables_ignore_patterns_context`

Override code-variable ignore patterns for exceptions in the current context.

```python
set_code_variables_ignore_patterns_context(ignore_patterns: list[str])
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `ignore_patterns`<br />**Type:** `list[str]` · **Required**<br />Variable-name patterns that should be omitted entirely when code variables are captured. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L236)

### `posthog.set_code_variables_mask_url_credentials_context`

Whether to scrub credentials embedded in URLs/DSNs (e.g. user:pass@host) from
captured code variables for the current context.

```python
set_code_variables_mask_url_credentials_context(enabled: bool)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `enabled`<br />**Type:** `bool` · **Required** |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L250)

### `posthog.set_code_variables_detect_secrets_context`

Whether to apply entropy-based secret detection as a last-resort redaction of
high-entropy values (API keys, tokens, strong passwords) in captured code
variables for the current context.

```python
set_code_variables_detect_secrets_context(enabled: bool)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `enabled`<br />**Type:** `bool` · **Required** |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L258)

### `posthog.tag`

Add a tag to the current context.

```python
tag(name: str, value: Any)
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required**<br />The tag key |
| `value`<br />**Type:** `Any` · **Required**<br />The tag value |

</details>

**Correct example.**

```python
from posthog import tag
tag("user_id", "123")
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L270)

### `posthog.get_tags`

Get all tags from the current context.

```python
get_tags() -> Dict[str, Any]
```

**Returns** `Dict[str, Any]`. Dict of all tags in the current context

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L290)

### `posthog.get_remote_config_payload`

Get the payload for a remote config feature flag.

```python
get_remote_config_payload(key: str)
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The key of the feature flag |

</details>

**Returns** . The payload associated with the feature flag. If payload is encrypted, the return value will be decrypted

**Note.** Requires secret_key to be set for authentication

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1017)

### `posthog.get_feature_flag_evaluation_runtime`

Return where a locally loaded feature flag is meant to be evaluated.

```python
get_feature_flag_evaluation_runtime(key: str) -> Optional[FeatureFlagEvaluationRuntime]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required** |

</details>

**Returns** `Optional[FeatureFlagEvaluationRuntime]`

**Details.** Reads the `evaluation_runtime` each flag definition carries, so no extra
request is made. Returns `None` when local evaluation has not loaded a
definition for this key. A definition that carries no runtime reports
`FeatureFlagEvaluationRuntime.ALL`, the default PostHog applies.

**Correct example.**

```python
from posthog import FeatureFlagEvaluationRuntime, get_feature_flag_evaluation_runtime
runtime = get_feature_flag_evaluation_runtime("my-flag")
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1160)

### `posthog.get_feature_flag_keys_by_evaluation_runtime`

Return the keys of locally loaded flags that a runtime can evaluate.

```python
get_feature_flag_keys_by_evaluation_runtime(
    evaluation_runtime: Union[FeatureFlagEvaluationRuntime, str],
) -> list[str]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `evaluation_runtime`<br />**Type:** `Union[FeatureFlagEvaluationRuntime, str]` · **Required** |

</details>

**Returns** `list[str]`

**Details.** A flag set to `FeatureFlagEvaluationRuntime.ALL` suits either runtime, so
it is returned for `CLIENT` and for `SERVER`. Use this to decide which
flags to hand to a browser when a backend serves flags to its own
frontend.

**Correct example.**

```python
from posthog import FeatureFlagEvaluationRuntime, get_feature_flag_keys_by_evaluation_runtime
client_keys = get_feature_flag_keys_by_evaluation_runtime(FeatureFlagEvaluationRuntime.CLIENT)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/__init__.py#L1184)

## Configs

<details>
<summary>Settings (7)</summary>

| Setting |
| --- |
| `POSTHOG_MW_CLIENT`<br />**Where:** Django settings<br />client used for automatic exception capture, or the global client. This does not redirect events explicitly captured through other clients. |
| `POSTHOG_MW_CAPTURE_EXCEPTIONS`<br />**Where:** Django settings<br />`False` enables context enrichment only; `True` enables automatic exception capture; `None` inherits the effective client's `enable_exception_autocapture`. When omitted, the legacy default is `True`. |
| `POSTHOG_MW_REQUEST_FILTER`<br />**Where:** Django settings<br />a callback receiving the request. Returning `False` disables both enrichment and automatic exception capture for that request. |
| `POSTHOG_MW_EXTRA_PROPERTIES`<br />**Where:** Django settings<br />a callback returning additional event properties. |
| `POSTHOG_MW_CAPTURE_TRACING_HEADERS`<br />**Where:** Django settings<br />read PostHog identity and session headers as request-scoped analytics context (default: `True`). Explicit capture IDs and event properties take precedence. These values are never authentication or authorization. Authenticated Django user identity remains the fallback when no usable header ID is available or header capture is disabled. |
| `POSTHOG_MW_TRUST_TRACING_HEADERS`<br />**Where:** Django settings<br />deprecated alias for `POSTHOG_MW_CAPTURE_TRACING_HEADERS`; the capture-named setting takes precedence. |
| `POSTHOG_MW_PROPERTIES_MAP`<br />**Where:** Django settings<br />an optional callback transforming request properties. |

</details>

## Types

### `OptionalCaptureArgs`

Optional arguments for the capture method.

<details>
<summary>Fields (8)</summary>

| Field |
| --- |
| `distinct_id`<br />**Type:** `NotRequired[Optional[ID_TYPES]]`<br />Unique identifier for the person associated with this event. If not set, the context distinct_id is used, if available, otherwise a UUID is generated, and the event is marked as personless. Setting context-level distinct_id's is recommended. |
| `properties`<br />**Type:** `NotRequired[Optional[Dict[str, Any]]]`<br />Dictionary of properties to track with the event |
| `timestamp`<br />**Type:** `NotRequired[Optional[Union[datetime, str]]]`<br />When the event occurred (defaults to current time). UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. |
| `uuid`<br />**Type:** `NotRequired[Optional[Union[str, UUID]]]`<br />Unique identifier for this specific event. If not provided, one is generated. The event UUID is returned, so you can correlate it with actions in your app (like showing users an error ID if you capture an exception). If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. |
| `groups`<br />**Type:** `NotRequired[Optional[Dict[str, str]]]`<br />Group identifiers to associate with this event (format: {group_type: group_key}) |
| `flags`<br />**Type:** `NotRequired[Optional[FeatureFlagEvaluations]]`<br />A `FeatureFlagEvaluations` snapshot from `evaluate_flags()`. The exact flag values from the snapshot are attached to the event with no additional network call — prefer this over `send_feature_flags`. |
| `send_feature_flags`<br />**Type:** `NotRequired[Optional[Union[bool, SendFeatureFlagsOptions]]]`<br />Deprecated — prefer `flags` with a `FeatureFlagEvaluations` snapshot. Whether to include currently active feature flags in the event properties. Can be a boolean or a SendFeatureFlagsOptions object. Defaults to False. Fires a hidden `/flags` request on capture and may return different values than the ones the code branched on. |
| `disable_geoip`<br />**Type:** `NotRequired[Optional[bool]]`<br />Whether to disable GeoIP lookup for this event. Defaults to False. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/args.py#L26)

### `OptionalSetArgs`

Optional arguments for the set method.

<details>
<summary>Fields (5)</summary>

| Field |
| --- |
| `distinct_id`<br />**Type:** `NotRequired[Optional[ID_TYPES]]`<br />Unique identifier for the user to set properties on. If not set, the context distinct_id is used, if available, otherwise this function does nothing. Setting context-level distinct_id's is recommended. |
| `properties`<br />**Type:** `NotRequired[Optional[Dict[str, Any]]]`<br />Dictionary of properties to set on the person |
| `timestamp`<br />**Type:** `NotRequired[Optional[Union[datetime, str]]]`<br />When the properties were set (defaults to current time). UTC is preferred; non-UTC datetimes and parseable ISO timestamp strings are converted to UTC. |
| `uuid`<br />**Type:** `NotRequired[Optional[Union[str, UUID]]]`<br />Unique identifier for this operation. If not provided, one is generated. This UUID is returned, so you can correlate it with actions in your app. If provided, it must be a valid UUID string or uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID. |
| `disable_geoip`<br />**Type:** `NotRequired[Optional[bool]]`<br />Whether to disable GeoIP lookup for this operation. Defaults to False. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/args.py#L70)

### `FeatureFlagEvaluations`

A point-in-time snapshot of feature flag evaluations for a single distinct_id.

Returned by `Client.evaluate_flags` — branch on `is_enabled` /
`get_flag` and pass the same object to `Client.capture` via the
`flags` option so the captured event carries the exact flag values the code
branched on.

Example:

```python
flags = posthog.evaluate_flags(distinct_id, person_properties={"plan": "enterprise"})
if flags.is_enabled("new-dashboard"):
    render_new_dashboard()
posthog.capture("page_viewed", distinct_id=distinct_id, flags=flags)
```

To narrow the set of flags that get attached to a captured event, use the in-memory
helpers `only` and `only_accessed`. To narrow the set of flags requested
from the server in the first place, pass `flag_keys` to `Client.evaluate_flags`.

#### `FeatureFlagEvaluations.is_enabled`

Return whether the flag is enabled. Fires `$feature_flag_called` on the
first access per (distinct_id, flag, value) tuple, deduped via the SDK's cache.

A flag that has a value always wins over `default_value`, including a
disabled flag, which returns `False` even when `default_value=True`.

```python
is_enabled(
    key: str,
    default_value: bool = False,
    send_feature_flag_events: bool = True,
) -> bool
```

<details>
<summary>Parameters (3)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The feature flag key. |
| `default_value`<br />**Type:** `bool` · **Default:** `False`<br />Returned when the flag has no value in this evaluation — the key was not part of the evaluated set, or the evaluation itself came back empty (failed `/flags` request, quota limit, or no resolvable `distinct_id`). Defaults to `False`. |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `True`<br />Whether this read sends `$feature_flag_called`. When `False`, no event is sent and the dedupe cache is left alone, but the key still counts as accessed for `only_accessed`. |

</details>

**Returns** `bool`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flag_evaluations.py#L164)

#### `FeatureFlagEvaluations.get_flag`

Return the flag value. Fires `$feature_flag_called` on first access.

Returns the variant string for multivariate flags, `True` for enabled flags
without a variant, `False` for disabled flags, and `None` for flags that
were not returned by the evaluation.

Pass `send_feature_flag_events=False` to skip the event for this read, as
with `is_enabled`.

```python
get_flag(key: str, send_feature_flag_events: bool = True) -> Optional[FlagValue]
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required** |
| `send_feature_flag_events`<br />**Type:** `bool` · **Default:** `True` |

</details>

**Returns** `Optional[FlagValue]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flag_evaluations.py#L191)

#### `FeatureFlagEvaluations.get_flag_payload`

Return the payload associated with a flag.

Does not count as an access for `only_accessed` and does not fire any event.

```python
get_flag_payload(key: str) -> Optional[Any]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required** |

</details>

**Returns** `Optional[Any]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flag_evaluations.py#L211)

#### `FeatureFlagEvaluations.only_accessed`

Return a filtered copy containing only flags accessed via `is_enabled`
or `get_flag` before this call.

Order-dependent: if nothing has been accessed yet, the returned snapshot is
empty. The method honors its name — pre-access if you want a populated result.

```python
only_accessed() -> FeatureFlagEvaluations
```

**Returns** `FeatureFlagEvaluations`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flag_evaluations.py#L219)

#### `FeatureFlagEvaluations.only`

Return a filtered copy containing only flags with the given keys. Keys that
are not present in the evaluation are dropped and logged as a warning.

```python
only(keys: List[str]) -> FeatureFlagEvaluations
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `keys`<br />**Type:** `List[str]` · **Required** |

</details>

**Returns** `FeatureFlagEvaluations`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flag_evaluations.py#L229)

#### `FeatureFlagEvaluations.keys`

Return the flag keys that are part of this evaluation.

```python
FeatureFlagEvaluations.keys: List[str]
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flag_evaluations.py#L249)

#### `FeatureFlagEvaluations.unresolved_flags`

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

```python
FeatureFlagEvaluations.unresolved_flags: Mapping[str, UnresolvedFlagReason]
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flag_evaluations.py#L254)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flag_evaluations.py#L110)

### `FeatureFlagResult`

The result of calling a feature flag which includes the flag result, variant, and payload.

<details>
<summary>Fields (5)</summary>

| Field |
| --- |
| `key`<br />**Type:** `str`<br />The unique identifier of the feature flag. |
| `enabled`<br />**Type:** `bool`<br />Whether the feature flag is enabled for the current context. |
| `variant`<br />**Type:** `Optional[str]`<br />The variant value if the flag is enabled and has variants, None otherwise. |
| `payload`<br />**Type:** `Optional[Any]`<br />Additional data associated with the feature flag, if any. |
| `reason`<br />**Type:** `Optional[str]`<br />A description of why the flag was enabled or disabled, if available. |

</details>

#### `FeatureFlagResult.get_value`

Returns the value of the flag. This is the variant if it exists, otherwise the enabled value.
This is the value we report as `$feature_flag_response` in the `$feature_flag_called` event.

```python
get_value() -> FlagValue
```

**Returns** `FlagValue`. Either a string variant or boolean value representing the flag's state.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L582)

#### `FeatureFlagResult.from_value_and_payload`

Creates a FeatureFlagResult from a flag value and payload.

```python
from_value_and_payload(
    key: str,
    value: Union[FlagValue, None],
    payload: Any,
) -> Union[FeatureFlagResult, None]
```

<details>
<summary>Parameters (3)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required**<br />The unique identifier of the feature flag. |
| `value`<br />**Type:** `Union[FlagValue, None]` · **Required**<br />The value of the flag (string variant or boolean). |
| `payload`<br />**Type:** `Any` · **Required**<br />Additional data associated with the feature flag. |

</details>

**Returns** `Union[FeatureFlagResult, None]`. Union[FeatureFlagResult, None]: A new FeatureFlagResult instance, or None if value is None.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L592)

#### `FeatureFlagResult.from_flag_details`

Create a FeatureFlagResult from a FeatureFlag object.

```python
from_flag_details(
    details: Union[FeatureFlag, None],
    override_match_value: Optional[FlagValue] = None,
) -> FeatureFlagResult | None
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `details`<br />**Type:** `Union[FeatureFlag, None]` · **Required**<br />The FeatureFlag object to convert. |
| `override_match_value`<br />**Type:** `Optional[FlagValue]` · **Default:** `None`<br />If provided, this value will be used to populate the enabled and variant fields instead of the values from the FeatureFlag. |

</details>

**Returns** `FeatureFlagResult | None`. FeatureFlagResult | None: A new FeatureFlagResult instance, or None if details is None.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L618)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L563)

### `FeatureFlag`

Detailed feature flag evaluation returned by the flags API.

`enabled`, `variant` and `metadata.payload` are the value rendered the way
responses before `/flags?v=4` carried it, which the legacy getters return.

<details>
<summary>Fields (5)</summary>

| Field |
| --- |
| `key`<br />**Type:** `str`<br />Feature flag key. |
| `enabled`<br />**Type:** `bool`<br />Whether the flag is enabled for the evaluated user or group. |
| `variant`<br />**Type:** `Optional[str]`<br />Variant key for multivariate flags, otherwise `None`. |
| `reason`<br />**Type:** `Optional[FlagReason]`<br />Optional reason metadata explaining the result. |
| `metadata`<br />**Type:** `Union[FlagMetadata, LegacyFlagMetadata]`<br />Payload and other metadata returned by the API. |

</details>

#### `FeatureFlag.get_value`

Return the variant for a multivariate flag, otherwise whether the flag is enabled.

```python
get_value() -> FlagValue
```

**Returns** `FlagValue`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L285)

#### `FeatureFlag.from_json`

Parse one flag record of a `/flags` response.

A record with a `value` member is a `/flags?v=4` record. A record
without one comes from an older server: its value is `variant`, or
`enabled` when there is no variant, and its config version is `1`.

```python
from_json(resp: Any) -> FeatureFlag
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `resp`<br />**Type:** `Any` · **Required** |

</details>

**Returns** `FeatureFlag`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L289)

#### `FeatureFlag.from_value_and_payload`

Build a flag from its value and payload. A string value is a variant, so the flag is enabled.

```python
from_value_and_payload(key: str, value: FlagValue, payload: Any) -> FeatureFlag
```

<details>
<summary>Parameters (3)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required** |
| `value`<br />**Type:** `FlagValue` · **Required** |
| `payload`<br />**Type:** `Any` · **Required** |

</details>

**Returns** `FeatureFlag`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L299)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L245)

### `FlagsAndPayloads`

Feature flag values and payloads keyed by feature flag key.

<details>
<summary>Fields (2)</summary>

| Field |
| --- |
| `featureFlags`<br />**Type:** `Optional[dict[str, FlagValue]]`<br />Each flag's value, keyed by flag key. |
| `featureFlagPayloads`<br />**Type:** `Optional[dict[str, Any]]`<br />Each flag's payload, keyed by flag key. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L551)

### `FeatureFlagEvaluationRuntime`

Where a feature flag is meant to be evaluated.

Set per flag in PostHog and carried on every locally cached flag definition.
`ALL` means the flag suits both client-side and server-side evaluation, so
it matches either runtime. Inheriting from `str` keeps the members directly
comparable to their `"all"` / `"client"` / `"server"` values.

<details>
<summary>Fields (3)</summary>

| Field |
| --- |
| `ALL`<br />**Value:** `'all'`<br />The flag suits both client-side and server-side evaluation. |
| `CLIENT`<br />**Value:** `'client'`<br />The flag is meant for client-side evaluation. |
| `SERVER`<br />**Value:** `'server'`<br />The flag is meant for server-side evaluation. |

</details>

#### `FeatureFlagEvaluationRuntime.from_value`

Coerce a raw `evaluation_runtime` value to a member.

A missing, null or unrecognized value becomes `ALL`, which is the
default PostHog applies to a flag that does not set a runtime.

```python
from_value(value: Any) -> FeatureFlagEvaluationRuntime
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `value`<br />**Type:** `Any` · **Required** |

</details>

**Returns** `FeatureFlagEvaluationRuntime`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L98)

#### `FeatureFlagEvaluationRuntime.matches`

Whether a flag set to one of these runtimes suits the other runtime.

`ALL` matches every runtime, so the check is symmetric.

```python
matches(other: FeatureFlagEvaluationRuntime) -> bool
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `other`<br />**Type:** `FeatureFlagEvaluationRuntime` · **Required** |

</details>

**Returns** `bool`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L112)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L80)

### `UnresolvedFlagReason`

Why local evaluation left a flag without a value.

Reported by `FeatureFlagEvaluations.unresolved_flags`. Inheriting from
`str` keeps the members directly comparable to their string values. When more
than one cause applies, the first reason in the order below is reported.

<details>
<summary>Fields (4)</summary>

| Field |
| --- |
| `EXPERIENCE_CONTINUITY`<br />**Value:** `'experience_continuity'`<br />The flag has experience continuity enabled, which local evaluation never resolves. Change the flag to fix it. |
| `UNSUPPORTED_DEFINITION`<br />**Value:** `'unsupported_definition'`<br />The flag uses something the local evaluator does not support, such as a static cohort, an unknown operator, a malformed value, a dependency with no loaded definition or a circular dependency. Change the flag or upgrade the SDK to fix it. |
| `UNRESOLVED_DEPENDENCY`<br />**Value:** `'unresolved_dependency'`<br />A flag this flag depends on has a loaded definition but was unresolved. |
| `MISSING_CONTEXT`<br />**Value:** `'missing_context'`<br />The call did not pass a property, group property or device id the flag needs, or passed it in an unusable form. Pass it to fix it. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/types.py#L120)

### `FlagDefinitionCacheProvider`

Interface for external caching of feature flag definitions.

Enables multi-worker environments to share flag definitions, reducing API
calls while ensuring all workers have consistent data.

Methods may be implemented as either synchronous functions or async
functions. If a method returns an awaitable, the SDK runs it to completion
before continuing.

The four methods handle the complete lifecycle of flag definition caching:

1. `should_fetch_flag_definitions()` - Called before each poll to determine
   if this worker should fetch new definitions. Use for distributed lock
   coordination to ensure only one worker fetches at a time.

2. `get_flag_definitions()` - Called when `should_fetch_flag_definitions()`
   returns False. Returns cached definitions if available.

3. `on_flag_definitions_received()` - Called after successfully fetching
   new definitions from the API. Store the data in your external cache
   and release any locks.

4. `shutdown()` - Called when the PostHog client shuts down. Release any
   distributed locks and clean up resources.

#### `FlagDefinitionCacheProvider.get_flag_definitions`

Retrieve cached flag definitions.

```python
get_flag_definitions(
) -> Union[Optional[FlagDefinitionCacheData], Awaitable[Optional[FlagDefinitionCacheData]]]
```

**Returns** `Union[Optional[FlagDefinitionCacheData], Awaitable[Optional[FlagDefinitionCacheData]]]`. Cached flag definitions if available and valid, None otherwise. May return an awaitable resolving to the same value. Returning None will trigger a fetch from the API if this worker has no flags loaded yet.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/flag_definition_cache.py#L93)

#### `FlagDefinitionCacheProvider.should_fetch_flag_definitions`

Determine whether this instance should fetch new flag definitions.

Use this for distributed lock coordination. Only one worker should
return True to avoid thundering herd problems. A typical implementation
uses a distributed lock (e.g., Redis SETNX) that expires after the
poll interval.

```python
should_fetch_flag_definitions() -> Union[bool, Awaitable[bool]]
```

**Returns** `Union[bool, Awaitable[bool]]`. True if this instance should fetch from the API, False otherwise. May return an awaitable resolving to the same value. When False, the client will call `get_flag_definitions()` to retrieve cached data instead.

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/flag_definition_cache.py#L109)

#### `FlagDefinitionCacheProvider.on_flag_definitions_received`

Called after successfully receiving new flag definitions from PostHog.

Use this to store the data in your external cache and release any
distributed locks acquired in `should_fetch_flag_definitions()`.

```python
on_flag_definitions_received(data: FlagDefinitionCacheData) -> Optional[Awaitable[None]]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `data`<br />**Type:** `FlagDefinitionCacheData` · **Required**<br />The flag definitions to cache, containing flags, group_type_mapping, and cohorts. |

</details>

**Returns** `Optional[Awaitable[None]]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/flag_definition_cache.py#L126)

#### `FlagDefinitionCacheProvider.shutdown`

Called when the PostHog client shuts down.

Use this to release any distributed locks and clean up resources.
This method is called even if `should_fetch_flag_definitions()`
returned False, so implementations should handle the case where
no lock was acquired.

```python
shutdown() -> Optional[Awaitable[None]]
```

**Returns** `Optional[Awaitable[None]]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/flag_definition_cache.py#L141)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/flag_definition_cache.py#L56)

### `FlagDefinitionCacheData`

Data structure for cached flag definitions.

<details>
<summary>Fields (5)</summary>

| Field |
| --- |
| `flags`<br />**Type:** `Required[List[Dict[str, Any]]]`<br />List of feature flag definition dictionaries from the API. |
| `group_type_mapping`<br />**Type:** `Required[Dict[str, str]]`<br />Mapping of group type indices to group names. |
| `cohorts`<br />**Type:** `Required[Dict[str, Any]]`<br />Dictionary of cohort definitions for local evaluation. |
| `minimal_flag_called_events`<br />**Type:** `NotRequired[bool]`<br />Server-controlled gate for minimal `$feature_flag_called` events. Treated as False when absent. |
| `property_matching_version`<br />**Type:** `NotRequired[int]`<br />Exact/is_not matching selector. Missing means legacy; only version 2 enables explicit matching. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/flag_definition_cache.py#L35)

### `InconclusiveMatchError`

Raised when local evaluation can't determine a flag's value. `reason` says
why, as reported by `FeatureFlagEvaluations.unresolved_flags`.

<details>
<summary>Fields (1)</summary>

| Field |
| --- |
| `reason`<br />**Type:** `UnresolvedFlagReason`<br />Why local evaluation couldn't resolve the flag. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flags.py#L98)

### `RequiresServerEvaluation`

Raised when feature flag evaluation requires server-side data that is not
available locally (e.g., static cohorts).

This error should propagate immediately to trigger API fallback, unlike
InconclusiveMatchError which allows trying other conditions.

<details>
<summary>Fields (1)</summary>

| Field |
| --- |
| `reason`<br />**Type:** `UnresolvedFlagReason`<br />Why the flag needs server-side evaluation. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/feature_flags.py#L115)

### `CaptureCompression`

Selects the request-body compression for capture-v1 uploads.

Only honored when `capture_mode` is `V1`; the legacy `/batch/` path
keeps using its own `gzip` flag. `NONE` sends the body uncompressed.
`GZIP` and `DEFLATE` (zlib, RFC 1950) are both stdlib / zero-dependency;
`ZSTD` is faster and compresses better but needs the optional zstandard
package (`pip install posthog[zstd]`) until stdlib support lands in
Python 3.14. Each maps to the matching `Content-Encoding` token the v1
server decodes (`br` is accepted by the server too but is intentionally
left out for now). Inheriting from `str` keeps the members comparable to
and serializable as their token values.

<details>
<summary>Fields (4)</summary>

| Field |
| --- |
| `NONE`<br />**Value:** `'none'`<br />Sends the body uncompressed. |
| `GZIP`<br />**Value:** `'gzip'`<br />gzip, from the standard library. |
| `DEFLATE`<br />**Value:** `'deflate'`<br />zlib (RFC 1950), from the standard library. |
| `ZSTD`<br />**Value:** `'zstd'`<br />Faster and smaller, but needs `pip install posthog[zstd]`. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/capture_compression.py#L21)

### `Span`

A handle to a span.

Every method is safe to call on any handle, including after `end()` and
on the inert handles returned when tracing is off, so calling code never
branches on whether tracing is running. Entering a handle (`with span:`)
makes it the active span for the block and ends it on exit, recording an
`Exception` raised inside it. A `BaseException` that is not an
`Exception` (`GeneratorExit`, `CancelledError`, `KeyboardInterrupt`)
still ends the span but is not recorded as a failure.

#### `Span.set_attribute`

Set one attribute on the span; last write wins. Ignored after `end()`.

Returns the span, so calls chain. Prefer primitive values: strings,
booleans, integers and floats keep their type. Lists and mappings are
sent as OTLP arrays and maps, but PostHog stores them as serialized
strings. `None` removes the key; anything else is stringified.

```python
set_attribute(key: str, value: Any) -> Span
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `key`<br />**Type:** `str` · **Required** |
| `value`<br />**Type:** `Any` · **Required** |

</details>

**Returns** `Span`

**Correct example.**

```python
span.set_attribute("http.status_code", 200).set_attribute("cache.hit", True)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L21)

#### `Span.set_attributes`

Set several attributes at once; last write wins per key. Ignored after `end()`.

Returns the span, so calls chain. Same encoding as `set_attribute()`.

```python
set_attributes(attributes: Mapping[str, Any]) -> Span
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `attributes`<br />**Type:** `Mapping[str, Any]` · **Required** |

</details>

**Returns** `Span`

**Correct example.**

```python
span.set_attributes({"db.system": "postgresql", "db.rows": 12})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L36)

#### `Span.add_event`

Add a timestamped event to the span. Ignored after `end()`.

`timestamp` defaults to now; it accepts a `datetime`, or seconds
since the epoch as a `float` or `int`. Returns the span, so calls
chain.

```python
add_event(
    name: str,
    attributes: Optional[Mapping[str, Any]] = None,
    timestamp: Union[datetime, float, None] = None,
) -> Span
```

<details>
<summary>Parameters (3)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required** |
| `attributes`<br />**Type:** `Optional[Mapping[str, Any]]` · **Default:** `None` |
| `timestamp`<br />**Type:** `Union[datetime, float, None]` · **Default:** `None` |

</details>

**Returns** `Span`

**Correct example.**

```python
span.add_event("cache.miss", {"key": "user:42"})
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L48)

#### `Span.set_status`

Set the span status to `"ok"` or `"error"`. Ignored after `end()`.

Unset by default. Any other `code` is ignored. `ok` is final for the
scoped form: an `Exception` raised inside `with span:` does not override
it. Returns the span, so calls chain.

```python
set_status(code: str, message: Optional[str] = None) -> Span
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `code`<br />**Type:** `str` · **Required** |
| `message`<br />**Type:** `Optional[str]` · **Default:** `None` |

</details>

**Returns** `Span`

**Correct example.**

```python
span.set_status("error", "upstream timed out")
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L67)

#### `Span.record_exception`

Record an exception as an `exception` event and mark the span `error`.

Ignored after `end()`. The event carries `exception.type`,
`exception.message` and, for a raised exception,
`exception.stacktrace` (its last `max_attribute_value_length`
characters, so a chained traceback keeps the outermost exception and
may lose its cause). Returns the span, so calls chain. Inside
`with span:` a raised `Exception` is recorded automatically, so
this is for exceptions that are caught and handled.

```python
record_exception(exception: BaseException) -> Span
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `exception`<br />**Type:** `BaseException` · **Required** |

</details>

**Returns** `Span`

**Correct example.**

```python
try:
    charge(card)
except PaymentError as e:
    span.record_exception(e)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L81)

#### `Span.update_name`

Rename the span, for a name only known after it started. Ignored after `end()`.

Returns the span, so calls chain.

```python
update_name(name: str) -> Span
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required** |

</details>

**Returns** `Span`

**Correct example.**

```python
span = posthog.start_span("http.request")
span.update_name(f"{request.method} {route.pattern}")
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L102)

#### `Span.traceparent`

The W3C `traceparent` header value to propagate, or `None`.

```python
traceparent() -> Optional[str]
```

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L115)

#### `Span.tracestate`

The W3C `tracestate` header value to propagate, or `None`.

```python
tracestate() -> Optional[str]
```

**Returns** `Optional[str]`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L119)

#### `Span.end`

End the span and queue it for export. Idempotent.

`end_time` defaults to now; it accepts a `datetime`, or seconds
since the epoch as a `float` or `int`, and is never earlier than
the start. Later calls, and later mutations, no-op. `with span:`
ends the span on exit, so call this only for spans started manually.

```python
end(end_time: Union[datetime, float, None] = None) -> None
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `end_time`<br />**Type:** `Union[datetime, float, None]` · **Default:** `None` |

</details>

**Returns** `None`

**Correct example.**

```python
span = posthog.start_span("job")
try:
    run_job()
finally:
    span.end()
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L123)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/tracing/span.py#L9)

### `PostHogMetrics`

The `client.metrics` API: `count`, `gauge`, `histogram`, `flush`.

Thread-safe; safe to call from hot paths. Configure via the `metrics`
client option (`flush_interval` is in seconds, matching the client's own
`flush_interval` — unlike posthog-js, whose `flushIntervalMs` is milliseconds):

```python
client = Client("phc_...", metrics={"service_name": "billing-worker"})
client.metrics.count("invoices.processed", 1, attributes={"plan": "pro"})
client.metrics.gauge("queue.depth", 42)
client.metrics.histogram("job.duration", 187, unit="ms")
```

#### `PostHogMetrics.count`

Record an increment for a monotonic counter (things that only go up).

```python
count(
    name: str,
    value: float = 1,
    unit: Optional[str] = None,
    attributes: Optional[dict] = None,
) -> None
```

<details>
<summary>Parameters (4)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required** |
| `value`<br />**Type:** `float` · **Default:** `1` |
| `unit`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `attributes`<br />**Type:** `Optional[dict]` · **Default:** `None` |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/metrics_capture.py#L374)

#### `PostHogMetrics.gauge`

Record the current value of something that goes up and down.

```python
gauge(
    name: str,
    value: float,
    unit: Optional[str] = None,
    attributes: Optional[dict] = None,
) -> None
```

<details>
<summary>Parameters (4)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required** |
| `value`<br />**Type:** `float` · **Required** |
| `unit`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `attributes`<br />**Type:** `Optional[dict]` · **Default:** `None` |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/metrics_capture.py#L384)

#### `PostHogMetrics.histogram`

Record one observation of a distribution (durations, sizes).

```python
histogram(
    name: str,
    value: float,
    unit: Optional[str] = None,
    attributes: Optional[dict] = None,
) -> None
```

<details>
<summary>Parameters (4)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required** |
| `value`<br />**Type:** `float` · **Required** |
| `unit`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `attributes`<br />**Type:** `Optional[dict]` · **Default:** `None` |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/metrics_capture.py#L394)

#### `PostHogMetrics.flush`

Sends everything aggregated so far without waiting for the flush interval.

```python
flush() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/metrics_capture.py#L404)

#### `PostHogMetrics.reset`

Clears the flush timer and drops the current window.

```python
reset() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/metrics_capture.py#L409)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/metrics_capture.py#L297)

### `Emails`

Sends transactional email from PostHog email templates.

`host` is your PostHog app host. PostHog Cloud ingestion hosts map to the
app host, so `us.i.posthog.com` sends to `us.posthog.com`. Any other
host is used as given.

Each send retries network errors, rate limits and an unavailable service up
to `max_retries` times, with the same idempotency key. Set
`max_retries=0` when a job runner already retries the send. `timeout`
is in seconds, per request. Pass `session` to send through your own
`requests.Session`, for example a test double.

#### `Emails.send`

Queue one email from a transactional email template.

```python
send(
    to: str,
    template_id: Union[str, UUID],
    template_data: Optional[Mapping[str, Any]] = None,
    distinct_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    max_retries: Optional[int] = None,
) -> SendEmailResult
```

<details>
<summary>Parameters (6)</summary>

| Parameter |
| --- |
| `to`<br />**Type:** `str` · **Required**<br />One recipient email address. |
| `template_id`<br />**Type:** `Union[str, UUID]` · **Required**<br />The UUID of a template made transactional in PostHog. |
| `template_data`<br />**Type:** `Optional[Mapping[str, Any]]` · **Default:** `None`<br />Values for the template's `{{ variables.* }}` placeholders. Values are encoded the way capture encodes properties. |
| `distinct_id`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The recipient's distinct ID in your product. The email's events go to this person. Without it, they go to a person whose distinct ID is the email address. |
| `idempotency_key`<br />**Type:** `Optional[str]` · **Default:** `None`<br />1 to 255 visible ASCII characters. A send with a key that PostHog has already accepted queues nothing and returns the first send's result. Without it, the client makes a key that covers its own retries. Pass a key derived from what the email is for, such as `welcome:user_123`, to cover retries outside this call, too. |
| `max_retries`<br />**Type:** `Optional[int]` · **Default:** `None`<br />Overrides the client's maximum number of retries for this send. `0` turns retries off. |

</details>

**Returns** `SendEmailResult`. The queued email. A result means PostHog queued the email, not that it was delivered.

**Raises** `EmailError`. PostHog did not queue the email, or the client could
not tell whether it did.

**Raises** `(TypeError, ValueError)`. An argument has the wrong type or format.

**Correct example.**

```python
emails.send(
    to="user@example.com",
    template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b",
    template_data={"name": "Ada"},
    distinct_id="user_123",
    idempotency_key="welcome:user_123",
)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L588)

#### `Emails.get`

Read what happened to an email that `send` queued.

```python
get(message_id: Union[str, UUID]) -> EmailStatus
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `message_id`<br />**Type:** `Union[str, UUID]` · **Required**<br />The `id` of the `SendEmailResult` that `send` returned. |

</details>

**Returns** `EmailStatus`. The email's latest status, for 30 days after the send.

**Raises** `EmailError`. PostHog did not answer with a status. `code` is
`not_found` for an unknown ID, an email older than 30 days,
or for a few seconds after the send.

**Raises** `ValueError`. `message_id` is not a message ID.

**Correct example.**

```python
result = emails.send(to="user@example.com", template_id="...")
status = emails.get(result.id)
if status.status == "bounced":
    print(status.reason)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L659)

#### `Emails.close`

Close the HTTP connections this client opened. A `session` you passed in stays open.

```python
close() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L737)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L553)

### `AsyncEmails`

The async counterpart of `Emails`. Needs `posthog[async]`.

Pass `http_client` to send through your own `httpx.AsyncClient`, for
example one with an `httpx.MockTransport` in tests.

#### `AsyncEmails.send`

Queue one email from a transactional email template.

Takes the same arguments and raises the same errors as
`Emails.send`.

```python
async send(
    to: str,
    template_id: Union[str, UUID],
    template_data: Optional[Mapping[str, Any]] = None,
    distinct_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    max_retries: Optional[int] = None,
) -> SendEmailResult
```

<details>
<summary>Parameters (6)</summary>

| Parameter |
| --- |
| `to`<br />**Type:** `str` · **Required** |
| `template_id`<br />**Type:** `Union[str, UUID]` · **Required** |
| `template_data`<br />**Type:** `Optional[Mapping[str, Any]]` · **Default:** `None` |
| `distinct_id`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `idempotency_key`<br />**Type:** `Optional[str]` · **Default:** `None` |
| `max_retries`<br />**Type:** `Optional[int]` · **Default:** `None` |

</details>

**Returns** `SendEmailResult`

**Correct example.**

```python
await emails.send(
    to="user@example.com",
    template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b",
    template_data={"name": "Ada"},
    distinct_id="user_123",
)
```

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L792)

#### `AsyncEmails.get`

Read what happened to an email that `send` queued.

Takes the same argument and raises the same errors as
`Emails.get`.

```python
async get(message_id: Union[str, UUID]) -> EmailStatus
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `message_id`<br />**Type:** `Union[str, UUID]` · **Required** |

</details>

**Returns** `EmailStatus`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L838)

#### `AsyncEmails.aclose`

Close the HTTP connections this client opened. An `http_client` you passed in stays open.

```python
async aclose() -> None
```

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L899)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L765)

### `EmailError`

PostHog did not queue the email, or the client could not tell whether it did.

Branch on `code`, PostHog's stable error code, or on `name`, a coarse
category of the failure. The message never contains the secret key or the
request body.

<details>
<summary>Fields (9)</summary>

| Field |
| --- |
| `name`<br />A coarse category of the failure. |
| `code`<br />PostHog's stable error code. |
| `message`<br />What went wrong. It never contains the secret key or the request body. |
| `status_code`<br />The HTTP status of the response, or `None` when none arrived. |
| `why`<br />PostHog's explanation of the failure, when it gave one. |
| `fix`<br />What to change to fix it, when PostHog said. |
| `docs_url`<br />A docs page about the error, when PostHog gave one. |
| `field`<br />The request field the error is about, when there is one. |
| `retry_after`<br />Seconds to wait before retrying, when PostHog said. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L122)

### `EmailStatus`

What happened to an email that `send()` queued.

`status` is one of:

- `queued`: the email waits to go out.
- `sent`: PostHog handed the email to the email provider.
- `failed`: the email was not sent, and `reason` says why.
- `delivered`: the recipient's mail server accepted the email.
- `bounced` and `complained`: the recipient's mail provider reported
  them after delivery.

Later SDK versions can see values this list does not have yet.
`reason` says why a failed email was not sent, such as
`provider_error` or `recipient_suppressed`, or gives the bounce type of
a bounced email: `hard`, `soft` or `unknown`. It is `None` otherwise.

<details>
<summary>Fields (5)</summary>

| Field |
| --- |
| `id`<br />**Type:** `str`<br />The email's message ID. |
| `status`<br />**Type:** `str`<br />Where the email is now, one of the values above. |
| `created_at`<br />**Type:** `datetime`<br />When PostHog queued the email. |
| `status_at`<br />**Type:** `datetime`<br />When the email reached its current status. |
| `reason`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Why a failed email was not sent, or a bounced email's bounce type. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L189)

### `SendEmailResult`

The queued email. `id` is its message ID.

<details>
<summary>Fields (3)</summary>

| Field |
| --- |
| `id`<br />**Type:** `str`<br />The email's message ID. |
| `replayed`<br />**Type:** `bool` · **Default:** `False`<br />`True` when the response says PostHog replayed an earlier send for the same `idempotency_key` instead of sending again. |
| `warnings`<br />**Type:** `tuple[EmailWarning, ...]` · **Default:** `()`<br />Warnings PostHog returned about the request. |

</details>

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/emails.py#L173)

### `Prompts`

Fetch and compile LLM prompts from PostHog.

Can be initialized with a PostHog client or with direct options.

#### `Prompts.get`

Fetch a prompt by name from the PostHog API.

When `with_metadata` is `True`, returns a `PromptResult`
with `source`, `name`, `version`, and `config` metadata.  When
omitted or `False`, returns a plain string (deprecated -- will be
removed in a future major version).

```python
get(
    name: str,
    with_metadata: Optional[bool] = None,
    cache_ttl_seconds: Optional[int] = None,
    fallback: Optional[str] = None,
    version: Optional[int] = None,
    label: Optional[str] = None,
) -> Union[str, PromptResult]
```

<details>
<summary>Parameters (6)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `str` · **Required**<br />The name of the prompt to fetch |
| `with_metadata`<br />**Type:** `Optional[bool]` · **Default:** `None`<br />If True, returns a PromptResult with source info. Omitting this parameter is deprecated. |
| `cache_ttl_seconds`<br />**Type:** `Optional[int]` · **Default:** `None`<br />Cache TTL in seconds (defaults to instance default) |
| `fallback`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Fallback prompt to use if fetch fails and no cache available |
| `version`<br />**Type:** `Optional[int]` · **Default:** `None`<br />Specific prompt version to fetch. Mutually exclusive with label. If neither is given, fetches the latest version |
| `label`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Fetch the version this label currently points to, e.g. 'production'. Mutually exclusive with version |

</details>

**Returns** `Union[str, PromptResult]`. str if with_metadata is False/omitted, PromptResult if True

**Raises** `ValueError`. If both version and label are provided

**Raises** `Exception`. If the prompt cannot be fetched and no fallback is available

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/ai/prompts.py#L359)

#### `Prompts.get_all`

Fetch every prompt in one batch.

Returns a dict mapping prompt name to `PromptResult`. With a
label, each prompt is at the version the label points to, and prompts
without the label are not included. Without a label, every prompt is
included at its latest version, matching what `get(name)` returns.

Each fetched prompt is stored in the cache, so later
`get(name, label=...)` (or plain `get(name)`) calls are served
from cache within the TTL. An app with many prompts can call this once
per cache cycle instead of making one `get()` request per prompt.

```python
get_all(label: Optional[str] = None) -> Dict[str, PromptResult]
```

<details>
<summary>Parameters (1)</summary>

| Parameter |
| --- |
| `label`<br />**Type:** `Optional[str]` · **Default:** `None`<br />The label to resolve, e.g. 'production'. Omit to fetch latest versions. |

</details>

**Returns** `Dict[str, PromptResult]`. Dict of prompt name to PromptResult.

**Raises** `Exception`. If the request fails, or a label was passed and the
server does not support fetching prompts by label on the list
endpoint (PostHog releases from before September 2026).

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/ai/prompts.py#L433)

#### `Prompts.compile`

Replace {{variableName}} placeholders with values.

Unmatched variables are left unchanged.
Supports variable names with hyphens and dots (e.g., user-id, company.name).

```python
compile(prompt: str, variables: PromptVariables) -> str
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `prompt`<br />**Type:** `str` · **Required**<br />The prompt template string |
| `variables`<br />**Type:** `PromptVariables` · **Required**<br />Object containing variable values |

</details>

**Returns** `str`. The compiled prompt string

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/ai/prompts.py#L720)

#### `Prompts.clear_cache`

Clear cached prompts.

```python
clear_cache(name: Optional[str] = None, version: Optional[int] = None) -> None
```

<details>
<summary>Parameters (2)</summary>

| Parameter |
| --- |
| `name`<br />**Type:** `Optional[str]` · **Default:** `None`<br />Specific prompt name to clear. If None, clears all cached prompts. |
| `version`<br />**Type:** `Optional[int]` · **Default:** `None`<br />Specific prompt version to clear. Requires name. |

</details>

**Returns** `None`

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/ai/prompts.py#L745)

[Source](https://github.com/PostHog/posthog-python/blob/v7.83.1/posthog/ai/prompts.py#L227)
