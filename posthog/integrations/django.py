from typing import TYPE_CHECKING, cast
from posthog import contexts, capture_exception
from posthog.client import Client

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse  # noqa: F401
    from typing import Callable, Dict, Any, Optional  # noqa: F401


class PosthogContextMiddleware:
    """Middleware to automatically track Django requests.

    This middleware wraps all calls with a posthog context. It attempts to extract the following from the request headers:
    - Session ID, (extracted from `X-POSTHOG-SESSION-ID`)
    - Distinct ID, (extracted from `X-POSTHOG-DISTINCT-ID`)
    - Request URL as $current_url
    - Request Method as $request_method

    The context will also auto-capture exceptions and send them to PostHog, unless you disable it by setting
    `POSTHOG_MW_CAPTURE_EXCEPTIONS` to `False` in your Django settings. The exceptions are captured using the
    global client, unless the setting `POSTHOG_MW_CLIENT` is set to a custom client instance

    The middleware behaviour is customisable through 3 additional functions:
    - `POSTHOG_MW_EXTRA_TAGS`, which is a Callable[[HttpRequest], Dict[str, Any]] expected to return a dictionary of additional tags to be added to the context.
    - `POSTHOG_MW_REQUEST_FILTER`, which is a Callable[[HttpRequest], bool] expected to return `False` if the request should not be tracked.
    - `POSTHOG_MW_TAG_MAP`, which is a Callable[[Dict[str, Any]], Dict[str, Any]], which you can use to modify the tags before they're added to the context.

    You can use the `POSTHOG_MW_TAG_MAP` function to remove any default tags you don't want to capture, or override them with your own values.

    Context tags are automatically included as properties on all events captured within a context, including exceptions.
    See the context documentation for more information. The extracted distinct ID and session ID, if found, are used to
    associate all events captured in the middleware context with the same distinct ID and session as currently active on the
    frontend. See the documentation for `set_context_session` and `identify_context` for more details.
    """

    def __init__(self, get_response):
        # type: (Callable[[HttpRequest], HttpResponse]) -> None
        self.get_response = get_response

        from django.conf import settings

        if hasattr(settings, "POSTHOG_MW_EXTRA_TAGS") and callable(
            settings.POSTHOG_MW_EXTRA_TAGS
        ):
            self.extra_tags = cast(
                "Optional[Callable[[HttpRequest], Dict[str, Any]]]",
                settings.POSTHOG_MW_EXTRA_TAGS,
            )
        else:
            self.extra_tags = None

        if hasattr(settings, "POSTHOG_MW_REQUEST_FILTER") and callable(
            settings.POSTHOG_MW_REQUEST_FILTER
        ):
            self.request_filter = cast(
                "Optional[Callable[[HttpRequest], bool]]",
                settings.POSTHOG_MW_REQUEST_FILTER,
            )
        else:
            self.request_filter = None

        if hasattr(settings, "POSTHOG_MW_TAG_MAP") and callable(
            settings.POSTHOG_MW_TAG_MAP
        ):
            self.tag_map = cast(
                "Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]",
                settings.POSTHOG_MW_TAG_MAP,
            )
        else:
            self.tag_map = None

        if hasattr(settings, "POSTHOG_MW_CAPTURE_EXCEPTIONS") and isinstance(
            settings.POSTHOG_MW_CAPTURE_EXCEPTIONS, bool
        ):
            self.capture_exceptions = settings.POSTHOG_MW_CAPTURE_EXCEPTIONS
        else:
            self.capture_exceptions = True

        if hasattr(settings, "POSTHOG_MW_CLIENT") and isinstance(
            settings.POSTHOG_MW_CLIENT, Client
        ):
            self.client = cast("Optional[Client]", settings.POSTHOG_MW_CLIENT)
        else:
            self.client = None

    def extract_tags(self, request):
        # type: (HttpRequest) -> Dict[str, Any]
        tags = {}

        (user_id, user_email) = self.extract_request_user(request)

        # Extract session ID from X-POSTHOG-SESSION-ID header
        session_id = request.headers.get("X-POSTHOG-SESSION-ID")
        if session_id:
            contexts.set_context_session(session_id)

        # Extract distinct ID from X-POSTHOG-DISTINCT-ID header or request user id
        distinct_id = request.headers.get("X-POSTHOG-DISTINCT-ID") or user_id
        if distinct_id:
            contexts.identify_context(distinct_id)

        # Extract user email
        if user_email:
            tags["email"] = user_email

        # Extract current URL
        absolute_url = request.build_absolute_uri()
        if absolute_url:
            tags["$current_url"] = absolute_url

        # Extract request method
        if request.method:
            tags["$request_method"] = request.method

        # Extract request path
        if request.path:
            tags["$request_path"] = request.path

        # Extract IP address
        ip_address = request.headers.get("X-Forwarded-For")
        if ip_address:
            tags["$ip_address"] = ip_address

        # Extract user agent
        user_agent = request.headers.get("User-Agent")
        if user_agent:
            tags["$user_agent"] = user_agent

        # Apply extra tags if configured
        if self.extra_tags:
            extra = self.extra_tags(request)
            if extra:
                tags.update(extra)

        # Apply tag mapping if configured
        if self.tag_map:
            tags = self.tag_map(tags)

        return tags

    def extract_request_user(self, request):
        user_id = None
        email = None

        user = getattr(request, "user", None)

        if user and getattr(user, "is_authenticated", False):
            try:
                user_id = str(user.pk)
            except Exception:
                pass

            try:
                email = str(user.email)
            except Exception:
                pass

        return user_id, email

    def __call__(self, request):
        # type: (HttpRequest) -> HttpResponse
        if self.request_filter and not self.request_filter(request):
            return self.get_response(request)

        with contexts.new_context(self.capture_exceptions, client=self.client):
            for k, v in self.extract_tags(request).items():
                contexts.tag(k, v)

            return self.get_response(request)

    def process_exception(self, request, exception):
        if self.request_filter and not self.request_filter(request):
            return

        if not self.capture_exceptions:
            return

        if self.client:
            self.client.capture_exception(exception)
        else:
            capture_exception(exception)
