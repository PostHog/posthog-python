"""
Integration for `celery`_ to capture task lifecycle events and exceptions with PostHog.

.. _celery: https://pypi.org/project/celery/

Features:
- Hooks into Celery signals to automatically capture task lifecycle events
  (started, success, failure, retry, published) and exceptions.
- Lifecycle events include Celery-specific properties such as task ID, task name,
  queue, retry count, duration, Celery version etc.
- Any custom events captured inside a task (via ``client.capture``) are automatically
  enriched with the same Celery-specific properties via context tags.
- Propagates PostHog context (distinct ID, session ID, tags) from the producer
  process to the worker process.

Supports Celery 4.0+ (Message Protocol Version 2).

Usage
-----

.. code-block:: python

    from posthog import Posthog
    from posthog.integrations.celery import PosthogCeleryIntegration

    posthog = Posthog("<ph_project_api_key>", host="<ph_client_api_host>")

    integration = PosthogCeleryIntegration(client=posthog)
    integration.instrument()

Both the producer process and each worker process must initialize the
PostHog client and instrument the integration because the worker needs
to bind to Celery signals, and the PostHog client may use background threads
to send captured events (depending on ``sync_mode``). Celery provides a signal
called ``worker_process_init`` that can be used to accomplish this.

See ``examples/celery_integration.py`` for a complete working example.

Supported task states for event emission:
    - ``started``
    - ``success``
    - ``failure``
    - ``retry``
    - ``published``

Event properties:
    All lifecycle and exception events include the following properties:

    - ``celery_task_id`` -- unique task ID
    - ``celery_task_name`` -- registered task name
    - ``celery_state`` -- lifecycle state (started, success, failure, etc.)
    - ``celery_hostname`` -- worker hostname
    - ``celery_exchange`` -- broker exchange
    - ``celery_routing_key`` -- broker routing key
    - ``celery_queue`` -- broker queue name
    - ``celery_retry_count`` -- number of retries so far
    - ``celery_version`` -- installed Celery library version
    - ``celery_task_duration_ms`` -- task wall-clock duration in milliseconds
      (present on terminal states: success, failure, retry)

    Additional properties on specific states:

    - **failure**: ``error_type``, ``error_message``
    - **retry**: ``celery_reason``
"""

import json
import logging
import time
from typing import Any, Callable, Optional

from posthog import contexts
from posthog.client import Client


CONTEXT_DISTINCT_ID_HEADER = "X-POSTHOG-DISTINCT-ID"
CONTEXT_SESSION_ID_HEADER = "X-POSTHOG-SESSION-ID"
CONTEXT_TAGS_HEADER = "X-POSTHOG-CONTEXT-TAGS"

logger = logging.getLogger("posthog")


class PosthogCeleryIntegration:
    """Celery integration that captures task lifecycle events and exceptions.

    Args:
        client: Optional ``Client`` instance. When provided, all events and
            exceptions are captured through this client rather than the
            global ``posthog`` module.
        capture_exceptions: Whether to capture task exceptions via
            ``capture_exception`` (default ``True``).
        capture_task_lifecycle_events: Whether to emit lifecycle events of the task
            such as "started", "success", "failure" etc. (default ``True``).
        propagate_context: Whether to propagate PostHog context (distinct
            ID, session ID, tags) from the producer to the worker via task
            headers (default ``True``).
        task_filter: Optional callback ``(task_name, task_properties) -> bool`` expected to
            return ``False`` if a given task should not be tracked.
    """

    def __init__(
        self,
        client: Optional[Client] = None,
        capture_exceptions: bool = True,
        capture_task_lifecycle_events: bool = True,
        propagate_context: bool = True,
        task_filter: Optional[Callable[[Optional[str], dict[str, Any]], bool]] = None,
    ):
        self.client = client
        self.capture_exceptions = capture_exceptions
        self.capture_task_lifecycle_events = capture_task_lifecycle_events
        self.propagate_context = propagate_context
        self.task_filter = task_filter

        self._instrumented = False
        self._signals: Optional[Any] = None
        self._celery_version: Optional[str] = None

    def instrument(self) -> None:
        if self._instrumented:
            return

        from celery import signals
        from celery import __version__ as celery_version

        self._signals = signals
        self._celery_version = celery_version

        signals.task_prerun.connect(self._on_task_prerun, weak=False)
        signals.task_success.connect(self._on_task_success, weak=False)
        signals.task_failure.connect(self._on_task_failure, weak=False)
        signals.task_retry.connect(self._on_task_retry, weak=False)
        signals.before_task_publish.connect(self._on_before_task_publish, weak=False)
        signals.after_task_publish.connect(self._on_after_task_publish, weak=False)

        self._instrumented = True

    def uninstrument(self) -> None:
        if not self._instrumented or not self._signals:
            return

        self._signals.task_prerun.disconnect(self._on_task_prerun)
        self._signals.task_success.disconnect(self._on_task_success)
        self._signals.task_failure.disconnect(self._on_task_failure)
        self._signals.task_retry.disconnect(self._on_task_retry)
        self._signals.before_task_publish.disconnect(self._on_before_task_publish)
        self._signals.after_task_publish.disconnect(self._on_after_task_publish)

        self._signals = None
        self._instrumented = False

    def _on_before_task_publish(self, *args, **kwargs):
        try:
            if not self.propagate_context:
                return

            headers = kwargs.get("headers")
            if not isinstance(headers, dict):
                return

            distinct_id = contexts.get_context_distinct_id()
            session_id = contexts.get_context_session_id()
            tags = contexts.get_tags()

            posthog_headers: dict[str, str] = {}
            if distinct_id:
                posthog_headers[CONTEXT_DISTINCT_ID_HEADER] = distinct_id
            if session_id:
                posthog_headers[CONTEXT_SESSION_ID_HEADER] = session_id
            if tags:
                posthog_headers[CONTEXT_TAGS_HEADER] = json.dumps(tags, default=str)

            if posthog_headers:
                headers.update(posthog_headers)
                # https://github.com/celery/celery/issues/4875
                # In Celery protocol v2, top-level custom headers do not
                # reliably appear in task.request.headers on the worker.
                # Only headers nested inside headers["headers"] survive.
                # Both sentry-sdk and dd-trace-py use this same workaround.
                headers.setdefault("headers", {}).update(posthog_headers)
        except Exception:
            logger.exception("Failed to propagate PostHog context in before_task_publish")

    def _on_after_task_publish(self, *args, **kwargs):
        try:
            if not self.capture_task_lifecycle_events:
                return

            sender = kwargs.get("sender")   # contains task name for publish events, NOT task object
            headers = kwargs.get("headers")
            task_id = headers.get("id") if isinstance(headers, dict) else None

            sender_properties = {
                "celery_task_id": task_id,
                "celery_task_name": sender,
                "celery_state": "published",
                "celery_exchange": kwargs.get("exchange"),
                "celery_routing_key": kwargs.get("routing_key"),
                "celery_hostname": None,    # Not available at publish time (no worker assigned yet)
                "celery_retry_count": headers.get("retries") if isinstance(headers, dict) else None,
                "celery_version": self._celery_version,
            }

            if self._should_track(sender, sender_properties):
                self._capture_event("celery task published", properties=sender_properties)
        except Exception:
            logger.exception("Failed to capture Celery after_task_publish lifecycle event")

    def _on_task_prerun(self, *args, **kwargs):
        try:
            task_id = kwargs.get("task_id")
            if not task_id:
                return

            sender = kwargs.get("sender")
            request = getattr(sender, "request", None)
            context_tags = self._extract_propagated_tags(request)
            task_properties = self._build_task_properties(
                sender=sender,
                task_id=task_id,
                state="started",
            )
            task_name = task_properties.get("celery_task_name")

            context_manager = contexts.new_context(
                fresh=True,  # to prevent context bleed across tasks
                capture_exceptions=False,  # Celery catches task exceptions internally and
                                           # delivers them via task_failure signal, so they
                                           # never propagate through the context manager.
                                           # We capture them in _on_task_failure.
                client=self.client,
            )
            context_manager.__enter__()

            if request is not None:
                request._posthog_ctx = context_manager
                request._posthog_start = time.monotonic()

            self._apply_propagated_identity(request)

            merged_tags = {**task_properties, **context_tags}
            for key, value in merged_tags.items():
                contexts.tag(key, value)

            if self.capture_task_lifecycle_events and self._should_track(task_name, task_properties):
                self._capture_event("celery task started", properties=task_properties)
        except Exception:
            logger.exception("Failed to process Celery task_prerun")

    def _on_task_success(self, *args, **kwargs):
        self._handle_task_end("success", **kwargs)

    def _on_task_failure(self, *args, **kwargs):
        self._handle_task_end("failure", **kwargs)

    def _on_task_retry(self, *args, **kwargs):
        self._handle_task_end("retry", extra_properties={
            "celery_reason": str(kwargs.get("reason")),
        }, **kwargs)

    def _handle_task_end(
        self,
        state: str,
        extra_properties: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        sender = kwargs.get("sender")
        request = getattr(sender, "request", None)

        try:
            task_id = kwargs.get("task_id")
            if task_id is None:
                task_id = getattr(request, "id", None)

            task_properties = self._build_task_properties(
                sender=sender,
                task_id=task_id,
                state=state,
            )
            if extra_properties:
                task_properties.update(extra_properties)

            self._add_duration(request, task_properties)

            exception = kwargs.get("exception")
            if exception:
                task_properties["error_type"] = type(exception).__name__
                task_properties["error_message"] = str(exception)
                if self.capture_exceptions:
                    self._capture_exception(exception)

            task_name = task_properties.get("celery_task_name")
            if self.capture_task_lifecycle_events and self._should_track(task_name, task_properties):
                self._capture_event(f"celery task {state}", properties=task_properties)
        except Exception:
            logger.exception("Failed to process Celery %s", state)
        finally:
            ctx = getattr(request, "_posthog_ctx", None)
            if ctx is not None:
                ctx.__exit__(None, None, None)

    def _apply_propagated_identity(self, request: Any) -> None:
        headers = self._extract_headers(request)
        distinct_id = headers.get(CONTEXT_DISTINCT_ID_HEADER)
        if distinct_id:
            contexts.identify_context(str(distinct_id))
        
        session_id = headers.get(CONTEXT_SESSION_ID_HEADER)
        if session_id:
            contexts.set_context_session(str(session_id))

    def _extract_propagated_tags(self, request: Any) -> dict[str, Any]:
        headers = self._extract_headers(request)

        try:
            parsed = json.loads(headers.get(CONTEXT_TAGS_HEADER))
        except Exception:
            return {}

        if isinstance(parsed, dict):
            return parsed
        return {}

    def _extract_headers(self, request: Any) -> dict[str, Any]:
        if request is None:
            return {}

        # On the Celery worker, request.headers maps to the nested
        # message["headers"]["headers"] dict (see celery#4875), which is
        # where _on_before_task_publish places PostHog context headers.
        headers = getattr(request, "headers", None)
        if isinstance(headers, dict):
            return headers

        if isinstance(request, dict):
            dict_headers = request.get("headers")
            if isinstance(dict_headers, dict):
                return dict_headers

        return {}

    def _build_task_properties(
        self,
        sender=None,
        task_id=None,
        state=None,
    ) -> dict[str, Any]:
        request = getattr(sender, "request", None)
        delivery_info = getattr(request, "delivery_info", None)
        delivery_info = delivery_info if isinstance(delivery_info, dict) else {}

        properties = {
            "celery_task_id": task_id,
            "celery_task_name": getattr(sender, "name", None),
            "celery_state": state,
            "celery_hostname": getattr(request, "hostname", None),
            "celery_exchange": delivery_info.get("exchange"),
            "celery_routing_key": delivery_info.get("routing_key"),
            "celery_queue": delivery_info.get("queue"),
            "celery_retry_count": getattr(request, "retries", None),
            "celery_version": self._celery_version,
        }
        return properties

    def _add_duration(self, request: Any, task_properties: dict[str, Any]) -> None:
        start_time = getattr(request, "_posthog_start", None)
        if start_time is not None:
            task_properties["celery_task_duration_ms"] = round(
                (time.monotonic() - start_time) * 1000.0, 3
            )

    def _should_track(self, task_name: Optional[str], task_properties: dict[str, Any]) -> bool:
        if self.task_filter:
            return bool(self.task_filter(task_name, task_properties))
        return True

    def _capture_event(self, event: str, properties: dict[str, Any]) -> None:
        if self.client:
            self.client.capture(event, properties=properties)
        else:
            from posthog import capture

            capture(event, properties=properties)

    def _capture_exception(self, exception: Exception) -> None:
        if self.client:
            self.client.capture_exception(exception)
        else:
            from posthog import capture_exception

            capture_exception(exception)


__all__ = [
    "PosthogCeleryIntegration",
]
