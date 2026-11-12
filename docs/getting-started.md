# Getting started

The Python SDK captures events, evaluates feature flags and reports errors from Python 3.10 or later.

To send your first event, install the package, create a client, capture an event, and shut the client down, as the steps below show.

## Install

```bash
pip install posthog
```

## Initialize

Create one client for your app with your project token and host. Your project token and host are in your [project settings](https://app.posthog.com/project/settings). Keep the token in an environment variable rather than in code.

```python
from posthog import Posthog

posthog = Posthog("<ph_project_token>", host="<ph_client_api_host>")
```

## Capture an event

```python
posthog.capture("user_signed_up", distinct_id="user_123", properties={"plan": "free"})
```

`capture` queues the event and returns right away. A background thread sends it.

## Shut down

Call `shutdown()` before your process exits, so queued events are sent.

```python
posthog.shutdown()
```

In an asyncio app, use `AsyncPosthog` as an async context manager instead. See [AsyncClient](#asyncclient).

## Install with the wizard

Run this command in your project directory to add PostHog to your Python project. The wizard detects your framework, installs the SDK, and sets up your first events.

```bash
npx -y @posthog/wizard@latest
```

Or set it up by hand with the steps above.

## Use the asyncio client

The SDK includes an asyncio-native client, available since 7.45.0. Keep using `Posthog` in synchronous apps. For an asyncio app, install the optional async dependencies:

```bash
pip install "posthog[async]>=8,<9"
```

Import `AsyncPosthog`, the customer-facing name for `AsyncClient`. Both names provide the same async context manager and lifecycle methods. Keep one client for the lifetime of your app. For example, use a FastAPI lifespan handler:

```python
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from posthog import AsyncPosthog

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncPosthog(
        os.environ["POSTHOG_PROJECT_TOKEN"],
        host=os.environ["POSTHOG_HOST"],
        secret_key=os.environ.get("POSTHOG_FEATURE_FLAGS_SECURE_API_KEY"),
    ) as posthog:
        app.state.posthog = posthog
        yield

app = FastAPI(lifespan=lifespan)
```

Exiting the context calls `shutdown()`. This flushes buffered events, waits for in-flight operations, stops the workers, and closes the HTTP transport.

If you don't use the context manager, call `await posthog.shutdown()` during app shutdown. `await posthog.join()` has the same effect.

## Capture without blocking

With `AsyncPosthog`, `capture()` queues an event and returns without waiting for a network request. Don't await it:

```python
posthog.capture(
    "event_name",
    distinct_id="user-distinct-id",
    properties={"source": "fastapi"},
)
```

Use `capture_immediate()` when your code must wait for that event's delivery attempt:

```python
capture_id = await posthog.capture_immediate(
    "event_name",
    distinct_id="user-distinct-id",
)
```

## Evaluate flags with asyncio

Await `evaluate_flags()` once, then use its snapshot with synchronous in-memory accessors. Pass the same snapshot to `capture()` to attach the exact values used for branching without another feature flag request:

```python
flags = await posthog.evaluate_flags("user-distinct-id")

if flags.is_enabled("new-checkout"):
    # Show the new checkout
    pass

posthog.capture(
    "checkout started",
    distinct_id="user-distinct-id",
    flags=flags,
)
```

The snapshot provides synchronous `is_enabled()`, `get_flag()`, and `get_flag_payload()` accessors. The awaited `evaluate_flags()` call also accepts `groups`, `person_properties`, `group_properties`, `disable_geoip`, `flag_keys`, and `device_id` arguments.

## Fetch remote config

Initialize the client with a server-side [secret API key](https://posthog.com/docs/feature-flags/remote-config#step-1-get-a-secret-api-key) as `secret_key`, then await the remote config request:

```python
config = await posthog.get_remote_config_payload("landing-page-config")
```

See [Remote config](https://posthog.com/docs/feature-flags/remote-config) for setup and security details.

## Identify users

> **Identifying users is required.** Backend events need a `distinct_id` to associate events with the correct user.

In Python, you can do this through a context. All event captures in the same context are tagged with the correct `distinct_id`. Typically, you set a fresh context and identify at the top of each route:

```python
from posthog import new_context, identify_context, capture

@app.get("/foo")
def foo(current_user: User = Depends(get_current_user)):
    with new_context(): # Set context at the top of a route
        identify_context(current_user.id)
        capture("foo_viewed")
    return {"status": "ok"}
```

When possible, write a small piece of middleware that resolves your authenticated user, wraps a context around the request, and identifies it. Every `capture()` downstream is then attributed to that user. The SDK's Django middleware does this for you, and you can replicate it in plain Python.

## Send custom events

Send custom events with `capture`. Events captured with no context and no explicit `distinct_id` are marked as personless and get an auto-generated `distinct_id`. The [Capture](#capture) entry has the examples.

> **Name events as `[object] [verb]`.** `[object]` is the entity the behavior relates to, and `[verb]` is the behavior itself. For example, `project created`, `user signed up`, or `invite sent`.

You can define event schemas with typed properties and generate type-safe code with [schema management](https://posthog.com/docs/product-analytics/schema-management).

To include more information with an event, pass a [properties](https://posthog.com/docs/data/events#event-properties) dictionary.

If you run PostHog on your backend only and don't capture events from your frontend, send `$pageview` events from your backend with a `$current_url` property.

## Set person properties

The SDK captures identified events if the current context is identified or if you pass a distinct ID explicitly. These create [person profiles](https://posthog.com/docs/data/persons). To set [person properties](https://posthog.com/docs/product-analytics/person-properties) in these profiles, include them when you capture an event:

```python
# Passing a distinct id explicitly
posthog.capture(
    'event_name',
    distinct_id='user-distinct-id',
    properties={
        '$set': {'name': 'Max Hedgehog'},
        '$set_once': {'initial_url': '/blog'}
    }
)

# Using contexts
from posthog import new_context, identify_context
with new_context():
    identify_context('user-distinct-id')
    posthog.capture('event_name')
```

The person properties docs explain [the difference between `$set` and `$set_once`](https://posthog.com/docs/product-analytics/person-properties#what-is-the-difference-between-set-and-set_once).

## Capture anonymous events

To capture [anonymous events](https://posthog.com/docs/data/anonymous-vs-identified-events) without person profiles, set the event's `process_person_profile` option to `False`. Events captured with no context or explicit `distinct_id` are marked as personless, and have an auto-generated `distinct_id`:

```python
posthog.capture(
    event='event_name',
    options={
        'process_person_profile': False
    }
)
```

## Alias a user

Sometimes you want to assign multiple distinct IDs to a single user. This helps when your primary distinct ID is inaccessible. For example, a distinct ID used on the frontend isn't available in your backend.

In this case, use `alias` to assign another distinct ID to the same user:

```python
posthog.alias(previous_id='distinct_id', distinct_id='alias_id')
```

Read the docs on [alias](https://posthog.com/docs/product-analytics/identify#alias-assigning-multiple-distinct-ids-to-the-same-user) before you use this method.

## Use contexts

The SDK uses nested contexts to manage state that's shared across events. Contexts are the recommended way to manage things like "which user is taking this action" (through `identify_context`), rather than passing user state through your app's stack by hand.

When events (including exceptions) are captured in a context, the event uses the user [distinct ID](https://posthog.com/docs/getting-started/identify-users), [session ID](https://posthog.com/docs/data/sessions), and tags that are set in the context. This is useful for adding properties to multiple events during a single user's interaction with your product.

Enter a context with the `with` statement:

```python
from posthog import new_context, tag, set_context_session, identify_context

with new_context():
    tag("transaction_id", "abc123")
    tag("some_arbitrary_value", {"tags": "can be dicts"})

    # Sessions are UUIDv7 values and used to track a sequence of events that occur within a single user session
    # See https://posthog.com/docs/data/sessions
    set_context_session(session_id)

    # Setting the context-level distinct ID. See below for more details.
    identify_context(user_id)

    # This event is captured with the distinct ID, session ID, and tags set above
    posthog.capture("order_processed")
```

Contexts persist across function calls. If you enter one, call a function, and capture an event in the called function, the event uses the tags and session ID set in the parent context:

```python
from posthog import new_context, tag

def some_function():
    # When called from `outer_function`, this event is captured with the property some-key="value-4"
    posthog.capture("order_processed")


def outer_function():
    with new_context():
        tag("some-key", "value-4")
        some_function()

```

Contexts are nested, so child contexts inherit tags added to a parent context. If you set the same tag in both a parent and a child context, the child's value overrides the parent's at event capture, and the parent context isn't affected. This nesting also applies to session IDs and distinct IDs.

```python
from posthog import new_context, tag

with new_context():
    tag("some-key", "value-1")
    tag("some-other-key", "another-value")
    with new_context():
        tag("some-key", "value-2")
        # This event is captured with some-key="value-2" and some-other-key="another-value"
        posthog.capture("order_processed")

    # This event is captured with some-key="value-1" and some-other-key="another-value"
    posthog.capture("order_processed")

```

To turn off this nesting, pass `fresh=True` to `new_context`:

```python
from posthog import new_context, tag

with new_context(fresh=True):
    tag("some-key", "value-2")
    # This event only has the property some-key="value-2" from the fresh context
    posthog.capture("order_processed")

```

> **Direct arguments win.** Distinct IDs, session IDs, and properties passed directly to `capture` and related functions override context state in the final event.

## Identify a context

Associate a context with a distinct ID by calling `posthog.identify_context`:

```python
from posthog import identify_context

identify_context("distinct-id")
```

Within a context associated with a distinct ID, all captured events are associated with that user. To override the distinct ID for a specific event, pass a `distinct_id` argument to `capture`:

```python
from posthog import new_context, identify_context

with new_context():
    identify_context("distinct-id")
    posthog.capture("order_processed") # will be associated with distinct-id
    posthog.capture("order_processed", distinct_id="another-distinct-id") # will be associated with another-distinct-id
```

Pass the currently active distinct ID from the frontend to the backend with the `X-POSTHOG-DISTINCT-ID` header. The Django middleware extracts it and associates it with the request handler context for you.

Read more in the [user identification docs](https://posthog.com/docs/product-analytics/identify).

## Link a session

Associate a context with a session ID by calling `posthog.set_context_session`. To link backend events to frontend sessions, use the session ID from the frontend SDK. PostHog session IDs are UUIDv7 strings.

```python
from posthog import new_context, set_context_session
with new_context():
    set_context_session(request.get_header("X-POSTHOG-SESSION-ID"))
```

> **Using PostHog on your frontend too?** The JavaScript Web SDK generates a session ID for you. Configure [`tracing_headers`](https://posthog.com/docs/libraries/js/reference#add-tracing-headers) for your backend hostname to add the session and distinct ID headers to browser requests. You then extract the header in your request handler. The Django middleware does this for you.

A context with a session gives you two things:

- **Backend events on the timeline.** You see them on the session timeline when you watch session replays.
- **Replays for backend errors.** You can watch the session replay of a user who triggered a backend exception in error tracking.

Read more in the [sessions docs](https://posthog.com/docs/data/sessions).

## Capture context exceptions

By default, exceptions raised within a context are captured and show up in [error tracking](https://posthog.com/docs/error-tracking). To turn this off, pass `capture_exceptions=False` to `new_context`:

```python
from posthog import new_context, tag

with new_context(capture_exceptions=False):
    tag("transaction_id", "abc123")
    tag("some_arbitrary_value", {"tags": "can be dicts"})

    # This event will be captured with the tags set above
    posthog.capture("order_processed")
    # This exception will not be captured
    raise Exception("Order processing failed")
```

## Decorate a function

The `scoped` decorator marks a whole function as being in a new context. It takes the same `fresh` and `capture_exceptions` arguments as `new_context`:

```python
from posthog import scoped, identify_context

@scoped(fresh=True)
def process_order(user, order_id):
    identify_context(user.distinct_id)
    posthog.capture("order_processed") # Associated with the user
    raise Exception("Order processing failed") # This exception is also captured and associated with the user
```

## Use group analytics

Group analytics lets you associate an event with a group, like a team or an organization. Read the [group analytics guide](https://posthog.com/docs/product-analytics/group-analytics) for more.

> **Group analytics is a paid feature.** It isn't available on the free cloud plan. See the [pricing page](https://posthog.com/pricing).

To capture an event and associate it with a group:

```python
posthog.capture('some_event', groups={'company': 'company_id_in_your_db'})
```

To update properties on a group:

```python
posthog.group_identify('company', 'company_id_in_your_db', {
    'name': 'Awesome Inc.',
    'employees': 11
})
```

`name` is a special property. The PostHog UI uses it as the name of the group. If you don't set a `name` property, the UI shows the group ID instead.

## Evaluate feature flags

PostHog's [feature flags](https://posthog.com/docs/feature-flags) let you safely deploy and roll back new features, and target specific users and groups with them.

The examples in the feature flag sections use the synchronous `Posthog` client. For `AsyncPosthog`, see [Evaluate flags with asyncio](#evaluate-flags-with-asyncio). The returned snapshot uses the same accessors.

Call `posthog.evaluate_flags()` once for the user, then read values from the returned snapshot.

For a boolean feature flag:

```python
flags = posthog.evaluate_flags("distinct_id_of_your_user")

if flags.is_enabled("flag-key"):
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = flags.get_flag_payload("flag-key")
```

For a multivariate feature flag:

```python
flags = posthog.evaluate_flags("distinct_id_of_your_user")

enabled_variant = flags.get_flag("flag-key")

if enabled_variant == "variant-key":  # replace "variant-key" with the key of your variant
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = flags.get_flag_payload("flag-key")
```

`flags.get_flag()` returns the variant string for multivariate flags, `True` for enabled boolean flags, `False` for disabled flags, and `None` when the evaluation didn't return the flag.

> **Prefer `evaluate_flags()` for new code.** `posthog.feature_enabled()`, `posthog.get_feature_flag()`, `posthog.get_feature_flag_payload()`, and `posthog.capture(send_feature_flags=True)` still work during the migration period, but they're deprecated.

## Attach flags to events

To break down or filter events by a feature flag in your [insights](https://posthog.com/docs/product-analytics/insights), include feature flag information in those events. This attributes the flag value correctly to the event.

> **Server-side only.** This step is only required for events captured with the server-side SDKs or the [API](https://posthog.com/docs/api).

There are two ways to do it. The first is to pass the same `flags` object that you used for branching to `capture()`. This attaches the exact flag values from that evaluation and doesn't make another `/flags` request.

```python
flags = posthog.evaluate_flags("distinct_id_of_your_user")

if flags.is_enabled("flag-key"):
    # Do something differently for this user
    pass

posthog.capture(
    "event_name",
    distinct_id="distinct_id_of_your_user",
    flags=flags,
)
```

By default, this attaches every flag in the snapshot as `$feature/<flag-key>` properties and `$active_feature_flags`.

To reduce event property bloat, pass a filtered snapshot:

```python
# Attach only flags accessed with is_enabled() or get_flag() before this call
posthog.capture(
    "event_name",
    distinct_id="distinct_id_of_your_user",
    flags=flags.only_accessed(),
)

# Attach only specific flags
posthog.capture(
    "event_name",
    distinct_id="distinct_id_of_your_user",
    flags=flags.only(["checkout-flow", "new-dashboard"]),
)
```

`only_accessed()` is order-dependent. If you call it before you access any flags with `is_enabled()` or `get_flag()`, no feature flag properties are attached.

The second way is to include a `$feature/feature_flag_name` property by hand, with the variant key as its value:

```python
posthog.capture(
    "event_name",
    distinct_id="distinct_id_of_the_user",
    properties={
        # Replace feature-flag-key with your flag key and "variant-key" with the key of your variant
        "$feature/feature-flag-key": "variant-key",
    },
)
```

## Evaluate specific flags

By default, `posthog.evaluate_flags()` evaluates every flag for the user. If you only need a few flags, pass `flag_keys` to request only those flags:

```python
flags = posthog.evaluate_flags(
    "distinct_id_of_your_user",
    flag_keys=["checkout-flow", "new-dashboard"],
)
```

## The flag called event

`$feature_flag_called` events tell PostHog when a user accessed a flag, which powers [analytics and insights](https://posthog.com/docs/product-analytics/insights) on the flag. With `posthog.evaluate_flags()`, the SDK sends this event when you call `flags.is_enabled()` or `flags.get_flag()` for a flag.

The SDK deduplicates these events per `(distinct_id, flag, value)` in a local cache. If you reinitialize the PostHog client, the cache resets and the SDK may send `$feature_flag_called` events again. PostHog handles duplicates, so they don't affect your analytics.

`flags.get_flag_payload()` doesn't send `$feature_flag_called` events and doesn't count as an access for `only_accessed()`.

## Override flag properties

Sometimes you want to evaluate feature flags with [person properties](https://posthog.com/docs/product-analytics/person-properties), [groups](https://posthog.com/docs/product-analytics/group-analytics), or group properties that haven't been ingested yet, or were set incorrectly earlier.

Pass them in the `person_properties`, `groups`, and `group_properties` arguments. PostHog then evaluates the flag with these values, rather than the properties stored in your project.

```python
flags = posthog.evaluate_flags(
    "distinct_id_of_the_user",
    person_properties={"property_name": "value"},
    groups={
        "your_group_type": "your_group_id",
        "another_group_type": "your_group_id",
    },
    group_properties={
        "your_group_type": {"group_property_name": "value"},
        "another_group_type": {"group_property_name": "value"},
    },
)

if flags.is_enabled("flag-key"):
    # Do something differently for this user
```

## Override GeoIP properties

By default, a user's GeoIP properties come from the IP address they use to capture events on the frontend. You may want to override them when you evaluate feature flags. A common reason is that you don't use PostHog on your frontend, so the user has no GeoIP properties.

To override them, include them in the `person_properties` argument next to your other person properties. This lets your backend evaluate flags with the client's location rather than your server's location.

You can override these GeoIP properties:

- `$geoip_country_code`
- `$geoip_country_name`
- `$geoip_city_name`
- `$geoip_city_confidence`
- `$geoip_continent_code`
- `$geoip_continent_name`
- `$geoip_latitude`
- `$geoip_longitude`
- `$geoip_postal_code`
- `$geoip_subdivision_1_code`
- `$geoip_subdivision_1_name`
- `$geoip_subdivision_2_code`
- `$geoip_subdivision_2_name`
- `$geoip_subdivision_3_code`
- `$geoip_subdivision_3_name`
- `$geoip_time_zone`

## Set a flag timeout

Set `feature_flags_request_timeout_seconds` when you initialize the client to set a flag request timeout. This keeps your code from blocking when PostHog's servers are slow to respond. The default is 3 seconds.

```python
posthog = Posthog(
    "<ph_project_token>",
    host="<ph_client_api_host>",
    feature_flags_request_timeout_seconds=3,  # Time in seconds. Defaults to 3.
)
```

## Evaluate flags locally

Evaluating feature flags takes a request to PostHog. With local evaluation, the SDK requests feature flag definitions on an interval and stores them locally, so you evaluate flags without more requests.

Use local evaluation when you can. It resolves flags faster and with fewer API calls. The [local evaluation guide](https://posthog.com/docs/feature-flags/local-evaluation) shows how to set it up.

In multi-worker or edge environments, you can cache flag definitions in Redis, Cloudflare KV, or another storage backend. This shares definitions across workers and coordinates fetches. See [local evaluation in distributed environments](https://posthog.com/docs/feature-flags/local-evaluation/distributed-environments?tab=Python).

## Run experiments

[Experiments](https://posthog.com/docs/experiments/start-here) use feature flags, so the code for an experiment is close to the feature flag code. This example uses the synchronous `Posthog` client:

```python
flags = posthog.evaluate_flags("user_distinct_id")
variant = flags.get_flag("experiment-feature-flag-key")

if variant == "variant-name":
    # Do something
```

With `AsyncPosthog`, await the evaluation: `flags = await posthog.evaluate_flags("user_distinct_id")`. The rest of the snapshot access is the same.

You can also [run experiments without feature flags](https://posthog.com/docs/experiments/running-experiments-without-feature-flags).

## Capture LLM usage

The SDK includes built-in AI observability. It captures LLM usage, performance, and more. See the [AI observability docs](https://posthog.com/docs/ai-observability) to set it up.

## Track errors

To [autocapture exceptions](https://posthog.com/docs/error-tracking/installation), set `enable_exception_autocapture` to `True` when you initialize the client:

```python
from posthog import Posthog

posthog = Posthog("<ph_project_token>", enable_exception_autocapture=True, ...)
```

You can also capture exceptions by hand with `capture_exception`:

```python
posthog.capture_exception(e, distinct_id='user_distinct_id', properties=additional_properties)
```

Contexts capture exceptions thrown inside them, unless you pass `capture_exceptions=False` to `new_context()`.

## Capture code variables

The SDK can capture the state of local variables when an exception occurs. This gives you a debugger-like view of your app's state at the time of the error:

```python
posthog = Posthog(
    "<ph_project_token>",
    enable_exception_autocapture=True,
    capture_exception_code_variables=True,
)
```

You can configure which variables are captured, masked, or ignored. See the [code variables docs](https://posthog.com/docs/error-tracking/code-variables/python) for the options.

## Trace your services

> **The span API is experimental.** Tracing is new in the SDK, available since 7.58.0, and its API can still change in a minor release. Spans you send are kept. It's the SDK surface that isn't frozen yet.

Tracing records spans, which are timed units of work, so you can see where time went in a request and how work fans out across your services. Spans created inside a [context](#use-contexts) carry the person and session they belong to, so a slow trace links back to the person who experienced it.

Tracing is off until you set the `traces` option. You don't need an OpenTelemetry dependency. For what you can do with spans once they arrive, see [Distributed tracing](https://posthog.com/docs/distributed-tracing/start-here).

```python
from posthog import Posthog

posthog = Posthog(
    "<ph_project_token>",
    host="<ph_client_api_host>",
    traces={"service_name": "checkout-api"},
)
```

If you use the module-level API instead, set `posthog.traces = {"service_name": "checkout-api"}` next to your other options, before you start the first span.

Set `service_name`. PostHog groups operations by service and span name.

Tracing works on the synchronous `Posthog` client and the module-level API. `AsyncPosthog` doesn't support it yet.

## Create spans

`start_span` returns a span. Use it in a `with` block to make it the active span for the block and end it when the block exits. Spans started inside the block nest under it.

```python
with posthog.start_span("POST /checkout", kind="server") as span:
    span.set_attribute("plan", user.plan)

    with posthog.start_span("create-order"):
        order = create_order(cart)
    with posthog.start_span("charge-card"):
        stripe.charge(order)
```

If an exception escapes the block, the span records it, its status is set to `error`, and the exception propagates unchanged. `KeyboardInterrupt`, `GeneratorExit`, and `asyncio.CancelledError` still end the span but aren't recorded as failures.

The recorded exception includes the stack trace, which contains file paths from your server. To keep those inside your process, remove `exception.stacktrace` in [`before_span_send`](#scrub-and-drop-spans).

For work that can't wrap a block, call `start_span` without `with`. **A span started this way isn't active**, so spans started afterwards aren't its children unless you pass `parent` explicitly. You must also call `end()` yourself.

```python
span = posthog.start_span("background-sync", attributes={"queue": "emails"})
try:
    # Explicitly parent a child to a span that isn't active.
    child = posthog.start_span("send-batch", parent=span)
    child.end()
finally:
    span.end()
```

`get_active_span()` returns the span active in the current context, or `None` when there isn't one.

The active span is tracked with [`contextvars`](https://docs.python.org/3/library/contextvars.html), so it carries across `await` in asyncio code. Threads don't reliably inherit it. Pass `parent=span` to continue the trace in a thread you start or a `ThreadPoolExecutor` task. A forked child process starts with no active span, so pass `parent` there too.

`start_span` always returns a usable span, even when tracing is off, so your code never needs to check whether tracing is enabled.

The `kind` argument says what the work is: `"internal"` (the default), `"server"` for an inbound request, `"client"` for an outbound call, and `"producer"` or `"consumer"` for queue work.

`start_time` backdates the span's start. The server clamps a start more than 24 hours old to receive time. With `debug` on, the SDK prints a debug message when you pass one.

## Name spans and attributes

Span names are low-cardinality operation names, like `GET /users/:id`, not `GET /users/123`. Variable values belong in attributes.

Strings, booleans, integers, and floats keep their type. Lists and dictionaries are sent as arrays and maps, but PostHog stores them as serialized strings. Setting an attribute to `None` removes it, and any other value is converted to a string.

```python
with posthog.start_span("GET /users/:id", kind="server") as span:
    span.set_attributes({"user.id": user_id, "db.rows": len(rows)})
    span.add_event("cache-miss")

    if not rows:
        span.set_status("error", "user not found")
```

The [`Span`](#span) entry lists every method. Every method except `traceparent()`, `tracestate()`, and `end()` returns the span, so calls chain. Calls after `end()` are ignored.

Use `record_exception(exception)` for exceptions you catch and handle. Use `update_name(name)` to replace the span name, for example once a route template resolves.

## Trace across services

Spans use [W3C Trace Context](https://www.w3.org/TR/trace-context/), so a trace can span several services. Pass an inbound `traceparent` header as `parent` to continue a trace another service started, and send `span.traceparent()` onward when you call out. For example, in a Flask `app.py`:

```python
import requests
from flask import request


@app.post("/checkout")
def checkout():
    with posthog.start_span(
        "POST /checkout",
        kind="server",
        parent=request.headers.get("traceparent"),
    ) as span:
        traceparent = span.traceparent()

        requests.post(
            "https://payments.internal/charge",
            headers={"traceparent": traceparent} if traceparent else {},
        )

        return {"status": "ok"}
```

A malformed `traceparent` starts a new trace rather than raising. A missing one (`None`) falls back to the active span, if there is one.

A continued trace propagates the sampled flag it was handed, so a downstream sampler sees the decision the head service made. PostHog itself doesn't sample. A span is recorded and exported whichever way that flag is set.

## Link traces to people

Spans created inside a [context](#use-contexts) that has a distinct ID or session ID carry `posthogDistinctId` and `sessionId` attributes. These make a trace reachable from a person or a Session Replay recording.

In Django, the [contexts middleware](https://posthog.com/docs/libraries/django#django-contexts-middleware) sets these for every request. Elsewhere, set them yourself:

```python
from posthog import new_context, identify_context, set_context_session

with new_context():
    identify_context(user.id)
    set_context_session(session_id)

    with posthog.start_span("POST /checkout"):
        process_order()
```

Spans created outside a context with those values omit the attributes.

## Scrub and drop spans

`before_span_send` runs on every finished span before it's queued for export. It receives the span as a dict with `name`, `kind`, `status`, `attributes`, `events`, `start_time_ns`, `end_time_ns`, `trace_id`, `span_id`, and `parent_span_id`. Edit it and return it, or return `None` to drop the span entirely.

```python
def scrub_spans(span):
    if span["attributes"].get("http.route") == "/health":
        return None

    span["attributes"].pop("http.request.header.authorization", None)
    return span


posthog = Posthog(
    "<ph_project_token>",
    host="<ph_client_api_host>",
    traces={
        "service_name": "checkout-api",
        "before_span_send": scrub_spans,
    },
)
```

Attributes are plain Python values, not the OTLP wire encoding. The hook runs after PostHog attaches `posthogDistinctId` and `sessionId`, so the hook can see and scrub those too. An exception's stack trace is on its event, under `event["attributes"]["exception.stacktrace"]`.

- **IDs are read-only.** Rewriting `trace_id`, `span_id`, or `parent_span_id` would orphan child spans that have already been exported, so changes are reverted.
- **A raising hook drops the span.** The span isn't exported without scrubbing.
- **Hooks can chain.** Pass a list to run several hooks in order. The first one to return `None` stops the chain.
- **Hooks are regular functions.** An `async` hook drops every span.
- **A non-callable entry turns tracing off.** The client stops tracing rather than exporting spans the hook was meant to scrub.

## The span limits

A span is capped at 128 attributes and 128 events, each event at 128 attributes, and each string attribute value at 8192 characters. The endpoint rejects a span that's too large, and a rejected span is lost whole rather than truncated, so the caps bound a span before it gets there.

Past the cap, the earliest attributes and events are kept. The number dropped is reported with the span, so a truncated span reads as truncated rather than as quietly incomplete.

The attributes PostHog attaches itself, `posthogDistinctId` and `sessionId`, don't count toward the cap and are never dropped. A span at the limit still links back to its person and session.

The event cap is absolute. An `exception` event the SDK records for you spends an ordinary slot like any other. A span that fills its events and then raises keeps its `error` status but not the exception detail, and reports the loss as a dropped event. Raise `max_events_per_span` on spans that record many events and can also fail.

The length bound reaches inside a value, including strings nested in lists and dictionaries. It applies to `exception.stacktrace` like any other attribute, so a long stack trace keeps its last 8192 characters. All four caps are re-applied after `before_span_send`, so a hook that enriches a span can't push it back over.

## Configure tracing

The `traces` dict takes these options:

| Option | Default | Sets |
|---|---|---|
| `service_name` | Unset | Name of the service producing spans |
| `service_version` | Unset | Version of the service |
| `environment` | Unset | Deployment environment, like `production` |
| `resource_attributes` | Unset | Extra OTLP resource attributes |
| `flush_interval` | `5` | Seconds between exports of queued spans |
| `max_export_batch_size` | `512` | Maximum spans per request |
| `max_queue_size` | `2048` | Maximum spans held in memory |
| `max_live_spans` | `10000` | Maximum spans open at once |
| `max_span_age` | `3600` | Seconds before an open span counts as leaked |
| `before_span_send` | Unset | Hook to edit or drop each finished span |
| `max_attributes_per_span` | `128` | Maximum attributes you set on one span |
| `max_events_per_span` | `128` | Maximum events on one span |
| `max_attribute_value_length` | `8192` | Maximum characters in a string attribute value |

Always set `service_name`. `resource_attributes` takes precedence over `service_name`, `service_version`, and `environment`.

Spans beyond `max_queue_size` are dropped. At the `max_live_spans` limit, `start_span` returns a span that isn't recorded. Once that limit is reached, spans open longer than `max_span_age` seconds are treated as leaked and never exported.

An invalid value falls back to its default with a warning. The exception is `before_span_send`: an entry that isn't callable turns tracing off.

## Flush spans on exit

Spans are exported on a background interval, even with `sync_mode` on. Both `flush()` and `shutdown()` export spans that have already ended.

A span still open at `flush()` is exported once it ends. A span still open at `shutdown()` is discarded with a warning, so end your spans before you shut down. A `with` block does this for you. `shutdown()` gives queued spans up to 30 seconds to send.

If your serverless handler reuses a client across invocations, call `flush()` before returning. It flushes events and ended spans concurrently without stopping the client. Reserve `shutdown()` for a disposable client or process exit.

```python
def handler(event, context):
    try:
        with posthog.start_span("handler"):
            do_work()
    finally:
        posthog.flush()
```

A script that exits without calling `shutdown()` still gets a brief best-effort flush at exit, but don't rely on it for spans you need.

## Enable GeoIP properties

By default, the SDK disregards the server IP, doesn't add GeoIP properties to events, and doesn't use them for feature flag evaluation. This has been the default since 3.0.0. GeoIP properties from a backend are based on your server's IP address rather than the user's, which resolves to the wrong location.

To add GeoIP properties anyway, set `disable_geoip` to `False` when you initialize the client:

```python
posthog = Posthog('api_key', disable_geoip=False)
```

This setting controls these properties:

1. `$geoip_city_name`
2. `$geoip_country_name`
3. `$geoip_country_code`
4. `$geoip_continent_name`
5. `$geoip_continent_code`
6. `$geoip_postal_code`
7. `$geoip_time_zone`
8. `$geoip_subdivision_1_code`
9. `$geoip_subdivision_1_name`
10. `$geoip_subdivision_2_code`
11. `$geoip_subdivision_2_name`

You can also enable or disable GeoIP for a single capture request:

```python
posthog.capture('test_event', disable_geoip=True|False)
```

## Turn on debug mode

If you don't see the events or the feature flag evaluations you expect, turn on debug mode to see what's happening. Set the `debug` option to `True`. This turns on verbose logs about the inner workings of the SDK.

```python
posthog.debug = True
```

## Disable requests in tests

To stop requests during tests, set the `disabled` option to `True`. The SDK then captures no events and sends no requests to PostHog.

```python
if settings.TEST:
    posthog.disabled = True
```

## Configure connections

The SDK uses HTTP connection pooling for better performance. You typically don't need to change these settings. In some environments, like behind NAT gateways, pooled connections can be terminated non-gracefully, which causes request failures.

Call the following settings during initialization, before the SDK makes any API requests.

TCP keepalive probes help keep network infrastructure from dropping idle connections. This is the recommended fix for most cases where idle connections are terminated.

```python
import posthog

posthog.enable_keep_alive()
```

This turns on TCP keepalive with a 60 second idle time, a 60 second probe interval, and 3 probes before timeout.

If you need each request to use a fresh connection, turn off connection reuse entirely. This adds overhead per request.

```python
import posthog

posthog.disable_connection_reuse()
```

For advanced cases, set arbitrary socket options on the underlying HTTP connection:

```python
import socket
import posthog

posthog.set_socket_options([
    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    # Add additional socket options as needed
])
```

Pass `None` to `set_socket_options()` to reset to the default behavior.

## Filter events before sending

Use `before_send` to modify or drop events before they're queued for delivery. Return the modified event dictionary to send it, or `None` to drop it.

```python
from typing import Any

import posthog


def scrub_pii(event: dict[str, Any]) -> dict[str, Any] | None:
    properties = event.get("properties", {})

    if "email" in properties:
        email = properties["email"]
        properties["email"] = f"***@{email.split('@', 1)[1]}" if "@" in email else "***"

    if event.get("event") == "test_event":
        return None

    return event


client = posthog.Client(
    "<ph_project_api_key>",
    before_send=scrub_pii,
)
```

If your callback raises an exception, the SDK logs the error and drops the event.

## Run historical migrations

You can use the SDK to run [historical migrations](https://posthog.com/docs/migrate) of data into PostHog. Set the `historical_migration` option to `True` when you initialize the client:

```python
from posthog import Posthog
from datetime import datetime

posthog = Posthog(
    '<ph_project_token>', 
    host='<ph_client_api_host>',
    debug=True,
    historical_migration=True
)

events = [
  {
    "event": "batched_event_name",
    "distinct_id": "user_id",
    "timestamp": datetime.fromisoformat("2024-04-02T12:00:00+00:00"),
    "properties": {"account_type": "pro"}
  },
  {
    "event": "batched_event_name",
    "distinct_id": "user_id",
    "timestamp": datetime.fromisoformat("2024-04-03T09:30:00+00:00"),
    "properties": {"account_type": "pro"}
  }
]

for event in events:
  posthog.capture(
    event["event"],
    distinct_id=event["distinct_id"],
    properties=event["properties"],
    timestamp=event["timestamp"],
  )

posthog.shutdown()
```

## Run in serverless

By default, the synchronous `Posthog` client buffers events before it sends them to the capture endpoint. A serverless platform, like Render or AWS Lambda, can freeze or terminate the runtime before background delivery finishes. You have three options:

- **A client shared across invocations.** Call `posthog.flush()` before the handler returns, including when your code throws. This blocking call attempts to deliver queued events without stopping the client. Its `timeout_seconds` argument defaults to `10` seconds.
- **A disposable client or process exit.** Call `posthog.shutdown()` after its final work. It attempts to deliver queued events and cleans up the client. Don't reuse the client afterward. `shutdown()` takes no timeout argument.
- **Sync mode.** Set `sync_mode` when you initialize the client, so each `posthog.capture()` call attempts delivery before it returns.

These calls don't guarantee that PostHog received every event.

If you [trace your services](#trace-your-services), `sync_mode` doesn't apply to spans. For a shared client, call `posthog.flush()` before the handler returns. See [Flush spans on exit](#flush-spans-on-exit).

With `AsyncPosthog`, keep one client for the lifetime of your app. Use buffered `capture()` by default, or `await capture_immediate()` when one invocation must wait for an event's delivery attempt. Call `await posthog.shutdown()` once during app cleanup. Don't shut down the client after each request.

## Use Django or Flask

See the [Django docs](https://posthog.com/docs/libraries/django) to set up PostHog in Django. The [Django contexts middleware](https://posthog.com/docs/libraries/django#django-contexts-middleware) adds request properties and the authenticated user's identity to events captured during a request.

Since 7.71.0, the middleware also reads the browser session and distinct ID headers by default. Set `POSTHOG_MW_CAPTURE_TRACING_HEADERS = False` to turn off header extraction.

For Flask, see the [Flask docs](https://posthog.com/docs/libraries/flask).

## Add ASGI middleware

The SDK includes framework-independent ASGI middleware for FastAPI, Starlette, Litestar, and raw ASGI apps, available since 7.69.0. It creates a PostHog context for each HTTP or WebSocket request, adds request properties to captured events, and captures exceptions that escape the app.

For FastAPI or Starlette, add the middleware to your app:

```python
from fastapi import FastAPI
from posthog import Posthog
from posthog.integrations.asgi import PosthogASGIMiddleware

posthog = Posthog(
    "<ph_project_token>",
    host="<ph_client_api_host>",
    enable_exception_autocapture=True,
)

app = FastAPI()
app.add_middleware(PosthogASGIMiddleware, client=posthog)
```

The middleware's `capture_exceptions` option defaults to `None`, which inherits the client's `enable_exception_autocapture` setting. Set it to `True` or `False` to override the client.

Use `request_filter` to skip selected requests and `extra_properties` to add properties from the ASGI scope. Both callbacks can be synchronous or asynchronous.

The middleware reads `X-PostHog-Distinct-ID` and `X-PostHog-Session-ID` by default, since 7.71.0. Set `capture_tracing_headers=False` to turn off header extraction. Clients can control these headers. Use them only as analytics context, never for authentication or authorization.

To instrument another ASGI framework or a raw ASGI app, wrap the app directly:

```python
app = PosthogASGIMiddleware(app, client=posthog)
```

## The package names

The open source [PostHog](https://github.com/PostHog/posthog) project shares the `posthog` module name. To avoid a module collision, there's a `posthoganalytics` package, mostly for internal use. It's the exact same SDK.

This library is largely based on the `analytics-python` package.

## The supported versions

The Python SDK 8.x.x needs Python 3.10 or later. To pin to 8.x.x, run `pip install "posthog>=8,<9"`.

8.0.0 sends events with [capture v1](https://posthog.com/docs/api/capture-v1) and adds event options, like `options={"process_person_profile": False}`. Earlier versions don't take `options`. Read the [7.x.x to 8.0.0 migration guide](https://posthog.com/docs/libraries/python/migration) before you upgrade. If you stay on 7.x.x, see the [7.x.x reference](https://posthog.com/docs/libraries/python/7.x.x).

Python 3.9 isn't supported on 7.0.0 and later. Pin to the 6.x.x line with `pip install 'posthog<7'`, where 6.9.3 is the final release.

Event capture, the context API (`new_context`, `identify_context`, `set_context_session`), and `PosthogContextMiddleware` are identical on 6.9.3 and 7.0.0. 7.0.0 only dropped Python 3.9 and bumped the optional LLM provider SDKs. The middleware identifies the request context from the `X-POSTHOG-DISTINCT-ID` header and falls back to the authenticated user on both lines.

Later 7.x.x releases add what the 6.x.x line doesn't get, like the Celery integration, tracing header sanitization, and `set_context_device_id`.

They also changed the middleware's own captured properties. 7.x.x sends the request IP as `$ip`, where 6.9.3 sends it as `$ip_address`. 7.x.x also captures `$request_path`, `$raw_user_agent`, and the authenticated user's `email`.

6.0.0 introduced the contexts API and breaking changes. If you upgrade from 5.x.x to 6.x.x, read the [6.x.x migration guide](https://posthog.com/tutorials/python-v6-migration) first.
