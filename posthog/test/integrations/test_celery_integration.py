import json
import unittest
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import Mock, patch

from parameterized import parameterized

from posthog import contexts
from posthog.capture_event import _to_v1_event
from posthog.client import Client
from posthog.integrations.celery import (
    CONTEXT_DISTINCT_ID_HEADER,
    CONTEXT_OPTIONS_HEADER,
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
    SIGNAL_NAMES = (
        "task_prerun",
        "task_success",
        "task_failure",
        "task_retry",
        "before_task_publish",
        "after_task_publish",
        "worker_process_shutdown",
    )

    def _build_fake_celery(self):
        fake_signals = SimpleNamespace(
            **{signal_name: FakeSignal() for signal_name in self.SIGNAL_NAMES}
        )
        fake_celery = ModuleType("celery")
        fake_celery.signals = fake_signals
        fake_celery.__version__ = "5.0.0"
        return fake_signals, fake_celery

    def _assert_signal_counts(
        self, fake_signals, expected_connected, expected_disconnected=None
    ):
        for signal_name in self.SIGNAL_NAMES:
            self.assertEqual(
                len(getattr(fake_signals, signal_name).connected),
                expected_connected,
                f"{signal_name} connected count mismatch",
            )
            if expected_disconnected is not None:
                self.assertEqual(
                    len(getattr(fake_signals, signal_name).disconnected),
                    expected_disconnected,
                    f"{signal_name} disconnected count mismatch",
                )

    def test_instrument_is_idempotent(self):
        fake_signals, fake_celery = self._build_fake_celery()

        integration = PosthogCeleryIntegration()

        with (
            patch.dict("sys.modules", {"celery": fake_celery}),
            patch("posthog.integrations.celery.atexit.register") as mock_register,
        ):
            integration.instrument()
            integration.instrument()

        self._assert_signal_counts(fake_signals, expected_connected=1)
        mock_register.assert_called_once_with(integration.shutdown)
        self.assertEqual(
            fake_signals.worker_process_shutdown.connected[0][0],
            integration._on_worker_process_shutdown,
        )

    def test_instrument_and_uninstrument_connect_signals(self):
        fake_signals, fake_celery = self._build_fake_celery()

        integration = PosthogCeleryIntegration()

        with (
            patch.dict("sys.modules", {"celery": fake_celery}),
            patch("posthog.integrations.celery.atexit.register"),
            patch("posthog.integrations.celery.atexit.unregister") as mock_unregister,
        ):
            integration.instrument()
            integration.uninstrument()

        self._assert_signal_counts(
            fake_signals, expected_connected=1, expected_disconnected=1
        )
        mock_unregister.assert_called_once_with(integration.shutdown)
        self.assertEqual(
            fake_signals.worker_process_shutdown.disconnected[0],
            integration._on_worker_process_shutdown,
        )

    def test_shutdown_uninstruments_and_flushes_client(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        with (
            patch.object(integration, "_disconnect_signals") as mock_disconnect,
            patch.object(integration, "uninstrument") as mock_uninstrument,
        ):
            integration.shutdown()

        mock_disconnect.assert_called_once_with()
        mock_uninstrument.assert_called_once_with()
        mock_client.flush.assert_called_once_with()
        self.assertTrue(integration._shut_down)

    def test_shutdown_falls_back_to_global_posthog_flush(self):
        integration = PosthogCeleryIntegration(client=None)

        with (
            patch.object(integration, "_disconnect_signals") as mock_disconnect,
            patch.object(integration, "uninstrument") as mock_uninstrument,
            patch("posthog.flush") as mock_flush,
        ):
            integration.shutdown()

        mock_disconnect.assert_called_once_with()
        mock_uninstrument.assert_called_once_with()
        mock_flush.assert_called_once_with()
        self.assertTrue(integration._shut_down)

    def test_shutdown_is_idempotent(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        with (
            patch.object(integration, "_disconnect_signals") as mock_disconnect,
            patch.object(integration, "uninstrument") as mock_uninstrument,
        ):
            integration.shutdown()
            integration.shutdown()

        mock_disconnect.assert_called_once_with()
        mock_uninstrument.assert_called_once_with()
        mock_client.flush.assert_called_once_with()

    def test_shutdown_keeps_atexit_registration_when_flush_fails(self):
        fake_signals, fake_celery = self._build_fake_celery()

        mock_client = Mock()
        mock_client.flush.side_effect = RuntimeError("flush failed")
        integration = PosthogCeleryIntegration(client=mock_client)

        with (
            patch.dict("sys.modules", {"celery": fake_celery}),
            patch("posthog.integrations.celery.atexit.register"),
            patch("posthog.integrations.celery.atexit.unregister") as mock_unregister,
        ):
            integration.instrument()
            integration.shutdown()

        self.assertFalse(integration._shut_down)
        self.assertTrue(integration._signals is None)
        self.assertFalse(integration._instrumented)
        mock_unregister.assert_not_called()
        self.assertEqual(len(fake_signals.worker_process_shutdown.disconnected), 1)

    def test_reinstrument_after_shutdown_allows_shutdown_again(self):
        fake_signals, fake_celery = self._build_fake_celery()

        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        with (
            patch.dict("sys.modules", {"celery": fake_celery}),
            patch("posthog.integrations.celery.atexit.register") as mock_register,
            patch("posthog.integrations.celery.atexit.unregister") as mock_unregister,
        ):
            integration.instrument()
            integration.shutdown()
            integration.instrument()
            integration.shutdown()

        self.assertEqual(mock_client.flush.call_count, 2)
        self.assertEqual(mock_register.call_count, 2)
        self.assertEqual(mock_unregister.call_count, 2)
        self._assert_signal_counts(
            fake_signals, expected_connected=2, expected_disconnected=2
        )

    def test_worker_process_shutdown_hook_calls_shutdown(self):
        integration = PosthogCeleryIntegration(client=Mock())

        with patch.object(integration, "shutdown") as mock_shutdown:
            integration._on_worker_process_shutdown()

        mock_shutdown.assert_called_once_with()

    def test_before_task_publish_propagates_context_headers(self):
        integration = PosthogCeleryIntegration()
        headers = {}

        with contexts.new_context(fresh=True):
            contexts.identify_context("distinct-123")
            contexts.set_context_session("session-456")
            contexts.tag("request_id", "abc")
            contexts.set_context_option("cookieless_mode", True)

            integration._on_before_task_publish(sender="test.task", headers=headers)

        self.assertEqual(headers[CONTEXT_DISTINCT_ID_HEADER], "distinct-123")
        self.assertEqual(headers[CONTEXT_SESSION_ID_HEADER], "session-456")
        self.assertEqual(
            json.loads(headers[CONTEXT_TAGS_HEADER]), {"request_id": "abc"}
        )
        self.assertEqual(
            json.loads(headers[CONTEXT_OPTIONS_HEADER]), {"cookieless_mode": True}
        )

        # celery#4875: headers must also be nested inside headers["headers"]
        # so they survive to task.request.headers on the worker
        inner = headers["headers"]
        self.assertEqual(inner[CONTEXT_DISTINCT_ID_HEADER], "distinct-123")
        self.assertEqual(inner[CONTEXT_SESSION_ID_HEADER], "session-456")
        self.assertEqual(json.loads(inner[CONTEXT_TAGS_HEADER]), {"request_id": "abc"})
        self.assertEqual(
            json.loads(inner[CONTEXT_OPTIONS_HEADER]), {"cookieless_mode": True}
        )

    def test_before_task_publish_skips_options_header_without_context_options(self):
        integration = PosthogCeleryIntegration()
        headers = {}

        with contexts.new_context(fresh=True):
            contexts.tag("request_id", "abc")
            integration._on_before_task_publish(sender="test.task", headers=headers)

        self.assertNotIn(CONTEXT_OPTIONS_HEADER, headers)
        self.assertNotIn(CONTEXT_OPTIONS_HEADER, headers["headers"])

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
            contexts.set_context_option("process_person_profile", False)
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
        self.assertEqual(
            contexts.get_context_options(), {"process_person_profile": False}
        )

        # close out the task lifecycle so the test doesn't leave context hanging around
        integration._on_task_success(sender=task)

    def test_before_task_publish_respects_propagate_context_flag(self):
        integration = PosthogCeleryIntegration(propagate_context=False)
        headers = {}

        with contexts.new_context(fresh=True):
            contexts.identify_context("distinct-123")
            contexts.set_context_session("session-456")
            contexts.tag("request_id", "abc")
            contexts.set_context_option("process_person_profile", False)

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

        # cleanup to not leave context hanging around
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
            delivery_info={
                "exchange": "celery",
                "routing_key": "default",
                "queue": "default",
            },
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

        with patch(
            "posthog.integrations.celery.time.monotonic", side_effect=[100, 100.125]
        ):
            integration._on_task_prerun(sender=task, task_id="task-t")
            integration._on_task_success(sender=task)

        completed_call = [
            c
            for c in mock_client.capture.call_args_list
            if c.args[0] == "celery task success"
        ]
        self.assertEqual(len(completed_call), 1)
        self.assertEqual(
            completed_call[0].kwargs["properties"]["celery_task_duration_ms"], 125.0
        )

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

        with patch(
            "posthog.integrations.celery.time.monotonic", side_effect=[100, 100.125]
        ):
            integration._on_task_prerun(sender=task, task_id="task-f")
            integration._on_task_failure(
                sender=task, task_id="task-f", exception=ValueError("boom")
            )

        failed_call = [
            c
            for c in mock_client.capture.call_args_list
            if c.args[0] == "celery task failure"
        ]
        self.assertEqual(len(failed_call), 1)
        self.assertEqual(
            failed_call[0].kwargs["properties"]["celery_task_duration_ms"], 125.0
        )

    def test_task_failure_captures_exception_and_failure_event(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(client=mock_client)

        task = SimpleNamespace(
            name="app.tasks.failing", request=SimpleNamespace(delivery_info={})
        )
        exception = ValueError("task failed")

        integration._on_task_failure(
            sender=task,
            task_id="task-2",
            exception=exception,
        )

        mock_client.capture_exception.assert_called_once_with(
            exception,
            _capture_metadata={
                "level": "error",
                "source": "celery.task_failure",
                "mechanism": {"type": "task", "handled": False},
            },
        )
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
            c
            for c in mock_client.capture.call_args_list
            if c.args[0] == "celery task failure"
        ]
        self.assertEqual(len(failure_calls), 1)
        props = failure_calls[0].kwargs["properties"]
        self.assertEqual(props["error_type"], "ValueError")
        self.assertEqual(props["error_message"], "task failed")

    def test_task_failure_skips_exception_capture_when_disabled(self):
        mock_client = Mock()
        integration = PosthogCeleryIntegration(
            client=mock_client, capture_exceptions=False
        )

        task = SimpleNamespace(
            name="app.tasks.failing", request=SimpleNamespace(delivery_info={})
        )
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

        task = SimpleNamespace(
            name="app.tasks.retrying", request=SimpleNamespace(delivery_info={})
        )

        integration._on_task_retry(
            sender=task,
            task_id="task-retry",
            reason=ConnectionError("broker down"),
        )

        event_names = [call.args[0] for call in mock_client.capture.call_args_list]
        self.assertIn("celery task retry", event_names)
        retry_call = [
            c
            for c in mock_client.capture.call_args_list
            if c.args[0] == "celery task retry"
        ][0]
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

    def test_task_filter_applies_to_failure_exception_and_lifecycle_event(self):
        mock_client = Mock()
        task_filter = Mock(return_value=False)
        integration = PosthogCeleryIntegration(
            client=mock_client,
            task_filter=task_filter,
        )
        request = SimpleNamespace(headers={}, delivery_info={})
        task = SimpleNamespace(name="app.tasks.filtered_failure", request=request)
        exception = ValueError("task failed")
        context_before = contexts._get_current_context()

        integration._on_task_prerun(sender=task, task_id="task-filtered-failure")
        task_filter.reset_mock()
        integration._on_task_failure(
            sender=task,
            task_id="task-filtered-failure",
            exception=exception,
        )

        task_filter.assert_called_once()
        mock_client.capture.assert_not_called()
        mock_client.capture_exception.assert_not_called()
        self.assertIs(contexts._get_current_context(), context_before)

    def test_task_filter_applies_to_retry_lifecycle_event(self):
        mock_client = Mock()
        task_filter = Mock(return_value=False)
        integration = PosthogCeleryIntegration(
            client=mock_client,
            task_filter=task_filter,
        )
        request = SimpleNamespace(headers={}, delivery_info={})
        task = SimpleNamespace(name="app.tasks.filtered_retry", request=request)
        context_before = contexts._get_current_context()

        integration._on_task_prerun(sender=task, task_id="task-filtered-retry")
        task_filter.reset_mock()
        integration._on_task_retry(
            sender=task,
            task_id="task-filtered-retry",
            reason=ConnectionError("broker down"),
        )

        task_filter.assert_called_once()
        mock_client.capture.assert_not_called()
        mock_client.capture_exception.assert_not_called()
        self.assertIs(contexts._get_current_context(), context_before)

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
        mock_client.capture_exception.assert_called_once_with(
            exception,
            _capture_metadata={
                "level": "error",
                "source": "celery.task_failure",
                "mechanism": {"type": "task", "handled": False},
            },
        )

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
            integration._capture_event(
                "celery task started", properties={"celery_task_id": "t1"}
            )

        mock_capture.assert_called_once_with(
            "celery task started", properties={"celery_task_id": "t1"}
        )

    def test_capture_exception_falls_back_to_global_capture_exception(self):
        integration = PosthogCeleryIntegration(client=None)
        exception = ValueError("boom")

        with patch("posthog.capture_exception") as mock_capture_exception:
            integration._capture_exception(exception)

        mock_capture_exception.assert_called_once_with(
            exception,
            _capture_metadata={
                "level": "error",
                "source": "celery.task_failure",
                "mechanism": {"type": "task", "handled": False},
            },
        )

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

        with patch.object(
            integration, "_apply_propagated_identity", side_effect=RuntimeError("boom")
        ):
            integration._on_task_prerun(sender=task, task_id="task-leak")

        ctx_after = contexts._get_current_context()
        self.assertIs(ctx_after, ctx_before)

    @parameterized.expand(
        [
            ("tags", CONTEXT_TAGS_HEADER, "_extract_propagated_tags"),
            ("options", CONTEXT_OPTIONS_HEADER, "_extract_propagated_options"),
        ]
    )
    def test_extract_propagated_header_invalid_json_returns_empty_dict(
        self, _name, header, extractor
    ):
        integration = PosthogCeleryIntegration()
        request = SimpleNamespace(headers={header: "{bad json"})

        self.assertEqual(getattr(integration, extractor)(request), {})


def _set_producer_context(distinct_id=None, tags=None, options=None):
    if distinct_id:
        contexts.identify_context(distinct_id)
    for key, value in (tags or {}).items():
        contexts.tag(key, value)
    for key, value in (options or {}).items():
        contexts.set_context_option(key, value)


class TestCeleryContextOptionsOnTheWire(unittest.TestCase):
    """Checks the options worker events send, through a real client."""

    def _client(self, **kwargs):
        sent = []
        client = Client(
            "phc_test",
            send=False,
            before_send=lambda msg: sent.append(msg) or msg,
            **kwargs,
        )
        return client, sent

    def _worker_task(self, headers):
        request = SimpleNamespace(
            headers=headers, delivery_info={}, hostname="worker-1", retries=0
        )
        return SimpleNamespace(name="app.tasks.example", request=request)

    def _run_worker(self, integration, client, headers, in_task=None):
        task = self._worker_task(headers)
        integration._on_task_prerun(sender=task, task_id="task-1")
        if in_task:
            in_task()
        client.capture("in task")
        integration._on_task_success(sender=task)

    def _publish_and_run(
        self, producer, super_options=None, in_task=None, propagate_context=True
    ):
        kwargs = {"super_options": super_options} if super_options else {}
        client, sent = self._client(**kwargs)
        integration = PosthogCeleryIntegration(
            client=client, propagate_context=propagate_context
        )
        headers = {}
        with contexts.new_context(fresh=True, client=client):
            _set_producer_context(**producer)
            integration._on_before_task_publish(sender="t", headers=headers)
        self._run_worker(integration, client, headers.get("headers", {}), in_task)
        return {
            event["event"]: event["options"]
            for event in (_to_v1_event(msg) for msg in sent)
        }

    @parameterized.expand(
        [
            (
                "all_four_options",
                {
                    "distinct_id": "user-1",
                    "options": {
                        "process_person_profile": False,
                        "cookieless_mode": True,
                        "disable_skew_correction": True,
                        "product_tour_id": "tour-1",
                    },
                },
                None,
                {
                    "process_person_profile": False,
                    "cookieless_mode": True,
                    "disable_skew_correction": True,
                    "product_tour_id": "tour-1",
                },
            ),
            (
                "option_beats_legacy_tag_on_same_key",
                {
                    "distinct_id": "user-1",
                    "tags": {"$process_person_profile": True},
                    "options": {"process_person_profile": False},
                },
                None,
                {"process_person_profile": False},
            ),
            (
                "legacy_tag_only",
                {"distinct_id": "user-1", "tags": {"$process_person_profile": False}},
                None,
                {"process_person_profile": False},
            ),
            (
                "context_option_beats_worker_super_options",
                {"distinct_id": "user-1", "options": {"cookieless_mode": False}},
                {"cookieless_mode": True},
                {"cookieless_mode": False},
            ),
            (
                "personless_process_person_profile_true",
                {"options": {"process_person_profile": True}},
                None,
                {"process_person_profile": True},
            ),
        ]
    )
    def test_worker_events_send_propagated_options(
        self, _name, producer, super_options, expected
    ):
        options_by_event = self._publish_and_run(producer, super_options)

        self.assertEqual(
            options_by_event,
            {
                "celery task started": expected,
                "in task": expected,
                "celery task success": expected,
            },
        )

    def test_option_set_inside_task_overrides_propagated_option(self):
        options_by_event = self._publish_and_run(
            {"distinct_id": "user-1", "options": {"process_person_profile": False}},
            in_task=lambda: contexts.set_context_option("process_person_profile", True),
        )

        self.assertEqual(
            options_by_event,
            {
                "celery task started": {"process_person_profile": False},
                "in task": {"process_person_profile": True},
                "celery task success": {"process_person_profile": True},
            },
        )

    @parameterized.expand(
        [
            ("missing", None, {}),
            ("bad_json", "{bad json", {}),
            ("not_an_object", '["cookieless_mode"]', {}),
            ("bytes", b'{"cookieless_mode": true}', {"cookieless_mode": True}),
        ]
    )
    def test_worker_handles_options_header(self, _name, raw, expected):
        client, sent = self._client()
        integration = PosthogCeleryIntegration(client=client)
        headers = {CONTEXT_DISTINCT_ID_HEADER: "user-1"}
        if raw is not None:
            headers[CONTEXT_OPTIONS_HEADER] = raw

        self._run_worker(integration, client, headers)

        self.assertEqual(
            [(msg["event"], _to_v1_event(msg)["options"]) for msg in sent],
            [
                ("celery task started", expected),
                ("in task", expected),
                ("celery task success", expected),
            ],
        )

    def test_propagate_context_false_sends_no_options(self):
        options_by_event = self._publish_and_run(
            {"distinct_id": "user-1", "options": {"cookieless_mode": True}},
            propagate_context=False,
        )

        # The distinct ID is not propagated either, so worker events are personless.
        personless = {"process_person_profile": False}
        self.assertEqual(
            options_by_event,
            {
                "celery task started": personless,
                "in task": personless,
                "celery task success": personless,
            },
        )
