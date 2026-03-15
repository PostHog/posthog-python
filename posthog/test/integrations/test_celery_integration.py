import unittest
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import Mock, patch

from posthog import contexts
from posthog.integrations.celery import (
    CONTEXT_DISTINCT_ID_HEADER,
    CONTEXT_SESSION_ID_HEADER,
    CONTEXT_TAGS_HEADER,
    PosthogCeleryIntegration,
)


class FakeSignal:
    def __init__(self):
        self.connected = []
        self.disconnected = []

    def connect(self, handler, weak=False):
        self.connected.append((handler, weak))

    def disconnect(self, handler):
        self.disconnected.append(handler)


class TestPosthogCeleryIntegration(unittest.TestCase):
    def test_instrument_is_idempotent(self):
        fake_signals = SimpleNamespace(
            task_prerun=FakeSignal(),
            task_success=FakeSignal(),
            task_failure=FakeSignal(),
            task_retry=FakeSignal(),
            before_task_publish=FakeSignal(),
            after_task_publish=FakeSignal(),
        )

        integration = PosthogCeleryIntegration()
        fake_celery = ModuleType("celery")
        fake_celery.signals = fake_signals
        fake_celery.__version__ = "5.0.0"

        with patch.dict("sys.modules", {"celery": fake_celery}):
            integration.instrument()
            integration.instrument()

        for sig in [
            "task_prerun",
            "task_success",
            "task_failure",
            "task_retry",
            "before_task_publish",
            "after_task_publish",
        ]:
            self.assertEqual(len(getattr(fake_signals, sig).connected), 1, f"{sig} connected multiple times")

    def test_instrument_and_uninstrument_connect_signals(self):
        fake_signals = SimpleNamespace(
            task_prerun=FakeSignal(),
            task_success=FakeSignal(),
            task_failure=FakeSignal(),
            task_retry=FakeSignal(),
            before_task_publish=FakeSignal(),
            after_task_publish=FakeSignal(),
        )

        integration = PosthogCeleryIntegration()

        fake_celery = ModuleType("celery")
        fake_celery.signals = fake_signals
        fake_celery.__version__ = "5.0.0"

        with patch.dict("sys.modules", {"celery": fake_celery}):
            integration.instrument()
            integration.uninstrument()

        for sig in ["task_prerun", "task_success", "task_failure",
                    "task_retry", "before_task_publish",
                    "after_task_publish"]:
            self.assertEqual(len(getattr(fake_signals, sig).connected), 1, f"{sig} not connected")
            self.assertEqual(len(getattr(fake_signals, sig).disconnected), 1, f"{sig} not disconnected")

    def test_before_task_publish_propagates_context_headers(self):
        integration = PosthogCeleryIntegration()
        headers = {}

        with contexts.new_context(fresh=True):
            contexts.identify_context("distinct-123")
            contexts.set_context_session("session-456")
            contexts.tag("request_id", "abc")

            integration._on_before_task_publish(sender="test.task", headers=headers)

        self.assertEqual(headers[CONTEXT_DISTINCT_ID_HEADER], "distinct-123")
        self.assertEqual(headers[CONTEXT_SESSION_ID_HEADER], "session-456")
        self.assertIn(CONTEXT_TAGS_HEADER, headers)

        # celery#4875: headers must also be nested inside headers["headers"]
        # so they survive to task.request.headers on the worker
        inner = headers["headers"]
        self.assertEqual(inner[CONTEXT_DISTINCT_ID_HEADER], "distinct-123")
        self.assertEqual(inner[CONTEXT_SESSION_ID_HEADER], "session-456")
        self.assertIn(CONTEXT_TAGS_HEADER, inner)

    def test_before_task_publish_preserves_existing_nested_headers(self):
        integration = PosthogCeleryIntegration()
        headers = {"headers": {"sentry-trace": "abc-123"}}

        with contexts.new_context(fresh=True):
            contexts.identify_context("distinct-123")
            integration._on_before_task_publish(sender="test.task", headers=headers)

        inner = headers["headers"]
        self.assertEqual(inner["sentry-trace"], "abc-123")
        self.assertEqual(inner[CONTEXT_DISTINCT_ID_HEADER], "distinct-123")

    def test_before_task_publish_nested_headers_round_trips_to_worker(self):
        integration = PosthogCeleryIntegration(client=Mock())
        headers = {}

        with contexts.new_context(fresh=True):
            contexts.identify_context("user-rt")
            contexts.set_context_session("sess-rt")
            contexts.tag("env", "test")
            integration._on_before_task_publish(sender="test.task", headers=headers)

        # Simulate Celery worker: task.request.headers is the nested dict
        worker_request = SimpleNamespace(
            headers=headers["headers"],
            delivery_info={},
            hostname="worker-1",
            retries=0,
        )
        task = SimpleNamespace(name="test.task", request=worker_request)

        integration._on_task_prerun(sender=task, task_id="task-rt")

        self.assertEqual(contexts.get_context_distinct_id(), "user-rt")
        self.assertEqual(contexts.get_context_session_id(), "sess-rt")
        self.assertEqual(contexts.get_tags().get("env"), "test")

        integration._on_task_success(sender=task)

    def test_before_task_publish_respects_propagate_context_flag(self):
        integration = PosthogCeleryIntegration(propagate_context=False)
        headers = {}

        with contexts.new_context(fresh=True):
            contexts.identify_context("distinct-123")
            contexts.set_context_session("session-456")
            contexts.tag("request_id", "abc")

            integration._on_before_task_publish(sender="test.task", headers=headers)

        self.assertEqual(headers, {})

    def test_task_context_is_cleared_after_task_end(self):
        integration = PosthogCeleryIntegration(client=Mock())

        first_request = SimpleNamespace(
            headers={
                CONTEXT_DISTINCT_ID_HEADER: "user-1",
                CONTEXT_SESSION_ID_HEADER: "sess-1",
                CONTEXT_TAGS_HEADER: '{"source": "api"}',
            },
            delivery_info={},
            hostname="worker-1",
            retries=0,
        )
        first_task = SimpleNamespace(name="app.tasks.first", request=first_request)

        integration._on_task_prerun(sender=first_task, task_id="task-1")

        self.assertEqual(contexts.get_context_distinct_id(), "user-1")
        self.assertEqual(contexts.get_context_session_id(), "sess-1")
        self.assertEqual(contexts.get_tags().get("source"), "api")

        integration._on_task_success(sender=first_task, task_id="task-1")

        self.assertIsNone(contexts.get_context_distinct_id())
        self.assertIsNone(contexts.get_context_session_id())
        self.assertEqual(contexts.get_tags(), {})

        second_request = SimpleNamespace(
            headers={},
            delivery_info={},
            hostname="worker-1",
            retries=0,
        )
        second_task = SimpleNamespace(name="app.tasks.second", request=second_request)

        integration._on_task_prerun(sender=second_task, task_id="task-2")

        self.assertIsNone(contexts.get_context_distinct_id())
        self.assertIsNone(contexts.get_context_session_id())
        self.assertNotIn("source", contexts.get_tags())

        integration._on_task_success(sender=second_task, task_id="task-2")

    def test_task_prerun_hydrates_context_and_postrun_cleans_up(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        request = SimpleNamespace(
            headers={
                CONTEXT_DISTINCT_ID_HEADER: "user-1",
                CONTEXT_SESSION_ID_HEADER: "sess-1",
                CONTEXT_TAGS_HEADER: '{"source": "api"}',
            },
            delivery_info={"exchange": "celery", "routing_key": "default", "queue": "default"},
            hostname="worker-1",
            retries=0,
        )
        task = SimpleNamespace(name="app.tasks.example", request=request)

        integration._on_task_prerun(sender=task, task_id="task-1")

        self.assertEqual(contexts.get_context_distinct_id(), "user-1")
        self.assertEqual(contexts.get_context_session_id(), "sess-1")
        self.assertEqual(contexts.get_tags().get("source"), "api")
        self.assertTrue(hasattr(request, "_posthog_ctx"))
        self.assertTrue(hasattr(request, "_posthog_start"))

        integration._on_task_success(sender=task)

        event_names = [call.args[0] for call in mock_client.capture.call_args_list]
        self.assertIn("celery task started", event_names)
        self.assertIn("celery task success", event_names)

    def test_postrun_includes_duration(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        request = SimpleNamespace(
            headers={},
            delivery_info={},
            hostname="worker-1",
            retries=0,
        )
        task = SimpleNamespace(name="app.tasks.timed", request=request)

        integration._on_task_prerun(sender=task, task_id="task-t")
        integration._on_task_success(sender=task)

        completed_call = [
            c for c in mock_client.capture.call_args_list if c.args[0] == "celery task success"
        ]
        self.assertEqual(len(completed_call), 1)
        self.assertIn("celery_task_duration_ms", completed_call[0].kwargs["properties"])

    def test_failure_includes_duration(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        request = SimpleNamespace(
            headers={},
            delivery_info={},
            hostname="worker-1",
            retries=0,
        )
        task = SimpleNamespace(name="app.tasks.failing_timed", request=request)

        integration._on_task_prerun(sender=task, task_id="task-f")
        integration._on_task_failure(
            sender=task, task_id="task-f", exception=ValueError("boom")
        )

        failed_call = [
            c for c in mock_client.capture.call_args_list if c.args[0] == "celery task failure"
        ]
        self.assertEqual(len(failed_call), 1)
        self.assertIn("celery_task_duration_ms", failed_call[0].kwargs["properties"])

    def test_task_failure_captures_exception_and_failure_event(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        task = SimpleNamespace(name="app.tasks.failing", request=SimpleNamespace(delivery_info={}))
        exception = ValueError("task failed")

        integration._on_task_failure(
            sender=task,
            task_id="task-2",
            exception=exception,
        )

        mock_client.capture_exception.assert_called_once_with(exception)
        event_names = [call.args[0] for call in mock_client.capture.call_args_list]
        self.assertIn("celery task failure", event_names)

    def test_task_failure_event_includes_error_fields(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        task = SimpleNamespace(
            name="app.tasks.failing",
            request=SimpleNamespace(delivery_info={}),
        )
        exception = ValueError("task failed")

        integration._on_task_failure(
            sender=task,
            task_id="task-2",
            exception=exception,
        )

        failure_calls = [
            c for c in mock_client.capture.call_args_list if c.args[0] == "celery task failure"
        ]
        self.assertEqual(len(failure_calls), 1)
        props = failure_calls[0].kwargs["properties"]
        self.assertEqual(props["error_type"], "ValueError")
        self.assertEqual(props["error_message"], "task failed")

    def test_task_failure_skips_exception_capture_when_disabled(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client, capture_exceptions=False)

        task = SimpleNamespace(name="app.tasks.failing", request=SimpleNamespace(delivery_info={}))
        exception = ValueError("task failed")

        integration._on_task_failure(
            sender=task,
            task_id="task-2",
            exception=exception,
        )

        mock_client.capture_exception.assert_not_called()
        event_names = [call.args[0] for call in mock_client.capture.call_args_list]
        self.assertIn("celery task failure", event_names)

    def test_task_retry_captures_event_with_reason(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        task = SimpleNamespace(name="app.tasks.retrying", request=SimpleNamespace(delivery_info={}))

        integration._on_task_retry(
            sender=task,
            task_id="task-retry",
            reason=ConnectionError("broker down"),
        )

        event_names = [call.args[0] for call in mock_client.capture.call_args_list]
        self.assertIn("celery task retry", event_names)
        retry_call = [c for c in mock_client.capture.call_args_list if c.args[0] == "celery task retry"][0]
        props = retry_call.kwargs["properties"]
        self.assertEqual(props["celery_reason"], "broker down")

    def test_task_filter_applies_to_worker_lifecycle_events(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(
            client=mock_client,
            task_filter=lambda task_name, properties: False,
        )

        request = SimpleNamespace(
            headers={},
            delivery_info={},
            hostname="worker-1",
            retries=0,
        )
        task = SimpleNamespace(name="app.tasks.filtered", request=request)

        integration._on_task_prerun(sender=task, task_id="task-3")
        integration._on_task_success(sender=task, task_id="task-3")

        mock_client.capture.assert_not_called()

    def test_task_failure_captures_exception_when_lifecycle_events_disabled(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(
            client=mock_client,
            capture_task_lifecycle_events=False,
        )

        task = SimpleNamespace(
            name="app.tasks.failing",
            request=SimpleNamespace(delivery_info={}),
        )
        exception = ValueError("task failed")

        integration._on_task_failure(
            sender=task,
            task_id="task-4",
            exception=exception,
        )

        mock_client.capture.assert_not_called()
        mock_client.capture_exception.assert_called_once_with(exception)

    def test_after_task_publish_captures_published_event(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        integration._on_after_task_publish(
            sender="app.tasks.published",
            headers={"id": "task-3"},
            exchange="celery",
            routing_key="default",
        )

        mock_client.capture.assert_called_once()
        self.assertEqual(mock_client.capture.call_args.args[0], "celery task published")
        props = mock_client.capture.call_args.kwargs["properties"]
        self.assertIn("celery_version", props)

    def test_after_task_publish_respects_task_filter(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(
            client=mock_client, task_filter=lambda task_name, properties: False
        )

        integration._on_after_task_publish(
            sender="app.tasks.filtered",
            headers={"id": "task-3"},
            exchange="celery",
            routing_key="default",
        )

        mock_client.capture.assert_not_called()

    def test_after_task_publish_skips_when_lifecycle_events_disabled(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(
            client=mock_client,
            capture_task_lifecycle_events=False,
        )

        integration._on_after_task_publish(
            sender="app.tasks.published",
            headers={"id": "task-3"},
            exchange="celery",
            routing_key="default",
        )

        mock_client.capture.assert_not_called()

    def test_capture_event_falls_back_to_global_capture(self):
        integration = PosthogCeleryIntegration(client=None)

        with patch("posthog.capture") as mock_capture:
            integration._capture_event("celery task started", properties={"celery_task_id": "t1"})

        mock_capture.assert_called_once_with(
            "celery task started", properties={"celery_task_id": "t1"}
        )

    def test_capture_exception_falls_back_to_global_capture_exception(self):
        integration = PosthogCeleryIntegration(client=None)
        exception = ValueError("boom")

        with patch("posthog.capture_exception") as mock_capture_exception:
            integration._capture_exception(exception)

        mock_capture_exception.assert_called_once_with(exception)

    def test_extract_headers_supports_request_dict_shape(self):
        integration = PosthogCeleryIntegration()
        request = {"headers": {CONTEXT_DISTINCT_ID_HEADER: "user-1"}}

        headers = integration._extract_headers(request)

        self.assertEqual(headers, {CONTEXT_DISTINCT_ID_HEADER: "user-1"})

    def test_prerun_exits_context_on_failure_after_entry(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        request = SimpleNamespace(
            headers={},
            delivery_info={},
            hostname="worker-1",
            retries=0,
        )
        task = SimpleNamespace(name="app.tasks.boom", request=request)

        ctx_before = contexts._get_current_context()

        with patch.object(integration, "_apply_propagated_identity", side_effect=RuntimeError("boom")):
            integration._on_task_prerun(sender=task, task_id="task-leak")

        ctx_after = contexts._get_current_context()
        self.assertIs(ctx_after, ctx_before)

    def test_extract_propagated_tags_invalid_json_returns_empty_dict(self):
        integration = PosthogCeleryIntegration()
        request = SimpleNamespace(headers={CONTEXT_TAGS_HEADER: "{bad json"})

        tags = integration._extract_propagated_tags(request)

        self.assertEqual(tags, {})
