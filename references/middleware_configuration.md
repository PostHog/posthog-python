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
| `trust_tracing_headers` | `POSTHOG_MW_TRUST_TRACING_HEADERS` | Accept `X-PostHog-Distinct-ID` and `X-PostHog-Session-ID` only when `True`; defaults to `False`. |

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

Tracing headers now require opt-in in Django and Flask, matching ASGI's secure
default. Applications intentionally relying on browser-to-backend analytics
attribution must enable the trust option. Sanitization limits malformed input;
it does not establish authenticity. Never use these headers for authentication or
authorization. Django can still use its authenticated request user as identity
when tracing headers are ignored.

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
