import logging
import asyncio
import time
import unittest
from datetime import datetime
from uuid import UUID, uuid4

from unittest import mock
from parameterized import parameterized

from posthog.capture_compression import CaptureCompression
from posthog.client import Client
from posthog.contexts import get_context_session_id, new_context, set_context_session
from posthog.request import APIError, GetResponse
from posthog.test.logging_helpers import capture_message_only_logs
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.types import FeatureFlag, LegacyFlagMetadata
from posthog.version import VERSION
from posthog.contexts import tag


class TestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This ensures no real HTTP POST requests are made
        cls.client_post_patcher = mock.patch("posthog.client.batch_post")
        cls.consumer_post_patcher = mock.patch("posthog.consumer.batch_post")
        cls.client_post_patcher.start()
        cls.consumer_post_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.client_post_patcher.stop()
        cls.consumer_post_patcher.stop()

    def set_fail(self, e, batch):
        """Mark the failure handler"""
        print("FAIL", e, batch)  # noqa: T201
        self.failed = True

    def setUp(self):
        self.failed = False
        self.client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail)

    def test_requires_api_key(self):
        self.assertRaises(TypeError, Client)

    @parameterized.expand(
        [
            ("valid_key", " \nphc_validkey\t ", "phc_validkey", False, False),
            ("whitespace_only", " \n\t ", "", True, True),
            ("empty_string", "", "", True, True),
        ]
    )
    def test_trims_api_key_whitespace(
        self, _, raw_api_key, expected_api_key, expected_disabled, expect_error_log
    ):
        with mock.patch.object(Client.log, "error") as mock_error:
            client = Client(raw_api_key, send=False)

        self.assertEqual(client.api_key, expected_api_key)
        self.assertEqual(client.disabled, expected_disabled)
        if expect_error_log:
            mock_error.assert_called_once_with(
                "api_key is empty after trimming whitespace; check your project API key"
            )
        else:
            mock_error.assert_not_called()

    def test_trims_host_and_personal_api_key_whitespace(self):
        client = Client(
            FAKE_TEST_API_KEY,
            host=" \nhttps://eu.posthog.com/\t ",
            personal_api_key=" \n\t ",
            send=False,
        )

        self.assertEqual(client.raw_host, "https://eu.posthog.com/")
        self.assertEqual(client.host, "https://eu.i.posthog.com")
        self.assertIsNone(client.personal_api_key)

    def test_client_with_empty_api_key_is_noop(self):
        client = Client("", send=False)

        self.assertIsNone(client.capture("event", distinct_id="distinct_id"))

    def _reset_duplicate_client_registry(self):
        Client._client_registry.clear()
        Client._duplicate_client_warnings.clear()

    def test_warns_once_on_duplicate_async_client_same_key_and_host(self):
        self._reset_duplicate_client_registry()
        self.addCleanup(self._reset_duplicate_client_registry)
        host = "https://us.i.posthog.com"
        registry_key = (FAKE_TEST_API_KEY, host)

        with (
            mock.patch("posthog.client.atexit.register"),
            mock.patch("posthog.client.Consumer.start"),
            mock.patch.object(Client.log, "warning") as mock_warning,
        ):
            first = Client(FAKE_TEST_API_KEY, host=host)
            second = Client(FAKE_TEST_API_KEY, host=host)
            third = Client(FAKE_TEST_API_KEY, host=host)

            self.assertIsNot(first, second)
            self.assertIsNot(second, third)
            mock_warning.assert_called_once_with(
                "Multiple active PostHog clients detected for the same project "
                "API key and host. Reuse one Posthog instance per app or "
                "process when possible to avoid competing background queues "
                "and missed shutdown flushes. Multiple clients are supported "
                "when intentional."
            )

            first.shutdown()
            second.shutdown()
            third.shutdown()

            self.assertNotIn(registry_key, Client._client_registry)
            self.assertNotIn(registry_key, Client._duplicate_client_warnings)

            fourth = Client(FAKE_TEST_API_KEY, host=host)
            fifth = Client(FAKE_TEST_API_KEY, host=host)

            self.assertEqual(mock_warning.call_count, 2)

            fourth.shutdown()
            fifth.shutdown()

    @parameterized.expand(
        [
            ("different_host", {"host": "https://two.example.com"}),
            ("sync_mode", {"host": "https://one.example.com", "sync_mode": True}),
            ("send_disabled", {"host": "https://one.example.com", "send": False}),
        ]
    )
    def test_duplicate_client_warning_allows_intentional_multi_client_cases(
        self, _, duplicate_kwargs
    ):
        self._reset_duplicate_client_registry()
        self.addCleanup(self._reset_duplicate_client_registry)

        with (
            mock.patch("posthog.client.atexit.register"),
            mock.patch("posthog.client.Consumer.start"),
            mock.patch.object(Client.log, "warning") as mock_warning,
        ):
            first = Client(FAKE_TEST_API_KEY, host="https://one.example.com")
            duplicate = Client(FAKE_TEST_API_KEY, **duplicate_kwargs)

            self.assertIsNot(first, duplicate)
            mock_warning.assert_not_called()

            first.shutdown()
            duplicate.shutdown()

    def test_message_only_info_logs_include_posthog_prefix(self):
        self.client.flag_cache = mock.Mock()
        self.client.flag_cache.get_stale_cached_flag.return_value = mock.Mock()

        with capture_message_only_logs(level=logging.INFO) as logs:
            self.client._get_stale_flag_fallback("distinct_id", "flag-key")

        self.assertEqual(
            logs.getvalue().strip(),
            "[PostHog] [FEATURE FLAGS] Using stale cached value for flag flag-key",
        )

    def test_message_only_logs_do_not_duplicate_existing_posthog_prefix(self):
        with capture_message_only_logs(level=logging.ERROR) as logs:
            self.client.log.error("[PostHog] already prefixed")

        self.assertEqual(logs.getvalue().strip(), "[PostHog] already prefixed")

    @mock.patch("posthog.client.get")
    def test_disabled_client_does_not_load_feature_flags(self, patch_get):
        client = Client("", personal_api_key="test", send=False)

        client.load_feature_flags()

        patch_get.assert_not_called()
        self.assertEqual(client.feature_flags, [])
        self.assertIsNone(client.poller)

    @mock.patch("posthog.client.flags")
    def test_disabled_client_does_not_get_flags_decision(self, patch_flags):
        client = Client("", send=False)

        self.assertEqual(client.get_flags_decision("distinct_id")["flags"], {})
        self.assertEqual(client.get_feature_variants("distinct_id"), {})
        self.assertEqual(client.get_feature_payloads("distinct_id"), {})
        self.assertEqual(
            client.get_feature_flags_and_payloads("distinct_id"),
            {"featureFlags": {}, "featureFlagPayloads": {}},
        )
        self.assertIsNone(
            client.capture("event", distinct_id="distinct_id", send_feature_flags=True)
        )
        patch_flags.assert_not_called()

    @mock.patch("posthog.client.flags")
    def test_client_flag_helpers_return_defaults_on_api_error(self, patch_flags):
        patch_flags.side_effect = APIError(401, "Unauthorized")
        client = Client(FAKE_TEST_API_KEY, send=False)

        test_cases = [
            (
                "get_flags_decision",
                lambda: client.get_flags_decision("distinct_id")["flags"],
                {},
            ),
            (
                "get_feature_variants",
                lambda: client.get_feature_variants("distinct_id"),
                {},
            ),
            (
                "get_feature_payloads",
                lambda: client.get_feature_payloads("distinct_id"),
                {},
            ),
            (
                "get_feature_flags_and_payloads",
                lambda: client.get_feature_flags_and_payloads("distinct_id"),
                {"featureFlags": {}, "featureFlagPayloads": {}},
            ),
        ]

        for method_name, call_helper, expected in test_cases:
            with self.subTest(method=method_name):
                self.assertEqual(call_helper(), expected)

    def test_empty_flush(self):
        self.client.flush()

    def test_flush_timeout_returns_when_queue_does_not_drain(self):
        client = Client(FAKE_TEST_API_KEY, send=False, thread=0)
        client.queue.put({"event": "stuck"})

        start = time.monotonic()
        with self.assertLogs("posthog", level="WARNING") as logs:
            client.flush(timeout_seconds=0.01)

        self.assertLess(time.monotonic() - start, 1)
        self.assertFalse(client.queue.empty())
        self.assertIn("flush timed out", logs.output[0])

        client.queue.get_nowait()
        client.queue.task_done()

    def test_flush_logs_and_returns_on_unexpected_error(self):
        client = Client(FAKE_TEST_API_KEY, send=False, thread=0)
        client.queue.put({"event": "stuck"})

        with mock.patch.object(
            client.queue.all_tasks_done,
            "wait",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("posthog", level="ERROR") as logs:
                client.flush(timeout_seconds=1)

        self.assertIn("error flushing queue", logs.output[0])

        client.queue.get_nowait()
        client.queue.task_done()

    def test_basic_capture(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.capture("python test event", distinct_id="distinct_id")
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertEqual(msg["properties"]["$is_server"], True)
            # these will change between platforms so just asssert on presence here
            assert msg["properties"]["$python_runtime"] == mock.ANY
            assert msg["properties"]["$python_version"] == mock.ANY
            assert msg["properties"]["$os"] == mock.ANY
            assert msg["properties"]["$os_version"] == mock.ANY

    def test_capture_omits_is_server_when_disabled(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                sync_mode=True,
                is_server=False,
            )
            client.capture("python test event", distinct_id="distinct_id")
            self.assertFalse(self.failed)

            msg = mock_post.call_args[1]["batch"][0]
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertNotIn("$is_server", msg["properties"])

    def test_is_server_not_overridden_by_super_properties(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                sync_mode=True,
                super_properties={"$is_server": False},
            )
            client.capture("python test event", distinct_id="distinct_id")
            self.assertFalse(self.failed)

            msg = mock_post.call_args[1]["batch"][0]
            self.assertEqual(msg["properties"]["$is_server"], True)

    def test_basic_capture_with_uuid(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            uuid = str(uuid4())
            msg_uuid = client.capture(
                "python test event", distinct_id="distinct_id", uuid=uuid
            )
            self.assertEqual(msg_uuid, uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertEqual(msg["uuid"], uuid)
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)

    def test_basic_capture_with_uuid_object(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            uuid = UUID("00000000-0000-4000-8000-000000000002")
            msg_uuid = client.capture(
                "python test event", distinct_id="distinct_id", uuid=uuid
            )
            self.assertEqual(msg_uuid, str(uuid))
            self.assertFalse(self.failed)

            mock_post.assert_called_once()
            msg = mock_post.call_args[1]["batch"][0]
            self.assertEqual(msg["uuid"], str(uuid))

    @parameterized.expand(
        [
            ("empty string", ""),
            ("invalid string", "not-a-uuid"),
            ("short string", "1234"),
            ("integer", 123),
        ]
    )
    def test_capture_with_invalid_uuid_logs_and_falls_back_to_generated_uuid(
        self, _name, invalid_uuid
    ):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            with self.assertLogs("posthog", level="ERROR") as logs:
                msg_uuid = client.capture(
                    "python test event", distinct_id="distinct_id", uuid=invalid_uuid
                )

            self.assertIsNotNone(msg_uuid)
            UUID(msg_uuid)
            mock_post.assert_called_once()
            msg = mock_post.call_args[1]["batch"][0]
            self.assertEqual(msg["uuid"], msg_uuid)
            self.assertNotEqual(msg["uuid"], str(invalid_uuid))
            self.assertTrue(
                any(
                    f"Invalid event uuid {invalid_uuid!r}" in message
                    and "Expected a valid UUID string or uuid.UUID instance" in message
                    and "Falling back to a generated UUID" in message
                    for message in logs.output
                )
            )

    @parameterized.expand(
        [
            ("empty string", ""),
            ("invalid string", "not-a-uuid"),
            ("short string", "1234"),
            ("integer", 123),
        ]
    )
    def test_capture_with_invalid_uuid_falls_back_in_debug(self, _name, invalid_uuid):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, debug=True, sync_mode=True)
            with self.assertLogs("posthog", level="ERROR"):
                msg_uuid = client.capture(
                    "python test event", distinct_id="distinct_id", uuid=invalid_uuid
                )

            self.assertIsNotNone(msg_uuid)
            UUID(msg_uuid)
            mock_post.assert_called_once()

    def test_basic_capture_with_project_api_key(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                project_api_key=FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                sync_mode=True,
            )

            msg_uuid = client.capture("python test event", distinct_id="distinct_id")
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)

    def test_basic_super_properties(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                super_properties={"source": "repo-name"},
                sync_mode=True,
            )

            msg_uuid = client.capture("python test event", distinct_id="distinct_id")
            self.assertIsNotNone(msg_uuid)

            # Check the enqueued message
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertEqual(msg["properties"]["source"], "repo-name")

    def test_basic_capture_exception(self):
        with mock.patch.object(Client, "capture", return_value=None) as patch_capture:
            client = self.client
            exception = Exception("test exception")
            client.capture_exception(exception, distinct_id="distinct_id")

            self.assertTrue(patch_capture.called)
            capture_call = patch_capture.call_args
            self.assertEqual(capture_call[0][0], "$exception")
            self.assertEqual(capture_call[1]["distinct_id"], "distinct_id")

    def test_basic_capture_exception_with_distinct_id(self):
        with mock.patch.object(Client, "capture", return_value=None) as patch_capture:
            client = self.client
            exception = Exception("test exception")
            client.capture_exception(exception, distinct_id="distinct_id")

            self.assertTrue(patch_capture.called)
            capture_call = patch_capture.call_args
            self.assertEqual(capture_call[0][0], "$exception")
            self.assertEqual(capture_call[1]["distinct_id"], "distinct_id")

    def test_basic_capture_exception_with_correct_host_generation(self):
        with mock.patch.object(Client, "capture", return_value=None) as patch_capture:
            client = Client(
                FAKE_TEST_API_KEY, on_error=self.set_fail, host="https://aloha.com"
            )
            exception = Exception("test exception")
            client.capture_exception(exception, distinct_id="distinct_id")

            self.assertTrue(patch_capture.called)
            call = patch_capture.call_args
            self.assertEqual(call[0][0], "$exception")
            self.assertEqual(call[1]["distinct_id"], "distinct_id")

    def test_basic_capture_exception_with_correct_host_generation_for_server_hosts(
        self,
    ):
        with mock.patch.object(Client, "capture", return_value=None) as patch_capture:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                host="https://app.posthog.com",
            )
            exception = Exception("test exception")
            client.capture_exception(exception, distinct_id="distinct_id")

            self.assertTrue(patch_capture.called)
            capture_call = patch_capture.call_args
            self.assertEqual(capture_call[0][0], "$exception")
            self.assertEqual(capture_call[1]["distinct_id"], "distinct_id")

    def test_basic_capture_exception_with_no_exception_given(self):
        with mock.patch.object(Client, "capture", return_value=None) as patch_capture:
            client = self.client
            try:
                raise Exception("test exception")
            except Exception:
                client.capture_exception(None, distinct_id="distinct_id")

            self.assertTrue(patch_capture.called)
            capture_call = patch_capture.call_args
            print(capture_call)
            self.assertEqual(capture_call[1]["distinct_id"], "distinct_id")
            self.assertEqual(capture_call[0][0], "$exception")
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["mechanism"][
                    "type"
                ],
                "generic",
            )
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["mechanism"][
                    "handled"
                ],
                True,
            )
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["module"], None
            )
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["type"], "Exception"
            )
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["value"],
                "test exception",
            )
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["stacktrace"][
                    "type"
                ],
                "raw",
            )
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["stacktrace"][
                    "frames"
                ][0]["filename"],
                "posthog/test/test_client.py",
            )
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["stacktrace"][
                    "frames"
                ][0]["function"],
                "test_basic_capture_exception_with_no_exception_given",
            )
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["stacktrace"][
                    "frames"
                ][0]["module"],
                "posthog.test.test_client",
            )
            self.assertEqual(
                capture_call[1]["properties"]["$exception_list"][0]["stacktrace"][
                    "frames"
                ][0]["in_app"],
                True,
            )

    def test_basic_capture_exception_with_no_exception_happening(self):
        with mock.patch.object(Client, "capture", return_value=None) as patch_capture:
            with self.assertLogs("posthog", level="WARNING") as logs:
                client = self.client
                client.capture_exception(None)

                self.assertFalse(patch_capture.called)
                self.assertEqual(
                    logs.output[0],
                    "WARNING:posthog:[PostHog] No exception information available",
                )

    def test_capture_exception_logs_when_enabled(self):
        client = Client(FAKE_TEST_API_KEY, log_captured_exceptions=True)
        with self.assertLogs("posthog", level="ERROR") as logs:
            client.capture_exception(
                Exception("test exception"), distinct_id="distinct_id"
            )
            self.assertEqual(
                logs.output[0], "ERROR:posthog:[PostHog] test exception\nNoneType: None"
            )

    @mock.patch("posthog.client.flags")
    def test_basic_capture_with_feature_flags(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"beta-feature": "random-variant"}}

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )
            msg_uuid = client.capture(
                "python test event", distinct_id="distinct_id", send_feature_flags=True
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertEqual(
                msg["properties"]["$feature/beta-feature"], "random-variant"
            )
            self.assertEqual(
                msg["properties"]["$active_feature_flags"], ["beta-feature"]
            )

            self.assertEqual(patch_flags.call_count, 1)

    @mock.patch("posthog.client.flags")
    def test_basic_capture_with_locally_evaluated_feature_flags(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"beta-feature": "random-variant"}}

        multivariate_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "beta-feature-local",
            "active": True,
            "rollout_percentage": 100,
            "filters": {
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "email",
                                "type": "person",
                                "value": "test@posthog.com",
                                "operator": "exact",
                            }
                        ],
                        "rollout_percentage": 100,
                    },
                    {
                        "rollout_percentage": 50,
                    },
                ],
                "multivariate": {
                    "variants": [
                        {
                            "key": "first-variant",
                            "name": "First Variant",
                            "rollout_percentage": 50,
                        },
                        {
                            "key": "second-variant",
                            "name": "Second Variant",
                            "rollout_percentage": 25,
                        },
                        {
                            "key": "third-variant",
                            "name": "Third Variant",
                            "rollout_percentage": 25,
                        },
                    ]
                },
                "payloads": {
                    "first-variant": "some-payload",
                    "third-variant": {"a": "json"},
                },
            },
        }
        basic_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "person-flag",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "region",
                                "operator": "exact",
                                "value": ["USA"],
                                "type": "person",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ],
                "payloads": {"true": 300},
            },
        }
        false_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "false-flag",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "properties": [],
                        "rollout_percentage": 0,
                    }
                ],
                "payloads": {"true": 300},
            },
        }

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )
            client.feature_flags = [multivariate_flag, basic_flag, false_flag]

            msg_uuid = client.capture(
                "python test event", distinct_id="distinct_id", send_feature_flags=True
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertEqual(
                msg["properties"]["$feature/beta-feature-local"], "third-variant"
            )
            self.assertEqual(msg["properties"]["$feature/false-flag"], False)
            self.assertEqual(
                msg["properties"]["$active_feature_flags"], ["beta-feature-local"]
            )
            assert "$feature/beta-feature" not in msg["properties"]

            self.assertEqual(patch_flags.call_count, 0)

        # test that flags are not evaluated without local evaluation
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )
            client.feature_flags = []
            msg_uuid = client.capture("python test event", distinct_id="distinct_id")
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            assert "$feature/beta-feature" not in msg["properties"]
            assert "$feature/beta-feature-local" not in msg["properties"]
            assert "$feature/false-flag" not in msg["properties"]
            assert "$active_feature_flags" not in msg["properties"]

    @mock.patch("posthog.client.get")
    def test_load_feature_flags_quota_limited(self, patch_get):
        mock_response = {
            "type": "quota_limited",
            "detail": "You have exceeded your feature flag request quota",
            "code": "payment_required",
        }
        patch_get.side_effect = APIError(402, mock_response["detail"])

        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        with self.assertLogs("posthog", level="WARNING") as logs:
            client._load_feature_flags()

            self.assertEqual(client.feature_flags, [])
            self.assertEqual(client.feature_flags_by_key, {})
            self.assertEqual(client.group_type_mapping, {})
            self.assertEqual(client.cohorts, {})
            self.assertIn("PostHog feature flags quota limited", logs.output[0])

    @mock.patch("posthog.client.get")
    def test_load_feature_flags_unauthorized(self, patch_get):
        patch_get.side_effect = APIError(401, "Unauthorized")

        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        with self.assertLogs("posthog", level="ERROR") as logs:
            client._load_feature_flags()

            self.assertEqual(client.feature_flags, [])
            self.assertEqual(client.feature_flags_by_key, {})
            self.assertEqual(client.group_type_mapping, {})
            self.assertEqual(client.cohorts, {})
            self.assertIn("Unauthorized", logs.output[0])
            self.assertIn("project_api_key", logs.output[0])
            self.assertIn("personal_api_key", logs.output[0])

    @mock.patch("posthog.client.flags")
    def test_dont_override_capture_with_local_flags(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"beta-feature": "random-variant"}}

        multivariate_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "beta-feature-local",
            "active": True,
            "rollout_percentage": 100,
            "filters": {
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "email",
                                "type": "person",
                                "value": "test@posthog.com",
                                "operator": "exact",
                            }
                        ],
                        "rollout_percentage": 100,
                    },
                    {
                        "rollout_percentage": 50,
                    },
                ],
                "multivariate": {
                    "variants": [
                        {
                            "key": "first-variant",
                            "name": "First Variant",
                            "rollout_percentage": 50,
                        },
                        {
                            "key": "second-variant",
                            "name": "Second Variant",
                            "rollout_percentage": 25,
                        },
                        {
                            "key": "third-variant",
                            "name": "Third Variant",
                            "rollout_percentage": 25,
                        },
                    ]
                },
                "payloads": {
                    "first-variant": "some-payload",
                    "third-variant": {"a": "json"},
                },
            },
        }
        basic_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "person-flag",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "region",
                                "operator": "exact",
                                "value": ["USA"],
                                "type": "person",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ],
                "payloads": {"true": 300},
            },
        }

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )
            client.feature_flags = [multivariate_flag, basic_flag]

            msg_uuid = client.capture(
                "python test event",
                distinct_id="distinct_id",
                properties={"$feature/beta-feature-local": "my-custom-variant"},
                send_feature_flags=True,
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertEqual(
                msg["properties"]["$feature/beta-feature-local"], "my-custom-variant"
            )
            self.assertEqual(
                msg["properties"]["$active_feature_flags"], ["beta-feature-local"]
            )
            assert "$feature/beta-feature" not in msg["properties"]
            assert "$feature/person-flag" not in msg["properties"]

            self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_basic_capture_with_feature_flags_returns_active_only(self, patch_flags):
        patch_flags.return_value = {
            "featureFlags": {
                "beta-feature": "random-variant",
                "alpha-feature": True,
                "off-feature": False,
            }
        }

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )
            msg_uuid = client.capture(
                "python test event", distinct_id="distinct_id", send_feature_flags=True
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertTrue(msg["properties"]["$geoip_disable"])
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertEqual(
                msg["properties"]["$feature/beta-feature"], "random-variant"
            )
            self.assertEqual(msg["properties"]["$feature/alpha-feature"], True)
            self.assertEqual(
                msg["properties"]["$active_feature_flags"],
                ["beta-feature", "alpha-feature"],
            )

            self.assertEqual(patch_flags.call_count, 1)
            patch_flags.assert_called_with(
                "random_key",
                "https://us.i.posthog.com",
                timeout=3,
                max_retries=1,
                distinct_id="distinct_id",
                groups={},
                person_properties={},
                group_properties={},
                geoip_disable=True,
                device_id=None,
            )

    @parameterized.expand(
        [
            ("default", 1),
            ("disabled", 0),
            ("two_retries", 2),
        ]
    )
    def test_feature_flags_request_max_retries_is_forwarded(
        self, _name, expected_max_retries
    ):
        with mock.patch("posthog.client.flags") as patch_flags:
            patch_flags.return_value = {"featureFlags": {}, "featureFlagPayloads": {}}
            client = Client(
                FAKE_TEST_API_KEY,
                feature_flags_request_max_retries=expected_max_retries,
                personal_api_key=FAKE_TEST_API_KEY,
            )

            client.get_all_flags("distinct_id")

        self.assertEqual(
            patch_flags.call_args.kwargs["max_retries"], expected_max_retries
        )

    @mock.patch("posthog.client.flags")
    def test_basic_capture_with_feature_flags_and_disable_geoip_returns_correctly(
        self, patch_flags
    ):
        patch_flags.return_value = {
            "featureFlags": {
                "beta-feature": "random-variant",
                "alpha-feature": True,
                "off-feature": False,
            }
        }

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                host="https://app.posthog.com",
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                disable_geoip=True,
                feature_flags_request_timeout_seconds=12,
                sync_mode=True,
            )
            msg_uuid = client.capture(
                "python test event",
                distinct_id="distinct_id",
                send_feature_flags=True,
                disable_geoip=False,
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))
            self.assertTrue("$geoip_disable" not in msg["properties"])
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertEqual(
                msg["properties"]["$feature/beta-feature"], "random-variant"
            )
            self.assertEqual(msg["properties"]["$feature/alpha-feature"], True)
            self.assertEqual(
                msg["properties"]["$active_feature_flags"],
                ["beta-feature", "alpha-feature"],
            )

            self.assertEqual(patch_flags.call_count, 1)
            patch_flags.assert_called_with(
                "random_key",
                "https://us.i.posthog.com",
                timeout=12,
                max_retries=1,
                distinct_id="distinct_id",
                groups={},
                person_properties={},
                group_properties={},
                geoip_disable=False,
                device_id=None,
            )

    @mock.patch("posthog.client.flags")
    def test_basic_capture_with_feature_flags_switched_off_doesnt_send_them(
        self, patch_flags
    ):
        patch_flags.return_value = {"featureFlags": {"beta-feature": "random-variant"}}

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )
            msg_uuid = client.capture(
                "python test event", distinct_id="distinct_id", send_feature_flags=False
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertTrue("$feature/beta-feature" not in msg["properties"])
            self.assertTrue("$active_feature_flags" not in msg["properties"])

            self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_capture_with_send_feature_flags_false_and_local_evaluation_doesnt_send_flags(
        self, patch_flags
    ):
        """Test that send_feature_flags=False with local evaluation enabled does NOT send flags"""
        patch_flags.return_value = {"featureFlags": {"beta-feature": "remote-variant"}}

        multivariate_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "beta-feature-local",
            "active": True,
            "rollout_percentage": 100,
            "filters": {
                "groups": [
                    {
                        "rollout_percentage": 100,
                    },
                ],
                "multivariate": {
                    "variants": [
                        {
                            "key": "first-variant",
                            "name": "First Variant",
                            "rollout_percentage": 50,
                        },
                        {
                            "key": "second-variant",
                            "name": "Second Variant",
                            "rollout_percentage": 50,
                        },
                    ]
                },
            },
        }
        simple_flag = {
            "id": 2,
            "name": "Simple Flag",
            "key": "simple-flag",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "rollout_percentage": 100,
                    }
                ],
            },
        }

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )
            client.feature_flags = [multivariate_flag, simple_flag]

            msg_uuid = client.capture(
                "python test event",
                distinct_id="distinct_id",
                send_feature_flags=False,
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertEqual(msg["distinct_id"], "distinct_id")

            # CRITICAL: Verify local flags are NOT included in the event
            self.assertNotIn("$feature/beta-feature-local", msg["properties"])
            self.assertNotIn("$feature/simple-flag", msg["properties"])
            self.assertNotIn("$active_feature_flags", msg["properties"])

            # CRITICAL: Verify the /flags API was NOT called
            self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_capture_with_send_feature_flags_true_and_local_evaluation_uses_local_flags(
        self, patch_flags
    ):
        """Test that send_feature_flags=True with local evaluation enabled uses local flags without API call"""
        patch_flags.return_value = {"featureFlags": {"remote-flag": "remote-variant"}}

        multivariate_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "beta-feature-local",
            "active": True,
            "rollout_percentage": 100,
            "filters": {
                "groups": [
                    {
                        "rollout_percentage": 100,
                    },
                ],
                "multivariate": {
                    "variants": [
                        {
                            "key": "first-variant",
                            "name": "First Variant",
                            "rollout_percentage": 50,
                        },
                        {
                            "key": "second-variant",
                            "name": "Second Variant",
                            "rollout_percentage": 50,
                        },
                    ]
                },
            },
        }
        simple_flag = {
            "id": 2,
            "name": "Simple Flag",
            "key": "simple-flag",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "rollout_percentage": 100,
                    }
                ],
            },
        }

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )
            client.feature_flags = [multivariate_flag, simple_flag]

            msg_uuid = client.capture(
                "python test event",
                distinct_id="distinct_id",
                send_feature_flags=True,
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertEqual(msg["distinct_id"], "distinct_id")

            # Verify local flags are included in the event
            self.assertIn("$feature/beta-feature-local", msg["properties"])
            self.assertIn("$feature/simple-flag", msg["properties"])
            self.assertEqual(msg["properties"]["$feature/simple-flag"], True)

            # Verify active feature flags are set correctly
            active_flags = msg["properties"]["$active_feature_flags"]
            self.assertIn("beta-feature-local", active_flags)
            self.assertIn("simple-flag", active_flags)

            # The remote flag should NOT be included since we used local evaluation
            self.assertNotIn("$feature/remote-flag", msg["properties"])

            # CRITICAL: Verify the /flags API was NOT called
            self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_capture_with_send_feature_flags_options_only_evaluate_locally_true(
        self, patch_flags
    ):
        """Test that SendFeatureFlagsOptions with only_evaluate_locally=True uses local evaluation"""
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )

            # Set up local flags
            client.feature_flags = [
                {
                    "id": 1,
                    "key": "local-flag",
                    "active": True,
                    "filters": {
                        "groups": [
                            {
                                "properties": [{"key": "region", "value": "US"}],
                                "rollout_percentage": 100,
                            }
                        ],
                    },
                }
            ]

            send_options = {
                "only_evaluate_locally": True,
                "person_properties": {"region": "US"},
            }

            msg_uuid = client.capture(
                "test event", distinct_id="distinct_id", send_feature_flags=send_options
            )

            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Verify flags() was not called (no remote evaluation)
            patch_flags.assert_not_called()

            # Check the message includes the local flag
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["properties"]["$feature/local-flag"], True)
            self.assertEqual(msg["properties"]["$active_feature_flags"], ["local-flag"])

    @mock.patch("posthog.client.flags")
    def test_capture_with_send_feature_flags_options_only_evaluate_locally_false(
        self, patch_flags
    ):
        """Test that SendFeatureFlagsOptions with only_evaluate_locally=False forces remote evaluation"""
        patch_flags.return_value = {"featureFlags": {"remote-flag": "remote-value"}}

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )

            send_options = {
                "only_evaluate_locally": False,
                "person_properties": {"plan": "premium"},
                "group_properties": {"company": {"type": "enterprise"}},
            }

            msg_uuid = client.capture(
                "test event",
                distinct_id="distinct_id",
                groups={"company": "acme"},
                send_feature_flags=send_options,
            )

            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Verify flags() was called with the correct properties
            patch_flags.assert_called_once()
            call_args = patch_flags.call_args[1]
            self.assertEqual(call_args["person_properties"], {"plan": "premium"})
            self.assertEqual(
                call_args["group_properties"], {"company": {"type": "enterprise"}}
            )

            # Check the message includes the remote flag
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["properties"]["$feature/remote-flag"], "remote-value")

    @mock.patch("posthog.client.flags")
    def test_capture_with_send_feature_flags_options_default_behavior(
        self, patch_flags
    ):
        """Test that SendFeatureFlagsOptions without only_evaluate_locally defaults to remote evaluation"""
        patch_flags.return_value = {"featureFlags": {"default-flag": "default-value"}}

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )

            send_options = {
                "person_properties": {"subscription": "pro"},
            }

            msg_uuid = client.capture(
                "test event", distinct_id="distinct_id", send_feature_flags=send_options
            )

            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Verify flags() was called (default to remote evaluation)
            patch_flags.assert_called_once()
            call_args = patch_flags.call_args[1]
            self.assertEqual(call_args["person_properties"], {"subscription": "pro"})

            # Check the message includes the flag
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(
                msg["properties"]["$feature/default-flag"], "default-value"
            )

    @mock.patch("posthog.client.flags")
    def test_capture_exception_with_send_feature_flags_options(self, patch_flags):
        """Test that capture_exception also supports SendFeatureFlagsOptions"""
        patch_flags.return_value = {"featureFlags": {"exception-flag": True}}

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )

            send_options = {
                "only_evaluate_locally": False,
                "person_properties": {"user_type": "admin"},
            }

            try:
                raise ValueError("Test exception")
            except ValueError as e:
                msg_uuid = client.capture_exception(
                    e, distinct_id="distinct_id", send_feature_flags=send_options
                )

            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Verify flags() was called with the correct properties
            patch_flags.assert_called_once()
            call_args = patch_flags.call_args[1]
            self.assertEqual(call_args["person_properties"], {"user_type": "admin"})

            # Check the message includes the flag
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "$exception")
            self.assertEqual(msg["properties"]["$feature/exception-flag"], True)

    def test_stringifies_distinct_id(self):
        # A large number that loses precision in node:
        # node -e "console.log(157963456373623802 + 1)" > 157963456373623800
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.capture(
                "python test event", distinct_id=157963456373623802
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["distinct_id"], "157963456373623802")

    def test_advanced_capture(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.capture(
                "python test event",
                distinct_id="distinct_id",
                properties={"property": "value"},
                timestamp=datetime(2014, 9, 3),
                uuid="00000000-0000-4000-8000-000000000001",
            )

            self.assertEqual(msg_uuid, "00000000-0000-4000-8000-000000000001")

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["timestamp"], "2014-09-03T00:00:00+00:00")
            self.assertEqual(msg["properties"]["property"], "value")
            self.assertEqual(msg["event"], "python test event")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertEqual(msg["uuid"], "00000000-0000-4000-8000-000000000001")
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertTrue("$groups" not in msg["properties"])

    def test_groups_capture(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.capture(
                "test_event",
                distinct_id="distinct_id",
                groups={"company": "id:5", "instance": "app.posthog.com"},
            )

            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(
                msg["properties"]["$groups"],
                {"company": "id:5", "instance": "app.posthog.com"},
            )

    def test_basic_set(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.set(
                distinct_id="distinct_id", properties={"trait": "value"}
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["$set"]["trait"], "value")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))
            self.assertEqual(msg["distinct_id"], "distinct_id")

    def test_advanced_set(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.set(
                distinct_id="distinct_id",
                properties={"trait": "value"},
                timestamp=datetime(2014, 9, 3),
                uuid="00000000-0000-4000-8000-000000000001",
            )

            self.assertEqual(msg_uuid, "00000000-0000-4000-8000-000000000001")

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["timestamp"], "2014-09-03T00:00:00+00:00")
            self.assertEqual(msg["$set"]["trait"], "value")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertEqual(msg["uuid"], "00000000-0000-4000-8000-000000000001")
            self.assertEqual(msg["distinct_id"], "distinct_id")

    def test_basic_set_once(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.set_once(
                distinct_id="distinct_id", properties={"trait": "value"}
            )
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["$set_once"]["trait"], "value")
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))
            self.assertEqual(msg["distinct_id"], "distinct_id")

    def test_advanced_set_once(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.set_once(
                distinct_id="distinct_id",
                properties={"trait": "value"},
                timestamp=datetime(2014, 9, 3),
                uuid="00000000-0000-4000-8000-000000000001",
            )

            self.assertEqual(msg_uuid, "00000000-0000-4000-8000-000000000001")

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["timestamp"], "2014-09-03T00:00:00+00:00")
            self.assertEqual(msg["$set_once"]["trait"], "value")
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertEqual(msg["uuid"], "00000000-0000-4000-8000-000000000001")
            self.assertEqual(msg["distinct_id"], "distinct_id")

    def test_basic_group_identify(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.group_identify("organization", "id:5")

            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "$groupidentify")
            self.assertEqual(
                msg["properties"],
                {
                    "$group_type": "organization",
                    "$group_key": "id:5",
                    "$group_set": {},
                    "$lib": "posthog-python",
                    "$lib_version": VERSION,
                    "$geoip_disable": True,
                    "$is_server": True,
                },
            )
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))

    def test_basic_group_identify_with_distinct_id(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.group_identify(
                "organization", "id:5", distinct_id="distinct_id"
            )
            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "$groupidentify")
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(
                msg["properties"],
                {
                    "$group_type": "organization",
                    "$group_key": "id:5",
                    "$group_set": {},
                    "$lib": "posthog-python",
                    "$lib_version": VERSION,
                    "$geoip_disable": True,
                    "$is_server": True,
                },
            )
            self.assertTrue(isinstance(msg["timestamp"], str))
            self.assertIsNotNone(msg.get("uuid"))

    def test_advanced_group_identify(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.group_identify(
                "organization",
                "id:5",
                {"trait": "value"},
                timestamp=datetime(2014, 9, 3),
                uuid="00000000-0000-4000-8000-000000000001",
            )

            self.assertEqual(msg_uuid, "00000000-0000-4000-8000-000000000001")

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "$groupidentify")
            self.assertEqual(
                msg["properties"],
                {
                    "$group_type": "organization",
                    "$group_key": "id:5",
                    "$group_set": {"trait": "value"},
                    "$lib": "posthog-python",
                    "$lib_version": VERSION,
                    "$geoip_disable": True,
                    "$is_server": True,
                },
            )
            self.assertEqual(msg["timestamp"], "2014-09-03T00:00:00+00:00")

    def test_advanced_group_identify_with_distinct_id(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.group_identify(
                "organization",
                "id:5",
                {"trait": "value"},
                timestamp=datetime(2014, 9, 3),
                uuid="00000000-0000-4000-8000-000000000001",
                distinct_id="distinct_id",
            )

            self.assertEqual(msg_uuid, "00000000-0000-4000-8000-000000000001")

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "$groupidentify")
            self.assertEqual(msg["distinct_id"], "distinct_id")

            self.assertEqual(
                msg["properties"],
                {
                    "$group_type": "organization",
                    "$group_key": "id:5",
                    "$group_set": {"trait": "value"},
                    "$lib": "posthog-python",
                    "$lib_version": VERSION,
                    "$geoip_disable": True,
                    "$is_server": True,
                },
            )
            self.assertEqual(msg["timestamp"], "2014-09-03T00:00:00+00:00")

    def test_basic_alias(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            msg_uuid = client.alias("previousId", "distinct_id")
            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]
            self.assertEqual(msg["properties"]["distinct_id"], "previousId")
            self.assertEqual(msg["properties"]["alias"], "distinct_id")

    @parameterized.expand(
        [
            # test_name, session_id, additional_properties, expected_properties
            ("basic_session_id", "test-session-123", {}, {}),
            (
                "session_id_with_other_properties",
                "test-session-456",
                {
                    "custom_prop": "custom_value",
                    "$process_person_profile": False,
                    "$current_url": "https://example.com",
                },
                {
                    "custom_prop": "custom_value",
                    "$process_person_profile": False,
                    "$current_url": "https://example.com",
                },
            ),
            ("session_id_uuid_format", str(uuid4()), {}, {}),
            ("session_id_numeric_string", "1234567890", {}, {}),
            ("session_id_empty_string", "", {}, {}),
            ("session_id_with_special_chars", "session-123_test.id", {}, {}),
        ]
    )
    def test_capture_with_session_id_variations(
        self, test_name, session_id, additional_properties, expected_properties
    ):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)

            properties = {"$session_id": session_id, **additional_properties}
            msg_uuid = client.capture(
                "python test event", distinct_id="distinct_id", properties=properties
            )

            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], "python test event")
            self.assertEqual(msg["distinct_id"], "distinct_id")
            self.assertEqual(msg["properties"]["$session_id"], session_id)
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)

            # Check additional expected properties
            for key, value in expected_properties.items():
                self.assertEqual(msg["properties"][key], value)

    def test_session_id_preserved_with_groups(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            session_id = "group-session-101"

            msg_uuid = client.capture(
                "test_event",
                distinct_id="distinct_id",
                properties={"$session_id": session_id},
                groups={"company": "id:5", "instance": "app.posthog.com"},
            )

            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["properties"]["$session_id"], session_id)
            self.assertEqual(
                msg["properties"]["$groups"],
                {"company": "id:5", "instance": "app.posthog.com"},
            )

    def test_session_id_with_anonymous_event(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            session_id = "anonymous-session-202"

            msg_uuid = client.capture(
                "anonymous_event",
                distinct_id="distinct_id",
                properties={
                    "$session_id": session_id,
                    "$process_person_profile": False,
                },
            )

            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["properties"]["$session_id"], session_id)
            self.assertEqual(msg["properties"]["$process_person_profile"], False)

    @parameterized.expand(
        [
            # test_name, event_name, session_id, additional_properties, expected_additional_properties
            (
                "screen_event",
                "$screen",
                "special-session-505",
                {"$screen_name": "HomeScreen"},
                {"$screen_name": "HomeScreen"},
            ),
            (
                "survey_event",
                "survey sent",
                "survey-session-606",
                {
                    "$survey_id": "survey_123",
                    "$survey_questions": [
                        {"id": "q1", "question": "How likely are you to recommend us?"}
                    ],
                },
                {"$survey_id": "survey_123"},
            ),
            (
                "complex_properties_event",
                "complex_event",
                "mixed-session-707",
                {
                    "$current_url": "https://example.com/page",
                    "$process_person_profile": True,
                    "custom_property": "custom_value",
                    "numeric_property": 42,
                    "boolean_property": True,
                },
                {
                    "$current_url": "https://example.com/page",
                    "$process_person_profile": True,
                    "custom_property": "custom_value",
                    "numeric_property": 42,
                    "boolean_property": True,
                },
            ),
            (
                "csp_violation",
                "$csp_violation",
                "csp-session-789",
                {
                    "$csp_version": "1.0",
                    "$current_url": "https://example.com/page",
                    "$process_person_profile": False,
                    "$raw_user_agent": "Mozilla/5.0 Test Agent",
                    "$csp_document_url": "https://example.com/page",
                    "$csp_blocked_url": "https://malicious.com/script.js",
                    "$csp_violated_directive": "script-src",
                },
                {
                    "$csp_version": "1.0",
                    "$current_url": "https://example.com/page",
                    "$process_person_profile": False,
                    "$raw_user_agent": "Mozilla/5.0 Test Agent",
                    "$csp_document_url": "https://example.com/page",
                    "$csp_blocked_url": "https://malicious.com/script.js",
                    "$csp_violated_directive": "script-src",
                },
            ),
        ]
    )
    def test_session_id_with_different_event_types(
        self,
        test_name,
        event_name,
        session_id,
        additional_properties,
        expected_additional_properties,
    ):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)

            properties = {"$session_id": session_id, **additional_properties}
            msg_uuid = client.capture(
                event_name, distinct_id="distinct_id", properties=properties
            )

            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["event"], event_name)
            self.assertEqual(msg["properties"]["$session_id"], session_id)

            # Check additional expected properties
            for key, value in expected_additional_properties.items():
                self.assertEqual(msg["properties"][key], value)

            # Verify system properties are still added
            self.assertEqual(msg["properties"]["$lib"], "posthog-python")
            self.assertEqual(msg["properties"]["$lib_version"], VERSION)

    @parameterized.expand(
        [
            # test_name, super_properties, event_session_id, expected_session_id, expected_super_props
            (
                "super_properties_override_session_id",
                {"$session_id": "super-session", "source": "test"},
                "event-session-808",
                "super-session",
                {"source": "test"},
            ),
            (
                "no_super_properties_conflict",
                {"source": "test", "version": "1.0"},
                "event-session-909",
                "event-session-909",
                {"source": "test", "version": "1.0"},
            ),
            (
                "empty_super_properties",
                {},
                "event-session-111",
                "event-session-111",
                {},
            ),
            (
                "super_properties_with_other_dollar_props",
                {"$current_url": "https://super.com", "source": "test"},
                "event-session-222",
                "event-session-222",
                {"$current_url": "https://super.com", "source": "test"},
            ),
        ]
    )
    def test_session_id_with_super_properties_variations(
        self,
        test_name,
        super_properties,
        event_session_id,
        expected_session_id,
        expected_super_props,
    ):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY, super_properties=super_properties, sync_mode=True
            )

            msg_uuid = client.capture(
                "test_event",
                distinct_id="distinct_id",
                properties={"$session_id": event_session_id},
            )

            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["properties"]["$session_id"], expected_session_id)

            # Check expected super properties are present
            for key, value in expected_super_props.items():
                self.assertEqual(msg["properties"][key], value)

    def test_flush(self):
        client = self.client
        # set up the consumer with more requests than a single batch will allow
        for i in range(1000):
            client.capture(
                "event", distinct_id="distinct_id", properties={"trait": "value"}
            )
        # We can't reliably assert that the queue is non-empty here; that's
        # a race condition. We do our best to load it up though.
        client.flush()
        # Make sure that the client queue is empty after flushing
        self.assertTrue(client.queue.empty())

    def test_shutdown(self):
        client = self.client
        # set up the consumer with more requests than a single batch will allow
        for i in range(1000):
            client.capture(
                "test event", distinct_id="distinct_id", properties={"trait": "value"}
            )
        client.shutdown()
        # we expect two things after shutdown:
        # 1. client queue is empty
        # 2. consumer thread has stopped
        self.assertTrue(client.queue.empty())
        for consumer in client.consumers:
            self.assertFalse(consumer.is_alive())

    def test_shutdown_clears_feature_flag_called_dedupe_cache(self):
        client = Client(FAKE_TEST_API_KEY, send=False, thread=0)
        client.distinct_ids_feature_flags_reported["user"] = {("flag", True, ())}

        client.shutdown()

        self.assertEqual(len(client.distinct_ids_feature_flags_reported), 0)

    def test_shutdown_flushes_without_timeout(self):
        client = Client(FAKE_TEST_API_KEY, send=False, thread=0)

        with mock.patch.object(client, "flush") as mock_flush:
            client.shutdown()

        mock_flush.assert_called_once_with(timeout_seconds=None)

    def test_synchronous(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, sync_mode=True)

            msg_uuid = client.capture("test event", distinct_id="distinct_id")
            self.assertFalse(client.consumers)
            self.assertTrue(client.queue.empty())
            self.assertIsNotNone(msg_uuid)

            # Verify the message was sent immediately
            mock_post.assert_called_once()

    def test_overflow(self):
        client = Client(FAKE_TEST_API_KEY, max_queue_size=1)
        # Ensure consumer thread is no longer uploading
        client.join()

        for i in range(10):
            client.capture("test event", distinct_id="distinct_id")

        msg_uuid = client.capture("test event", distinct_id="distinct_id")
        # Make sure we are informed that the queue is at capacity
        self.assertIsNone(msg_uuid)

    def test_unicode(self):
        Client("unicode_key")

    def test_numeric_distinct_id(self):
        self.client.capture("python event", distinct_id=1234)
        self.client.flush()
        self.assertFalse(self.failed)

    def test_debug(self):
        Client("bad_key", debug=True)

    def test_gzip(self):
        client = Client(FAKE_TEST_API_KEY, on_error=self.fail, gzip=True)
        for _ in range(10):
            client.capture(
                "event", distinct_id="distinct_id", properties={"trait": "value"}
            )
        client.flush()
        self.assertFalse(self.failed)

    def test_user_defined_flush_at(self):
        client = Client(
            FAKE_TEST_API_KEY, on_error=self.fail, flush_at=10, flush_interval=3
        )

        def mock_post_fn(*args, **kwargs):
            self.assertEqual(len(kwargs["batch"]), 10)

        # the post function should be called 2 times, with a batch size of 10
        # each time.
        with mock.patch(
            "posthog.consumer.batch_post", side_effect=mock_post_fn
        ) as mock_post:
            for _ in range(20):
                client.capture(
                    "event", distinct_id="distinct_id", properties={"trait": "value"}
                )
            time.sleep(1)
            self.assertEqual(mock_post.call_count, 2)

    def test_user_defined_timeout(self):
        client = Client(FAKE_TEST_API_KEY, timeout=10)
        for consumer in client.consumers:
            self.assertEqual(consumer.timeout, 10)

    def test_default_timeout_15(self):
        client = Client(FAKE_TEST_API_KEY)
        for consumer in client.consumers:
            self.assertEqual(consumer.timeout, 15)

    def test_disabled(self):
        client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, disabled=True)
        msg_uuid = client.capture("python test event", distinct_id="distinct_id")
        client.flush()
        self.assertIsNone(msg_uuid)
        self.assertFalse(self.failed)

    @mock.patch("posthog.client.flags")
    def test_disabled_with_feature_flags(self, patch_flags):
        client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, disabled=True)

        response = client.get_feature_flag("beta-feature", "12345")
        self.assertIsNone(response)
        patch_flags.assert_not_called()

        response = client.feature_enabled("beta-feature", "12345")
        self.assertIsNone(response)
        patch_flags.assert_not_called()

        response = client.get_all_flags("12345")
        self.assertIsNone(response)
        patch_flags.assert_not_called()

        response = client.get_feature_flag_payload("key", "12345")
        self.assertIsNone(response)
        patch_flags.assert_not_called()

        response = client.get_all_flags_and_payloads("12345")
        self.assertEqual(response, {"featureFlags": None, "featureFlagPayloads": None})
        patch_flags.assert_not_called()

        # no capture calls
        self.assertTrue(client.queue.empty())

    def test_enabled_to_disabled(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                disabled=False,
                sync_mode=True,
            )
            msg_uuid = client.capture("python test event", distinct_id="distinct_id")

            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]
            self.assertEqual(msg["event"], "python test event")

            client.disabled = True
            msg_uuid = client.capture("python test event", distinct_id="distinct_id")
            self.assertIsNone(msg_uuid)
            self.assertFalse(self.failed)

    def test_disable_geoip_default_on_events(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                disable_geoip=True,
                sync_mode=True,
            )
            msg_uuid = client.capture("python test event", distinct_id="distinct_id")
            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            capture_msg = batch_data[0]
            self.assertEqual(capture_msg["properties"]["$geoip_disable"], True)

    def test_disable_geoip_override_on_events(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                disable_geoip=False,
                sync_mode=True,
            )
            msg_uuid = client.set(
                distinct_id="distinct_id",
                properties={"a": "b", "c": "d"},
                disable_geoip=True,
            )
            self.assertIsNotNone(msg_uuid)

            msg_uuid = client.capture(
                "event",
                distinct_id="distinct_id",
                properties={"trait": "value"},
                disable_geoip=False,
            )
            self.assertIsNotNone(msg_uuid)

            # Check both calls were made
            self.assertEqual(mock_post.call_count, 2)

            # Check set event
            set_batch = mock_post.call_args_list[0][1]["batch"]
            capture_msg = set_batch[0]
            self.assertEqual(capture_msg["properties"]["$geoip_disable"], True)

            # Check page event
            page_batch = mock_post.call_args_list[1][1]["batch"]
            identify_msg = page_batch[0]
            self.assertEqual("$geoip_disable" not in identify_msg["properties"], True)

    def test_disable_geoip_method_overrides_init_on_events(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                disable_geoip=True,
                sync_mode=True,
            )
            msg_uuid = client.capture(
                "python test event", distinct_id="distinct_id", disable_geoip=False
            )
            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]
            self.assertTrue("$geoip_disable" not in msg["properties"])

    @mock.patch("posthog.client.flags")
    def test_disable_geoip_default_on_decide(self, patch_flags):
        patch_flags.return_value = {
            "featureFlags": {
                "beta-feature": "random-variant",
                "alpha-feature": True,
                "off-feature": False,
            }
        }
        client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, disable_geoip=False)
        client.get_feature_flag("random_key", "some_id", disable_geoip=True)
        patch_flags.assert_called_with(
            "random_key",
            "https://us.i.posthog.com",
            timeout=3,
            max_retries=1,
            distinct_id="some_id",
            groups={},
            person_properties={},
            group_properties={},
            geoip_disable=True,
            device_id=None,
            flag_keys_to_evaluate=["random_key"],
        )
        patch_flags.reset_mock()
        client.feature_enabled(
            "random_key", "feature_enabled_distinct_id", disable_geoip=True
        )
        patch_flags.assert_called_with(
            "random_key",
            "https://us.i.posthog.com",
            timeout=3,
            max_retries=1,
            distinct_id="feature_enabled_distinct_id",
            groups={},
            person_properties={},
            group_properties={},
            geoip_disable=True,
            device_id=None,
            flag_keys_to_evaluate=["random_key"],
        )
        patch_flags.reset_mock()
        client.get_all_flags_and_payloads("all_flags_payloads_id")
        patch_flags.assert_called_with(
            "random_key",
            "https://us.i.posthog.com",
            timeout=3,
            max_retries=1,
            distinct_id="all_flags_payloads_id",
            groups={},
            person_properties={},
            group_properties={},
            geoip_disable=False,
            device_id=None,
        )

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_call_identify_fails(self, patch_get, patch_poller):
        def raise_effect():
            raise Exception("http exception")

        patch_get.return_value.raiseError.side_effect = raise_effect
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [{"key": "example"}]

        self.assertFalse(client.feature_enabled("example", "distinct_id"))

    @mock.patch("posthog.client.flags")
    def test_default_properties_get_added_properly(self, patch_flags):
        patch_flags.return_value = {
            "featureFlags": {
                "beta-feature": "random-variant",
                "alpha-feature": True,
                "off-feature": False,
            }
        }
        client = Client(
            FAKE_TEST_API_KEY,
            host="http://app2.posthog.com",
            on_error=self.set_fail,
            disable_geoip=False,
        )
        client.get_feature_flag(
            "random_key",
            "some_id",
            groups={"company": "id:5", "instance": "app.posthog.com"},
            person_properties={"x1": "y1"},
            group_properties={"company": {"x": "y"}},
        )
        patch_flags.assert_called_with(
            "random_key",
            "http://app2.posthog.com",
            timeout=3,
            max_retries=1,
            distinct_id="some_id",
            groups={"company": "id:5", "instance": "app.posthog.com"},
            person_properties={"x1": "y1"},
            group_properties={
                "company": {"$group_key": "id:5", "x": "y"},
                "instance": {"$group_key": "app.posthog.com"},
            },
            geoip_disable=False,
            device_id=None,
            flag_keys_to_evaluate=["random_key"],
        )

        patch_flags.reset_mock()
        client.get_feature_flag(
            "random_key",
            "some_id",
            groups={"company": "id:5", "instance": "app.posthog.com"},
            person_properties={"distinct_id": "override"},
            group_properties={
                "company": {
                    "$group_key": "group_override",
                }
            },
        )
        patch_flags.assert_called_with(
            "random_key",
            "http://app2.posthog.com",
            timeout=3,
            max_retries=1,
            distinct_id="some_id",
            groups={"company": "id:5", "instance": "app.posthog.com"},
            person_properties={"distinct_id": "override"},
            group_properties={
                "company": {"$group_key": "group_override"},
                "instance": {"$group_key": "app.posthog.com"},
            },
            geoip_disable=False,
            device_id=None,
            flag_keys_to_evaluate=["random_key"],
        )

        patch_flags.reset_mock()
        # test nones
        client.get_all_flags_and_payloads(
            "some_id", groups={}, person_properties=None, group_properties=None
        )
        patch_flags.assert_called_with(
            "random_key",
            "http://app2.posthog.com",
            timeout=3,
            max_retries=1,
            distinct_id="some_id",
            groups={},
            person_properties={},
            group_properties={},
            geoip_disable=False,
            device_id=None,
        )

    @parameterized.expand(
        [
            # method, method_args, expected_person_props, expected_flag_keys
            (
                "get_feature_flag",
                ["random_key", "some_id"],
                {},
                ["random_key"],
            ),
            (
                "feature_enabled",
                ["random_key", "some_id"],
                {},
                ["random_key"],
            ),
            (
                "get_all_flags_and_payloads",
                ["some_id"],
                {},
                None,
            ),
            ("get_all_flags", ["some_id"], {}, None),
            ("get_flags_decision", ["some_id"], {}, None),
        ]
    )
    @mock.patch("posthog.client.flags")
    def test_device_id_is_passed_to_flags_request(
        self,
        method,
        method_args,
        expected_person_props,
        expected_flag_keys,
        patch_flags,
    ):
        """Test that device_id is properly passed to the flags request when provided."""
        patch_flags.return_value = {"featureFlags": {"beta-feature": "random-variant"}}
        client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail)

        getattr(client, method)(*method_args, device_id="test-device-123")

        expected_call = {
            "distinct_id": "some_id",
            "groups": {},
            "person_properties": expected_person_props,
            "group_properties": {},
            "geoip_disable": True,
            "device_id": "test-device-123",
        }
        if expected_flag_keys:
            expected_call["flag_keys_to_evaluate"] = expected_flag_keys

        patch_flags.assert_called_with(
            "random_key",
            "https://us.i.posthog.com",
            timeout=3,
            max_retries=1,
            **expected_call,
        )

    @mock.patch("posthog.client.flags")
    def test_device_id_from_context_is_used_in_flags_request(self, patch_flags):
        """Test that device_id from context is used in flags request when not explicitly provided."""
        from posthog.contexts import new_context, set_context_device_id

        patch_flags.return_value = {
            "featureFlags": {
                "beta-feature": "random-variant",
            }
        }
        client = Client(
            FAKE_TEST_API_KEY,
            on_error=self.set_fail,
        )

        # Test that device_id from context is used
        with new_context():
            set_context_device_id("context-device-id")
            client.get_feature_flag("random_key", "some_id")
            patch_flags.assert_called_with(
                "random_key",
                "https://us.i.posthog.com",
                timeout=3,
                max_retries=1,
                distinct_id="some_id",
                groups={},
                person_properties={},
                group_properties={},
                geoip_disable=True,
                device_id="context-device-id",
                flag_keys_to_evaluate=["random_key"],
            )

        # Test that explicit device_id overrides context
        patch_flags.reset_mock()
        with new_context():
            set_context_device_id("context-device-id")
            client.get_feature_flag(
                "random_key", "some_id", device_id="explicit-device-id"
            )
            patch_flags.assert_called_with(
                "random_key",
                "https://us.i.posthog.com",
                timeout=3,
                max_retries=1,
                distinct_id="some_id",
                groups={},
                person_properties={},
                group_properties={},
                geoip_disable=True,
                device_id="explicit-device-id",
                flag_keys_to_evaluate=["random_key"],
            )

    @mock.patch("posthog.client.flags")
    def test_client_set_context_device_id_is_used_in_flags_request(self, patch_flags):
        patch_flags.return_value = {
            "featureFlags": {
                "beta-feature": "random-variant",
            }
        }
        client = Client(
            FAKE_TEST_API_KEY,
            on_error=self.set_fail,
        )

        with client.new_context():
            client.set_context_device_id("client-context-device-id")
            client.get_feature_flag("random_key", "some_id")

        patch_flags.assert_called_with(
            "random_key",
            "https://us.i.posthog.com",
            timeout=3,
            max_retries=1,
            distinct_id="some_id",
            groups={},
            person_properties={},
            group_properties={},
            geoip_disable=True,
            device_id="client-context-device-id",
            flag_keys_to_evaluate=["random_key"],
        )

    @parameterized.expand(
        [
            # name, sys_platform, version_info, expected_runtime, expected_version, expected_os, expected_os_version, expected_os_distro, platform_method, platform_return
            (
                "macOS",
                "darwin",
                (3, 8, 10),
                "MockPython",
                "3.8.10",
                "Mac OS X",
                "10.15.7",
                None,
                "mac_ver",
                ("10.15.7", "", ""),
            ),
            (
                "Windows",
                "win32",
                (3, 8, 10),
                "MockPython",
                "3.8.10",
                "Windows",
                "10",
                None,
                "win32_ver",
                ("10", "", "", ""),
            ),
            (
                "Linux",
                "linux",
                (3, 8, 10),
                "MockPython",
                "3.8.10",
                "Linux",
                "20.04",
                "Ubuntu",
                None,
                None,
            ),
        ]
    )
    def test_mock_system_context(
        self,
        _name,
        sys_platform,
        version_info,
        expected_runtime,
        expected_version,
        expected_os,
        expected_os_version,
        expected_os_distro,
        platform_method,
        platform_return,
    ):
        """Test that we can mock platform and sys for testing system_context"""
        with mock.patch("posthog.utils.platform") as mock_platform:
            with mock.patch("posthog.utils.sys") as mock_sys:
                # Set up common mocks
                mock_platform.python_implementation.return_value = expected_runtime
                mock_sys.version_info = version_info
                mock_sys.platform = sys_platform

                # Set up platform-specific mocks
                if platform_method:
                    getattr(
                        mock_platform, platform_method
                    ).return_value = platform_return

                # Special handling for Linux which uses distro module
                if sys_platform == "linux":
                    with mock.patch("posthog.utils.distro") as mock_distro:
                        mock_distro.info.return_value = {"version": expected_os_version}
                        mock_distro.name.return_value = expected_os_distro or ""

                        from posthog.utils import system_context

                        context = system_context()
                else:
                    # Get system context for non-Linux platforms
                    from posthog.utils import system_context

                    context = system_context()

                # Verify results
                expected_context = {
                    "$python_runtime": expected_runtime,
                    "$python_version": expected_version,
                    "$os": expected_os,
                    "$os_version": expected_os_version,
                }

                if sys_platform == "linux":
                    expected_context["$os_distro"] = expected_os_distro

                assert context == expected_context

    @mock.patch("posthog.client.flags")
    def test_get_decide_returns_normalized_decide_response(self, patch_flags):
        patch_flags.return_value = {
            "featureFlags": {
                "beta-feature": "random-variant",
                "alpha-feature": True,
                "off-feature": False,
            },
            "featureFlagPayloads": {"beta-feature": '{"some": "data"}'},
            "errorsWhileComputingFlags": False,
            "requestId": "test-id",
        }

        client = Client(FAKE_TEST_API_KEY)
        distinct_id = "test_distinct_id"
        groups = {"test_group_type": "test_group_id"}
        person_properties = {"test_property": "test_value"}

        response = client.get_flags_decision(distinct_id, groups, person_properties)

        assert response == {
            "flags": {
                "beta-feature": FeatureFlag(
                    key="beta-feature",
                    enabled=True,
                    variant="random-variant",
                    reason=None,
                    metadata=LegacyFlagMetadata(
                        payload='{"some": "data"}',
                    ),
                ),
                "alpha-feature": FeatureFlag(
                    key="alpha-feature",
                    enabled=True,
                    variant=None,
                    reason=None,
                    metadata=LegacyFlagMetadata(
                        payload=None,
                    ),
                ),
                "off-feature": FeatureFlag(
                    key="off-feature",
                    enabled=False,
                    variant=None,
                    reason=None,
                    metadata=LegacyFlagMetadata(
                        payload=None,
                    ),
                ),
            },
            "errorsWhileComputingFlags": False,
            "requestId": "test-id",
        }

    def test_set_context_session_with_capture(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            with new_context():
                set_context_session("context-session-123")

                msg_uuid = client.capture(
                    "test_event",
                    distinct_id="distinct_id",
                    properties={"custom_prop": "value"},
                )

                self.assertIsNotNone(msg_uuid)

                # Get the enqueued message from the mock
                mock_post.assert_called_once()
                batch_data = mock_post.call_args[1]["batch"]
                msg = batch_data[0]

                self.assertEqual(
                    msg["properties"]["$session_id"], "context-session-123"
                )

    @parameterized.expand([("new_context",), ("scoped",)])
    def test_client_context_helpers_apply_to_capture(self, context_helper):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)

            def capture_in_context():
                client.tag("client_tag", "tag-value")
                client.identify_context("context-user")
                client.set_context_session("context-session-123")

                self.assertEqual(client.get_tags(), {"client_tag": "tag-value"})

                return client.capture(
                    "test_event",
                    properties={"custom_prop": "value"},
                )

            if context_helper == "new_context":
                with client.new_context(fresh=True):
                    msg_uuid = capture_in_context()
            else:

                @client.scoped(fresh=True)
                def scoped_capture():
                    return capture_in_context()

                msg_uuid = scoped_capture()

            self.assertIsNotNone(msg_uuid)
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["distinct_id"], "context-user")
            self.assertEqual(msg["properties"]["client_tag"], "tag-value")
            self.assertEqual(msg["properties"]["custom_prop"], "value")
            self.assertEqual(msg["properties"]["$session_id"], "context-session-123")
            self.assertCountEqual(msg["properties"]["$context_tags"], ["client_tag"])
            self.assertEqual(client.get_tags(), {})

    def test_client_scoped_context_helpers_apply_to_capture_async(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)

            @client.scoped(fresh=True)
            async def scoped_capture():
                client.tag("async_scoped_tag", "async-scoped-value")
                client.identify_context("async-scoped-user")
                client.set_context_session("async-scoped-session-123")
                await asyncio.sleep(0)
                return client.capture("async_scoped_event")

            msg_uuid = asyncio.run(scoped_capture())

            self.assertIsNotNone(msg_uuid)
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["distinct_id"], "async-scoped-user")
            self.assertEqual(
                msg["properties"]["async_scoped_tag"], "async-scoped-value"
            )
            self.assertEqual(
                msg["properties"]["$session_id"], "async-scoped-session-123"
            )
            self.assertCountEqual(
                msg["properties"]["$context_tags"], ["async_scoped_tag"]
            )
            self.assertEqual(client.get_tags(), {})

    def test_set_context_session_with_page_explicit_properties(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            with new_context():
                set_context_session("page-explicit-session-789")

                properties = {
                    "$session_id": get_context_session_id(),
                    "page_type": "landing",
                }
                msg_uuid = client.capture(
                    "$page", distinct_id="distinct_id", properties=properties
                )

                self.assertIsNotNone(msg_uuid)

                # Get the enqueued message from the mock
                mock_post.assert_called_once()
                batch_data = mock_post.call_args[1]["batch"]
                msg = batch_data[0]

                self.assertEqual(
                    msg["properties"]["$session_id"], "page-explicit-session-789"
                )

    def test_set_context_session_override_in_capture(self):
        """Test that explicit session ID overrides context session ID in capture"""
        from posthog.contexts import new_context, set_context_session

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)
            with new_context():
                set_context_session("context-session-override")

                msg_uuid = client.capture(
                    "test_event",
                    distinct_id="distinct_id",
                    properties={
                        "$session_id": "explicit-session-override",
                        "custom_prop": "value",
                    },
                )

                self.assertIsNotNone(msg_uuid)

                # Get the enqueued message from the mock
                mock_post.assert_called_once()
                batch_data = mock_post.call_args[1]["batch"]
                msg = batch_data[0]

                self.assertEqual(
                    msg["properties"]["$session_id"], "explicit-session-override"
                )

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_enable_local_evaluation_false_disables_poller(
        self, patch_get, patch_poller
    ):
        """Test that when enable_local_evaluation=False, the poller is not started"""
        patch_get.return_value = GetResponse(
            data={
                "flags": [
                    {
                        "id": 1,
                        "name": "Beta Feature",
                        "key": "beta-feature",
                        "active": True,
                    }
                ],
                "group_type_mapping": {},
                "cohorts": {},
            },
            etag='"test-etag"',
        )

        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test-personal-key",
            enable_local_evaluation=False,
        )

        # Load feature flags should not start the poller
        client.load_feature_flags()

        # Assert that the poller was not created/started
        patch_poller.assert_not_called()
        # But the feature flags should still be loaded
        patch_get.assert_called_once()
        self.assertEqual(len(client.feature_flags), 1)
        self.assertEqual(client.feature_flags[0]["key"], "beta-feature")

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_enable_local_evaluation_true_starts_poller(self, patch_get, patch_poller):
        """Test that when enable_local_evaluation=True (default), the poller is started"""
        patch_get.return_value = GetResponse(
            data={
                "flags": [
                    {
                        "id": 1,
                        "name": "Beta Feature",
                        "key": "beta-feature",
                        "active": True,
                    }
                ],
                "group_type_mapping": {},
                "cohorts": {},
            },
            etag='"test-etag"',
        )

        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test-personal-key",
            enable_local_evaluation=True,
        )

        # Load feature flags should start the poller
        client.load_feature_flags()

        # Assert that the poller was created and started
        patch_poller.assert_called_once()
        patch_get.assert_called_once()
        self.assertEqual(len(client.feature_flags), 1)
        self.assertEqual(client.feature_flags[0]["key"], "beta-feature")

    @mock.patch("posthog.client.remote_config")
    def test_get_remote_config_payload_works_without_poller(self, patch_remote_config):
        """Test that get_remote_config_payload works without local evaluation enabled"""
        patch_remote_config.return_value = {"test": "payload"}

        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test-personal-key",
            enable_local_evaluation=False,
        )

        # Should work without poller
        result = client.get_remote_config_payload("test-flag")

        self.assertEqual(result, {"test": "payload"})
        patch_remote_config.assert_called_once_with(
            "test-personal-key",
            FAKE_TEST_API_KEY,
            client.host,
            "test-flag",
            timeout=client.feature_flags_request_timeout_seconds,
        )

    def test_get_remote_config_payload_requires_personal_api_key(self):
        """Test that get_remote_config_payload requires personal API key"""
        client = Client(
            FAKE_TEST_API_KEY,
            enable_local_evaluation=False,
        )

        result = client.get_remote_config_payload("test-flag")

        self.assertIsNone(result)

    def test_parse_send_feature_flags_method(self):
        """Test the _parse_send_feature_flags helper method"""
        client = Client(FAKE_TEST_API_KEY, sync_mode=True)

        # Test boolean True
        result = client._parse_send_feature_flags(True)
        expected = {
            "should_send": True,
            "only_evaluate_locally": None,
            "person_properties": None,
            "group_properties": None,
            "flag_keys_filter": None,
        }
        self.assertEqual(result, expected)

        # Test boolean False
        result = client._parse_send_feature_flags(False)
        expected = {
            "should_send": False,
            "only_evaluate_locally": None,
            "person_properties": None,
            "group_properties": None,
            "flag_keys_filter": None,
        }
        self.assertEqual(result, expected)

        # Test options dict with all fields
        options = {
            "only_evaluate_locally": True,
            "person_properties": {"plan": "premium"},
            "group_properties": {"company": {"type": "enterprise"}},
        }
        result = client._parse_send_feature_flags(options)
        expected = {
            "should_send": True,
            "only_evaluate_locally": True,
            "person_properties": {"plan": "premium"},
            "group_properties": {"company": {"type": "enterprise"}},
            "flag_keys_filter": None,
        }
        self.assertEqual(result, expected)

        # Test options dict with partial fields
        options = {"person_properties": {"user_id": "123"}}
        result = client._parse_send_feature_flags(options)
        expected = {
            "should_send": True,
            "only_evaluate_locally": None,
            "person_properties": {"user_id": "123"},
            "group_properties": None,
            "flag_keys_filter": None,
        }
        self.assertEqual(result, expected)

        # Test empty dict
        result = client._parse_send_feature_flags({})
        expected = {
            "should_send": True,
            "only_evaluate_locally": None,
            "person_properties": None,
            "group_properties": None,
            "flag_keys_filter": None,
        }
        self.assertEqual(result, expected)

        # Test invalid types
        with self.assertRaises(TypeError) as cm:
            client._parse_send_feature_flags("invalid")
        self.assertIn("Invalid type for send_feature_flags", str(cm.exception))

        with self.assertRaises(TypeError) as cm:
            client._parse_send_feature_flags(123)
        self.assertIn("Invalid type for send_feature_flags", str(cm.exception))

        with self.assertRaises(TypeError) as cm:
            client._parse_send_feature_flags(None)
        self.assertIn("Invalid type for send_feature_flags", str(cm.exception))

    @mock.patch("posthog.client.flags")
    def test_capture_with_send_feature_flags_flag_keys_filter(self, patch_flags):
        """Test that SendFeatureFlagsOptions with flag_keys_filter only evaluates specified flags"""
        # When flag_keys_to_evaluate is provided, the API should only return the requested flags
        patch_flags.return_value = {
            "featureFlags": {
                "flag1": "value1",
                "flag3": "value3",
            }
        }

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                personal_api_key=FAKE_TEST_API_KEY,
                sync_mode=True,
            )

            send_options = {
                "flag_keys_filter": ["flag1", "flag3"],
                "person_properties": {"subscription": "pro"},
            }

            msg_uuid = client.capture(
                "test event", distinct_id="distinct_id", send_feature_flags=send_options
            )

            self.assertIsNotNone(msg_uuid)
            self.assertFalse(self.failed)

            # Verify flags() was called with flag_keys_to_evaluate
            patch_flags.assert_called_once()
            call_args = patch_flags.call_args[1]
            self.assertEqual(call_args["flag_keys_to_evaluate"], ["flag1", "flag3"])
            self.assertEqual(call_args["person_properties"], {"subscription": "pro"})

            # Check the message includes only the filtered flags
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]

            self.assertEqual(msg["properties"]["$feature/flag1"], "value1")
            self.assertEqual(msg["properties"]["$feature/flag3"], "value3")
            # flag2 should not be included since it wasn't requested
            self.assertNotIn("$feature/flag2", msg["properties"])

    @mock.patch("posthog.client.batch_post")
    def test_get_feature_flag_result_with_empty_string_payload(self, patch_batch_post):
        """Test that get_feature_flag_result returns a FeatureFlagResult when payload is empty string"""
        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_api_key",
            sync_mode=True,
        )

        # Set up local evaluation with a flag that has empty string payload
        client.feature_flags = [
            {
                "id": 1,
                "name": "Test flag",
                "key": "test-flag",
                "is_simple_flag": False,
                "active": True,
                "rollout_percentage": None,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": None,
                            "variant": "empty-variant",
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "empty-variant",
                                "name": "Empty Variant",
                                "rollout_percentage": 100,
                            }
                        ]
                    },
                    "payloads": {"empty-variant": ""},  # Empty string payload
                },
            }
        ]

        # Test get_feature_flag_result
        result = client.get_feature_flag_result(
            "test-flag", "test-user", only_evaluate_locally=True
        )

        # Should return a FeatureFlagResult, not None
        self.assertIsNotNone(result)
        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.get_value(), "empty-variant")
        self.assertEqual(result.payload, "")  # Should be empty string, not None

    @mock.patch("posthog.client.batch_post")
    def test_get_all_flags_and_payloads_with_empty_string(self, patch_batch_post):
        """Test that get_all_flags_and_payloads includes flags with empty string payloads"""
        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_api_key",
            sync_mode=True,
        )

        # Set up multiple flags with different payload types
        client.feature_flags = [
            {
                "id": 1,
                "name": "Flag with empty payload",
                "key": "empty-payload-flag",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "variant": "variant1"}],
                    "multivariate": {
                        "variants": [{"key": "variant1", "rollout_percentage": 100}]
                    },
                    "payloads": {"variant1": ""},  # Empty string
                },
            },
            {
                "id": 2,
                "name": "Flag with normal payload",
                "key": "normal-payload-flag",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "variant": "variant2"}],
                    "multivariate": {
                        "variants": [{"key": "variant2", "rollout_percentage": 100}]
                    },
                    "payloads": {"variant2": "normal payload"},
                },
            },
        ]

        result = client.get_all_flags_and_payloads(
            "test-user", only_evaluate_locally=True
        )

        # Check that both flags are included
        self.assertEqual(result["featureFlags"]["empty-payload-flag"], "variant1")
        self.assertEqual(result["featureFlags"]["normal-payload-flag"], "variant2")

        # Check that empty string payload is included (not filtered out)
        self.assertIn("empty-payload-flag", result["featureFlagPayloads"])
        self.assertEqual(result["featureFlagPayloads"]["empty-payload-flag"], "")
        self.assertEqual(
            result["featureFlagPayloads"]["normal-payload-flag"], "normal payload"
        )

    def test_context_tags_added(self):
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail, sync_mode=True)

            with new_context():
                tag("random_tag", 12345)
                client.capture("python test event", distinct_id="distinct_id")

            batch_data = mock_post.call_args[1]["batch"]
            msg = batch_data[0]
            self.assertEqual(msg["properties"]["$context_tags"], ["random_tag"])

    @mock.patch(
        "posthog.client.Client._enqueue", side_effect=Exception("Unexpected error")
    )
    def test_methods_handle_exceptions(self, mock_enqueue):
        """Test that all decorated methods handle exceptions gracefully."""
        client = Client("test-key")

        test_cases = [
            ("capture", ["test_event"], {}),
            ("set", [], {"distinct_id": "some-id", "properties": {"a": "b"}}),
            ("set_once", [], {"distinct_id": "some-id", "properties": {"a": "b"}}),
            ("group_identify", ["group-type", "group-key"], {}),
            ("alias", ["some-id", "new-id"], {}),
        ]

        for method_name, args, kwargs in test_cases:
            with self.subTest(method=method_name):
                method = getattr(client, method_name)
                result = method(*args, **kwargs)
                self.assertEqual(result, None)

    @mock.patch(
        "posthog.client.Client._enqueue", side_effect=Exception("Expected error")
    )
    def test_debug_flag_re_raises_exceptions(self, mock_enqueue):
        """Test that methods re-raise exceptions when debug=True."""
        client = Client("test-key", debug=True)

        test_cases = [
            ("capture", ["test_event"], {}),
            ("set", [], {"distinct_id": "some-id", "properties": {"a": "b"}}),
            ("set_once", [], {"distinct_id": "some-id", "properties": {"a": "b"}}),
            ("group_identify", ["group-type", "group-key"], {}),
            ("alias", ["some-id", "new-id"], {}),
        ]

        for method_name, args, kwargs in test_cases:
            with self.subTest(method=method_name):
                method = getattr(client, method_name)
                with self.assertRaises(Exception) as cm:
                    method(*args, **kwargs)
                self.assertEqual(str(cm.exception), "Expected error")


class TestClientSyncCaptureMode(unittest.TestCase):
    """Sync-mode `_enqueue` selects the analytics submitter by `capture_mode`;
    the dedicated AI endpoint always uses the legacy submitter."""

    def _client(self, **kwargs):
        return Client(FAKE_TEST_API_KEY, sync_mode=True, **kwargs)

    @parameterized.expand(
        [
            ("v0", None, False),
            ("v1", "v1", True),
        ]
    )
    def test_capture_mode_selects_sync_submitter(self, _name, capture_mode, expects_v1):
        kwargs = {"capture_mode": capture_mode} if capture_mode else {}
        with (
            mock.patch("posthog.client.batch_post") as mock_post,
            mock.patch("posthog.client._send_v1_batch") as mock_v1,
        ):
            self._client(**kwargs).capture("evt", distinct_id="d")
        if expects_v1:
            mock_post.assert_not_called()
            mock_v1.assert_called_once()
            sent_batch = mock_v1.call_args.args[2]
            self.assertEqual(len(sent_batch), 1)
            self.assertEqual(sent_batch[0]["event"], "evt")
        else:
            mock_v1.assert_not_called()
            mock_post.assert_called_once()

    def test_v1_sync_forwards_config_to_submitter(self):
        with (
            mock.patch("posthog.client.batch_post"),
            mock.patch("posthog.client._send_v1_batch") as mock_v1,
        ):
            self._client(
                capture_mode="v1",
                capture_compression=CaptureCompression.GZIP,
                max_retries=4,
                historical_migration=True,
            ).capture("evt", distinct_id="d")
            kwargs = mock_v1.call_args.kwargs
            self.assertEqual(kwargs["compression"], CaptureCompression.GZIP)
            self.assertEqual(kwargs["max_retries"], 4)
            self.assertEqual(kwargs["historical_migration"], True)

    def test_v1_sync_gzip_flag_falls_back_to_gzip_compression(self):
        # Legacy `gzip=True` with no explicit capture_compression -> GZIP on v1.
        with (
            mock.patch("posthog.client.batch_post"),
            mock.patch("posthog.client._send_v1_batch") as mock_v1,
        ):
            self._client(capture_mode="v1", gzip=True).capture("evt", distinct_id="d")
            self.assertEqual(
                mock_v1.call_args.kwargs["compression"], CaptureCompression.GZIP
            )

    def test_v1_sync_dedicated_ai_event_stays_legacy(self):
        # $ai_* on the dedicated AI endpoint has no v1 form.
        with (
            mock.patch("posthog.client.batch_post") as mock_post,
            mock.patch("posthog.client._send_v1_batch") as mock_v1,
        ):
            client = self._client(capture_mode="v1", _dedicated_ai_endpoint=True)
            client.capture("$ai_generation", distinct_id="d")
            mock_v1.assert_not_called()
            mock_post.assert_called_once()
            self.assertEqual(mock_post.call_args.kwargs["path"], "/i/v0/ai/batch/")

    def test_v1_sync_dedicated_ai_analytics_event_uses_v1(self):
        with (
            mock.patch("posthog.client.batch_post") as mock_post,
            mock.patch("posthog.client._send_v1_batch") as mock_v1,
        ):
            client = self._client(capture_mode="v1", _dedicated_ai_endpoint=True)
            client.capture("regular_event", distinct_id="d")
            mock_post.assert_not_called()
            mock_v1.assert_called_once()
