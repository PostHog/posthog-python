import unittest

import mock

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
        """Test that exceptions in before_send don't crash the client."""

        def buggy_before_send(event):
            raise ValueError("Oops!")

        with mock.patch("posthog.client.batch_post") as mock_post:
            client = Client(
                FAKE_TEST_API_KEY,
                on_error=self.set_fail,
                before_send=buggy_before_send,
                sync_mode=True,
            )
            msg_uuid = client.capture("robust_event", distinct_id="user1")

            # Event should still be sent despite the exception
            self.assertIsNotNone(msg_uuid)

            # Check the enqueued message
            mock_post.assert_called_once()
            batch_data = mock_post.call_args[1]["batch"]
            enqueued_msg = batch_data[0]
            self.assertEqual(enqueued_msg["event"], "robust_event")

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
