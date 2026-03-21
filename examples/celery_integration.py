"""
Celery integration example for the PostHog Python SDK.

Demonstrates how to use ``PosthogCeleryIntegration`` with:
- producer-side and worker-side instrumentation (publishing events and context propagation)
- context propagation (distinct ID, session ID, tags) from producer to worker
- task lifecycle events (published, started, success, failure, retry)
- exception capture from failed tasks
- ``task_filter`` customization hook

Setup:
    1. Set ``POSTHOG_PROJECT_API_KEY`` and ``POSTHOG_HOST`` in your environment
    2. Install dependencies: pip install posthog celery redis
    3. Start Redis: redis-server
    4. Start the worker: celery -A examples.celery_integration worker --loglevel=info
    5. Run the producer: python -m examples.celery_integration
"""

import os
import time
from typing import Any, Optional

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown

import posthog
from posthog.integrations.celery import PosthogCeleryIntegration


# --- Configuration ---

POSTHOG_PROJECT_API_KEY = os.getenv("POSTHOG_PROJECT_API_KEY", "phc_...")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "http://localhost:8000")

app = Celery(
    "examples.celery_integration",
    broker="redis://localhost:6379/0",
)


# --- Integration wiring ---

def configure_posthog() -> None:
    posthog.api_key = POSTHOG_PROJECT_API_KEY
    posthog.host = POSTHOG_HOST
    posthog.enable_local_evaluation = False     # to not require personal_api_key for this example
    posthog.setup()


def task_filter(task_name: Optional[str], task_properties: dict[str, Any]) -> bool:
    if task_name is not None and task_name.endswith(".health_check"):
        return False
    return True


def create_integration() -> PosthogCeleryIntegration:
    return PosthogCeleryIntegration(
        capture_exceptions=True,
        capture_task_lifecycle_events=True,
        propagate_context=True,
        task_filter=task_filter,
    )

configure_posthog()
integration = create_integration()
integration.instrument()


# --- Worker process setup ---
# Celery's default prefork pool runs tasks in child processes. This example
# runs on a single host, so the inherited PostHog client and Celery
# integration are fork-safe and do not need to be recreated in each child.
# If workers run across multiple hosts, configure PostHog and instrument a
# worker-local integration in worker_process_init.
@worker_process_init.connect
def on_worker_process_init(**kwargs) -> None:
    # global integration

    # configure_posthog()
    # integration = create_integration()
    # integration.instrument()
    return


# Use this signal to shutdown the integration and PostHog client
# Calling shutdown() is important to flush any pending events
@worker_process_shutdown.connect
def on_worker_process_shutdown(**kwargs) -> None:
    integration.shutdown()
    posthog.shutdown()


# --- Example tasks ---

@app.task
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.task(max_retries=3)
def process_order(order_id: str) -> dict:
    """A task that processes an order successfully."""

    # simulate work
    time.sleep(0.1)

    # Custom event inside the task - context tags propagated from the
    # producer (e.g. "source", "release") should appear on this event
    # and this should be attributed to the correct distinct ID and session.
    posthog.capture(
        "celery example order processed",
        properties={"order_id": order_id, "amount": 99.99},
    )

    return {"order_id": order_id, "status": "completed"}


@app.task(bind=True, max_retries=3)
def send_notification(self, user_id: str, message: str) -> None:
    """A task that may fail and retry."""
    if self.request.retries < 2:
        raise self.retry(
            exc=ConnectionError("notification service unavailable"),
            countdown=120,
        )
    return None


@app.task
def failing_task() -> None:
    """A task that always fails."""
    raise ValueError("something went wrong")


# --- Producer code ---

if __name__ == "__main__":
    print("PostHog Celery Integration Example")
    print("=" * 40)
    print()

    # Set up PostHog context before dispatching tasks.
    # The integration propagates this context to workers via task headers.
    with posthog.new_context(fresh=True):
        posthog.identify_context("user-123")
        posthog.set_context_session("session-user-123-abc")
        posthog.tag("source", "celery_integration_example_script")
        posthog.tag("release", "v1.2.3")

        print("Dispatching tasks...")

        # This task is intentionally filtered and should not emit task events.
        result = health_check.delay()
        print(f"  health_check dispatched (filtered): {result.id}")

        # This task will produce events:
        #   celery task published  (sender side)
        #   celery task started    (worker side)
        #   order processed        (custom event, should carry propagated context tags)
        #   celery task success    (worker side, includes duration)
        result = process_order.delay("order-456")
        print(f"  process_order dispatched: {result.id}")

        # This task will produce events:
        #   celery task published
        #   celery task started
        #   celery task retry      (with reason)
        #   celery task started    (retry attempt)
        #   celery task success
        result = send_notification.delay("user-123", "Hello!")
        print(f"  send_notification dispatched: {result.id}")

        # This task will produce events:
        #   celery task published
        #   celery task started
        #   celery task failure    (with error_type and error_message)
        result = failing_task.delay()
        print(f"  failing_task dispatched: {result.id}")

    print()
    print("Tasks dispatched. Check your Celery worker logs and PostHog for events.")
    print()

    integration.shutdown()
    posthog.shutdown()
