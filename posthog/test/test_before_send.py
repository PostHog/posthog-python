import importlib
import unittest
from unittest import mock

import posthog

from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY


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

    def test_before_send_callback_modifies_event(self):
        """Test that before_send callback can modify events."""
        processed_events = []

        def my_before_send(event):
            processed_events.append(event.copy())
            if "properties" not in event:
                event["properties"] = {}
            event["properties"]["processed_by_before_send"] = True
            return event

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                before_send=my_before_send,
                sync_mode=True,
            )
            msg_uuid = client.capture(
                "test_event", distinct_id="user1", properties={"original": "value"}
            )

            self.assertIsNotNone(msg_uuid)

            # Get the enqueued message from the mock
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            enqueued_msg = batch_data[0]

            self.assertEqual(
                enqueued_msg["properties"]["processed_by_before_send"], True
            )
            self.assertEqual(enqueued_msg["properties"]["original"], "value")
            self.assertEqual(len(processed_events), 1)
            self.assertEqual(processed_events[0]["event"], "test_event")

    def test_before_send_callback_replacing_uuid_changes_the_returned_uuid(self):
        """capture()'s return value must match the uuid on the wire event."""
        replacement_uuid = "12345678-1234-5678-1234-567812345678"

        def replace_uuid(event):
            event["uuid"] = replacement_uuid
            return event

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                before_send=replace_uuid,
                sync_mode=True,
            )
            msg_uuid = client.capture("test_event", distinct_id="user1")

            self.assertEqual(msg_uuid, replacement_uuid)

            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            enqueued_msg = batch_data[0]
            self.assertEqual(enqueued_msg["uuid"], replacement_uuid)

    def test_before_send_callback_removing_uuid_regenerates_it(self):
        """If before_send drops the uuid, a fresh one is generated and returned."""

        def remove_uuid(event):
            del event["uuid"]
            return event

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                before_send=remove_uuid,
                sync_mode=True,
            )
            msg_uuid = client.capture("test_event", distinct_id="user1")

            self.assertIsNotNone(msg_uuid)

            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            enqueued_msg = batch_data[0]
            self.assertEqual(enqueued_msg["uuid"], msg_uuid)

    def test_before_send_callback_drops_event(self):
        """Test that before_send callback can drop events by returning None."""

        def drop_test_events(event):
            if event.get("event") == "test_drop_me":
                return None
            return event

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                before_send=drop_test_events,
                sync_mode=True,
            )

            # Event should be dropped
            msg_uuid = client.capture("test_drop_me", distinct_id="user1")
            self.assertIsNone(msg_uuid)

            # Event should go through
            msg_uuid = client.capture("keep_me", distinct_id="user1")
            self.assertIsNotNone(msg_uuid)

            # Check the enqueued message
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            enqueued_msg = batch_data[0]
            self.assertEqual(enqueued_msg["event"], "keep_me")

    def test_before_send_callback_handles_exceptions(self):
        """Test that exceptions in before_send drop the event without crashing."""

        def buggy_before_send(event):
            event["properties"]["partially_mutated"] = True
            event["uuid"] = "invalid"
            raise ValueError("Oops!")

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                before_send=buggy_before_send,
                sync_mode=True,
            )
            with (
                mock.patch.object(
                    client,
                    "_normalize_event_uuid",
                    wraps=client._normalize_event_uuid,
                ) as normalize_uuid,
                self.assertLogs("posthog", level="ERROR") as logs,
            ):
                msg_uuid = client.capture("robust_event", distinct_id="user1")

            self.assertIsNone(msg_uuid)
            mock_post.assert_not_called()
            normalize_uuid.assert_called_once()
            self.assertIn("Error in before_send callback: Oops!", logs.output[0])

    def test_before_send_callback_exception_does_not_enqueue_mutated_event(self):
        def buggy_before_send(event):
            event["properties"]["partially_mutated"] = True
            raise ValueError("Oops!")

        client = Client(
            FAKE_TEST_API_KEY,
            on_error=self.set_fail,
            before_send=buggy_before_send,
            flush_at=1,
        )
        try:
            with (
                mock.patch("posthog.consumer.batch_post") as mock_post,
                mock.patch.object(
                    client._analytics_lane,
                    "enqueue",
                    wraps=client._analytics_lane.enqueue,
                ) as enqueue,
                self.assertLogs("posthog", level="ERROR"),
            ):
                msg_uuid = client.capture("robust_event", distinct_id="user1")

            self.assertIsNone(msg_uuid)
            enqueue.assert_not_called()
            mock_post.assert_not_called()
        finally:
            client.shutdown()

    def test_before_send_callback_output_is_recleaned(self):
        marker = object()

        def add_unsupported_value(event):
            event["properties"]["marker"] = marker
            return event

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                before_send=add_unsupported_value,
                sync_mode=True,
            )
            self.assertIsNotNone(client.capture("recleaned", distinct_id="user1"))

        sent_event = mock_post.call_args.kwargs["batch"][0]
        self.assertIsNone(sent_event["properties"]["marker"])

    def test_before_send_callback_non_dict_output_drops_event(self):
        with (
            mock.patch("posthog.client.batch_post") as mock_post,
            mock.patch("posthog.client.Client.log.exception") as mock_log,
        ):
            client = Client(
                FAKE_TEST_API_KEY,
                before_send=lambda _event: "invalid",
                sync_mode=True,
            )
            self.assertIsNone(client.capture("original", distinct_id="user1"))

        mock_post.assert_not_called()
        self.assertIn(
            "before_send must return a dict or None", mock_log.call_args.args[0]
        )

    def test_malformed_before_send_event_does_not_stop_consumer_or_shutdown(self):
        def add_invalid_mapping_key(event):
            if event["event"] == "malformed":
                event["properties"][("private-key",)] = "private-value"
            return event

        client = Client(
            FAKE_TEST_API_KEY,
            before_send=add_invalid_mapping_key,
            flush_at=1,
            flush_interval=0.01,
        )
        with (
            mock.patch("posthog.consumer.batch_post") as mock_post,
            self.assertLogs("posthog", level="ERROR") as logs,
        ):
            client.capture("malformed", distinct_id="user1")
            client.capture("valid", distinct_id="user1")
            client.shutdown()

        mock_post.assert_called_once()
        sent_batch = mock_post.call_args.kwargs["batch"]
        self.assertEqual([event["event"] for event in sent_batch], ["valid"])
        self.assertEqual(client.queue.unfinished_tasks, 0)
        self.assertTrue(all(not consumer.is_alive() for consumer in client.consumers))
        self.assertNotIn("private-key", "\n".join(logs.output))
        self.assertNotIn("private-value", "\n".join(logs.output))

    def test_before_send_callback_works_with_all_event_types(self):
        """Test that before_send works with capture, set, etc."""

        def add_marker(event):
            if "properties" not in event:
                event["properties"] = {}
            event["properties"]["marked"] = True
            return event

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                before_send=add_marker,
                sync_mode=True,
            )

            # Test capture
            msg_uuid = client.capture("event", distinct_id="user1")
            self.assertIsNotNone(msg_uuid)

            # Test set
            msg_uuid = client.set(distinct_id="user1", properties={"prop": "value"})
            self.assertIsNotNone(msg_uuid)

            # Check all events were marked
            self.assertEqual(mock_post.call_count, 2)
            for call in mock_post.call_args_list:
                batch_data = call[1]["batch"]
                enqueued_msg = batch_data[0]
                self.assertTrue(enqueued_msg["properties"]["marked"])

    def test_before_send_callback_disabled_when_none(self):
        """Test that client works normally when before_send is None."""
        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                before_send=None,
                sync_mode=True,
            )
            msg_uuid = client.capture("normal_event", distinct_id="user1")
            self.assertIsNotNone(msg_uuid)

            # Check the event was sent normally
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            enqueued_msg = batch_data[0]
            self.assertEqual(enqueued_msg["event"], "normal_event")

    def test_before_send_callback_pii_scrubbing_example(self):
        """Test a realistic PII scrubbing use case."""

        def scrub_pii(event):
            properties = event.get("properties", {})

            # Mask email but keep domain
            if "email" in properties:
                email = properties["email"]
                if "@" in email:
                    domain = email.split("@")[1]
                    properties["email"] = f"***@{domain}"
                else:
                    properties["email"] = "***"

            # Remove credit card
            properties.pop("credit_card", None)

            return event

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                before_send=scrub_pii,
                sync_mode=True,
            )
            msg_uuid = client.capture(
                "form_submit",
                distinct_id="user1",
                properties={
                    "email": "user@example.com",
                    "credit_card": "1234-5678-9012-3456",
                    "form_name": "contact",
                },
            )

            self.assertIsNotNone(msg_uuid)

            # Check the enqueued message was scrubbed
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            enqueued_msg = batch_data[0]

            self.assertEqual(enqueued_msg["properties"]["email"], "***@example.com")
            self.assertNotIn("credit_card", enqueued_msg["properties"])
            self.assertEqual(enqueued_msg["properties"]["form_name"], "contact")


class TestModuleLevelBeforeSend(unittest.TestCase):
    def setUp(self):
        importlib.reload(posthog)

    def tearDown(self):
        if posthog.default_client:
            posthog.shutdown()
        importlib.reload(posthog)

    def test_before_send_callback_used_during_module_level_setup(self):
        def my_before_send(event):
            event["properties"]["module_level_before_send"] = True
            return event

        with mock.patch("posthog.client.batch_post") as mock_post:
            posthog.api_key = FAKE_TEST_API_KEY
            posthog.before_send = my_before_send
            posthog.sync_mode = True

            msg_uuid = posthog.capture("test_event", distinct_id="user1")

            self.assertIsNotNone(msg_uuid)
            self.assertIs(posthog.default_client.before_send, my_before_send)

            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            enqueued_msg = batch_data[0]
            self.assertTrue(enqueued_msg["properties"]["module_level_before_send"])

    def test_before_send_callback_updates_after_client_initialization(self):
        def my_before_send(event):
            event["properties"]["updated_after_init"] = True
            return event

        with mock.patch("posthog.client.batch_post") as mock_post:
            posthog.api_key = FAKE_TEST_API_KEY
            posthog.sync_mode = True

            first_msg_uuid = posthog.capture("first_event", distinct_id="user1")

            posthog.before_send = my_before_send
            second_msg_uuid = posthog.capture("second_event", distinct_id="user1")

            self.assertIsNotNone(first_msg_uuid)
            self.assertIsNotNone(second_msg_uuid)
            self.assertIs(posthog.default_client.before_send, my_before_send)

            self.assertEqual(mock_post.call_count, 2)
            first_batch = mock_post.call_args_list[0][1]["batch"]
            second_batch = mock_post.call_args_list[1][1]["batch"]

            self.assertNotIn("updated_after_init", first_batch[0]["properties"])
            self.assertTrue(second_batch[0]["properties"]["updated_after_init"])
