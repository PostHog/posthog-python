"""PostHog capture from preloaded gunicorn gevent workers.

Setup:
    1. Set ``POSTHOG_PROJECT_API_KEY`` and optionally ``POSTHOG_HOST``.
    2. Start gunicorn from the repository root::

        uv run --with gunicorn --with 'gevent>=25.4.1' \
          gunicorn --workers 2 --worker-class gevent --preload \
          examples.gevent_gunicorn:app

    3. Capture an event with ``curl http://localhost:8000``.

The SDK reinitializes its consumer after gunicorn forks, so no gunicorn
``post_fork`` hook or additional PostHog setup is required.
"""

import os

from posthog import Posthog


posthog = Posthog(
    os.environ["POSTHOG_PROJECT_API_KEY"],
    host=os.getenv("POSTHOG_HOST", "https://us.i.posthog.com"),
    flush_at=1,
)


def app(environ, start_response):
    """Capture one event and return a minimal WSGI response."""
    posthog.capture(
        "gevent gunicorn example request",
        distinct_id=environ.get("REMOTE_ADDR", "unknown"),
    )
    start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
    return [b"Event queued\n"]
