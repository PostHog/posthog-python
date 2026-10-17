# Request integration configuration

ASGI, Flask, and Django enrich events captured during an active request with request
properties and available identity/session context. Enrichment does not create a
request event or redirect manually captured events to a different client.

## Shared options

| ASGI / Flask option | Django setting | Meaning |
| --- | --- | --- |
| `client` | `POSTHOG_MW_CLIENT` | Client for automatic exceptions; defaults to the global client. |
| `capture_exceptions` | `POSTHOG_MW_CAPTURE_EXCEPTIONS` | `True`: capture automatically; `False`: context only; `None`: inherit the effective client's `enable_exception_autocapture`. |
| `request_filter` | `POSTHOG_MW_REQUEST_FILTER` | Callback returning `False` to skip both enrichment and automatic capture for a request. |
| `extra_properties` | `POSTHOG_MW_EXTRA_PROPERTIES` | Callback returning additional event properties. Explicit properties passed to event capture take precedence. |
| `capture_tracing_headers` | `POSTHOG_MW_CAPTURE_TRACING_HEADERS` | Read `X-PostHog-Distinct-ID` and `X-PostHog-Session-ID` into request-scoped analytics context; defaults to `True` once the integration is installed. |

ASGI callbacks receive the ASGI scope and can be synchronous or asynchronous.
Flask callbacks receive the Flask request. Django callbacks receive the Django
`HttpRequest`.

ASGI and Flask default `capture_exceptions` to `None`, so installing an integration
alone does not enable error tracking on a client that has it disabled. Client
inheritance is resolved when an exception occurs, so client configuration performed
after middleware registration is respected. The flag
controls integration-level automatic capture, not manual `capture_exception()`
calls or other independently installed error-capture hooks.

## Django compatibility

Django retains its historical automatic exception capture when
`POSTHOG_MW_CAPTURE_EXCEPTIONS` is omitted. Set it to `None` to inherit the client's
configuration, or `False` for context-only behavior.

`POSTHOG_MW_EXTRA_TAGS` remains an alias for `POSTHOG_MW_EXTRA_PROPERTIES`, and
`POSTHOG_MW_TAG_MAP` remains an alias for `POSTHOG_MW_PROPERTIES_MAP`. The latter is
a Django-specific callback that transforms the extracted properties. When both
old and new settings are present, the new setting takes precedence (including
when set to `None` to disable the callback). Existing tag-named middleware methods
and attributes remain compatibility aliases.

## Tracing headers

All three integrations capture tracing headers by default once installed. Disable
extraction with `capture_tracing_headers=False`, or
`POSTHOG_MW_CAPTURE_TRACING_HEADERS = False` in Django. This does not disable request
metadata or automatic exception capture.

`trust_tracing_headers` and `POSTHOG_MW_TRUST_TRACING_HEADERS` remain deprecated
aliases and emit `DeprecationWarning` when configured. Explicit `False` on either
alias continues to disable extraction. The capture-named option takes precedence
when both names are supplied; `None` means the option was not supplied.

Header names are case-insensitive. Values must be strings; control characters are
removed, surrounding whitespace is trimmed, and values are capped at 1000
characters. Empty or invalid values are ignored. Repeated ASGI tracing headers use
the first usable value rather than combining identities. A usable header distinct ID
sets the request context identity, replacing inherited context identity; Django
falls back to its authenticated request user ID when no usable header ID is
available or extraction is disabled. A usable session header sets the context
session and is attached to captured events as `$session_id`.

Explicit per-call distinct IDs and event properties, including `$session_id`,
always take precedence over header-derived context. Application code may also
replace request context identity with `identify_context`. Context changes remain
request-local and are restored when request handling ends.

These headers are client-controlled analytics context, never authentication or
authorization evidence. Sanitization limits malformed input; it does not establish
authenticity.

## Django REST Framework

DRF handles many exceptions before Django middleware can see them, so its
PostHog exception handler is configured separately. It does not establish request
context: use the Django middleware for enrichment, request properties, and header
policy.

An explicit `create_exception_handler(capture_exceptions=True/False)` overrides the
Django setting. Otherwise the handler follows `POSTHOG_MW_CAPTURE_EXCEPTIONS`,
including `None` for client inheritance and the omitted legacy `True` default.
Its client precedence is explicit handler client, `POSTHOG_DRF_CLIENT` (legacy
handler-specific setting), `POSTHOG_MW_CLIENT`, then the global client.
The middleware request filter also applies to handled DRF errors.

## Lifetime and isolation

Context is read when an event is captured, before it is queued for transport.
It does not depend on a later flush occurring inside the request. Outside the
request scope it is restored. Background threads do not automatically inherit
request context; async tasks follow Python's context-variable propagation rules.
Flask isolates identity and request properties while retaining effective enclosing
exception code-variable privacy controls.

## OpenTelemetry spans

Native PostHog spans read the active request IDs when `start_span` runs.
For application-owned OpenTelemetry spans, install `opentelemetry-sdk` and add
`PostHogContextSpanProcessor` to the application's existing `TracerProvider`:

```python
from posthog.integrations.otel import PostHogContextSpanProcessor

provider.add_span_processor(PostHogContextSpanProcessor())
```

Register the processor before requests start. Keep the existing provider,
exporters, and sampler. The processor adds `sessionId` and `posthogDistinctId`
from the active PostHog context when each recording span starts. These values
stay on the span after the request ends. Explicit span attributes take precedence.
The bridge skips enrichment when there is not enough attribute capacity or the
SDK does not expose its limit. OpenTelemetry SDK 1.20.0 or later is supported.
An existing application identity, such as `user.id`, prevents fallback identity
from being added. Absent IDs are omitted. OpenTelemetry remains optional for
applications that do not use this integration.

Django's `PosthogContextMiddleware` also adds IDs to the active OpenTelemetry SDK
request span after it reads the request context. This covers a request span that
starts before the PostHog middleware. Put the OpenTelemetry request middleware
outside the PostHog middleware. Put Django's authentication middleware before
PostHog if you want authenticated user identity. Both sync and async Django
request handlers use the same bridge. Tracing headers are captured by default.
Set `POSTHOG_MW_CAPTURE_TRACING_HEADERS = False` to disable extraction. These
headers remain untrusted analytics context.

Flask's `PosthogFlaskIntegration` adds IDs to the active OpenTelemetry SDK
request span after its request hook sets the context. Keep OpenTelemetry's WSGI
middleware outside the Flask app so that its request span is active before
Flask runs `before_request` hooks. Browser session and person headers are captured
by default. Set `capture_tracing_headers=False` to disable extraction. Register the shared processor above for
child spans; the request hook alone does not enrich later OpenTelemetry spans.
The application must set authenticated identity on the request span before the
PostHog hook if that identity must take precedence over a browser header.
Requests excluded by `request_filter` receive no enrichment. Flask request
context closes in `teardown_request`, including when the view raises an error.

ASGI's `PosthogASGIMiddleware` also adds IDs after it reads the headers. This
covers an active recording request span started by outer tracing middleware.
FastAPI applications use the same ASGI middleware:

```python
from posthog.integrations.asgi import PosthogASGIMiddleware

app.add_middleware(PosthogASGIMiddleware)
```

Keep OpenTelemetry request tracing outside this middleware and register
`PostHogContextSpanProcessor` for child spans. Both async handlers and FastAPI's
sync handlers run within the request context. HTTP and WebSocket scopes are
supported; WebSocket tracing requires the application to supply a recording span.
Lifespan scopes and requests rejected by `request_filter` pass through without
span enrichment. The middleware does not create request spans. Spans started by
outer error-response handlers after the PostHog scope exits omit request IDs.

For a custom request integration, call `enrich_current_span()` from
`posthog.integrations.otel` once after setting the request IDs. This call handles
an already active recording SDK span. The processor handles spans started later
in the request. A context lookup failure does not interrupt the request.
No provider, exporter, trace, session ID, or recording is created by this bridge.

The bridge does not propagate context into a separate process or a new thread.
Only spans started during the supported request lifetime inherit request IDs.
Spans started by an inherited async task after the request ends omit request IDs.
A background task can set its own IDs in a new `new_context()` scope.
Only IDs from active scopes are used. Set `fresh=True` to prevent inheritance
from any parent scope.
The AI OpenTelemetry processor is a separate integration with different attributes.

## Celery task spans

`PosthogCeleryIntegration` carries context through its existing task message
headers when `propagate_context=True` (the default). Instrument the producer and
each worker process. A worker starts a fresh PostHog context before each task and
restores the prior context after success, failure, or retry. A task without
PostHog headers does not inherit identity or session IDs from an enclosing scope.

Add `PostHogContextSpanProcessor` to the application's existing provider for
child spans. The Celery integration also enriches an active task span after it
reads the message headers. It does not create task spans. To create task spans,
install and configure `opentelemetry-instrumentation-celery` separately.

Call `PosthogCeleryIntegration.instrument()` before
`CeleryInstrumentor().instrument(tracer_provider=provider)`. This order ensures
that PostHog restores the job context before OpenTelemetry starts the task span.
This also matters for eager and nested tasks: with the reverse order, the span
processor can read the enclosing context before the task context is available.
Existing span attributes are preserved, so the later task hook cannot replace
those values.

Celery's eager execution does not emit task publish signals. To carry context
into an eager task, pass the task's PostHog headers explicitly with
`task.apply(headers=...)` or `task.apply_async(headers=...)`. Do not use these
analytics headers for authentication or authorization. Task context propagation
does not grant a background task access to an ended request scope.
