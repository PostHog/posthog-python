import time
import unittest
from datetime import date, datetime

import mock
import six
from freezegun import freeze_time

from posthog.client import Client
from posthog.request import APIError
from posthog.test.utils import TEST_API_KEY
from posthog.version import VERSION


class TestClient(unittest.TestCase):
    def set_fail(self, e, batch):
        """Mark the failure handler"""
        print("FAIL", e, batch)
        self.failed = True

    def setUp(self):
        self.failed = False
        self.client = Client(TEST_API_KEY, on_error=self.set_fail)

    def test_requires_api_key(self):
        self.assertRaises(AssertionError, Client)

    def test_empty_flush(self):
        self.client.flush()

    def test_basic_capture(self):
        client = self.client
        success, msg = client.capture("distinct_id", "python test event")
        client.flush()
        self.assertTrue(success)
        self.assertFalse(self.failed)

        self.assertEqual(msg["event"], "python test event")
        self.assertTrue(isinstance(msg["timestamp"], str))
        self.assertTrue(isinstance(msg["messageId"], str))
        self.assertEqual(msg["distinct_id"], "distinct_id")
        self.assertEqual(msg["properties"]["$lib"], "posthog-python")
        self.assertEqual(msg["properties"]["$lib_version"], VERSION)

    def test_stringifies_distinct_id(self):
        # A large number that loses precision in node:
        # node -e "console.log(157963456373623802 + 1)" > 157963456373623800
        client = self.client
        success, msg = client.capture(distinct_id=157963456373623802, event="python test event")
        client.flush()
        self.assertTrue(success)
        self.assertFalse(self.failed)

        self.assertEqual(msg["distinct_id"], "157963456373623802")

    def test_advanced_capture(self):
        client = self.client
        success, msg = client.capture(
            "distinct_id",
            "python test event",
            {"property": "value"},
            {"ip": "192.168.0.1"},
            datetime(2014, 9, 3),
            "messageId",
        )

        self.assertTrue(success)

        self.assertEqual(msg["timestamp"], "2014-09-03T00:00:00+00:00")
        self.assertEqual(msg["properties"]["property"], "value")
        self.assertEqual(msg["context"]["ip"], "192.168.0.1")
        self.assertEqual(msg["event"], "python test event")
        self.assertEqual(msg["properties"]["$lib"], "posthog-python")
        self.assertEqual(msg["properties"]["$lib_version"], VERSION)
        self.assertEqual(msg["messageId"], "messageId")
        self.assertEqual(msg["distinct_id"], "distinct_id")

    def test_basic_identify(self):
        client = self.client
        success, msg = client.identify("distinct_id", {"trait": "value"})
        client.flush()
        self.assertTrue(success)
        self.assertFalse(self.failed)

        self.assertEqual(msg["$set"]["trait"], "value")
        self.assertTrue(isinstance(msg["timestamp"], str))
        self.assertTrue(isinstance(msg["messageId"], str))
        self.assertEqual(msg["distinct_id"], "distinct_id")

    def test_advanced_identify(self):
        client = self.client
        success, msg = client.identify(
            "distinct_id", {"trait": "value"}, {"ip": "192.168.0.1"}, datetime(2014, 9, 3), "messageId"
        )

        self.assertTrue(success)

        self.assertEqual(msg["timestamp"], "2014-09-03T00:00:00+00:00")
        self.assertEqual(msg["context"]["ip"], "192.168.0.1")
        self.assertEqual(msg["$set"]["trait"], "value")
        self.assertEqual(msg["properties"]["$lib"], "posthog-python")
        self.assertEqual(msg["properties"]["$lib_version"], VERSION)
        self.assertTrue(isinstance(msg["timestamp"], str))
        self.assertEqual(msg["messageId"], "messageId")
        self.assertEqual(msg["distinct_id"], "distinct_id")

    def test_basic_alias(self):
        client = self.client
        success, msg = client.alias("previousId", "distinct_id")
        client.flush()
        self.assertTrue(success)
        self.assertFalse(self.failed)
        self.assertEqual(msg["properties"]["distinct_id"], "previousId")
        self.assertEqual(msg["properties"]["alias"], "distinct_id")

    def test_basic_page(self):
        client = self.client
        success, msg = client.page("distinct_id", url="https://posthog.com/contact")
        self.assertFalse(self.failed)
        client.flush()
        self.assertTrue(success)
        self.assertEqual(msg["distinct_id"], "distinct_id")
        self.assertEqual(msg["properties"]["$current_url"], "https://posthog.com/contact")

    def test_advanced_page(self):
        client = self.client
        success, msg = client.page(
            "distinct_id",
            "https://posthog.com/contact",
            {"property": "value"},
            {"ip": "192.168.0.1"},
            datetime(2014, 9, 3),
            "messageId",
        )

        self.assertTrue(success)

        self.assertEqual(msg["timestamp"], "2014-09-03T00:00:00+00:00")
        self.assertEqual(msg["context"]["ip"], "192.168.0.1")
        self.assertEqual(msg["properties"]["$current_url"], "https://posthog.com/contact")
        self.assertEqual(msg["properties"]["property"], "value")
        self.assertEqual(msg["properties"]["$lib"], "posthog-python")
        self.assertEqual(msg["properties"]["$lib_version"], VERSION)
        self.assertTrue(isinstance(msg["timestamp"], str))
        self.assertEqual(msg["messageId"], "messageId")
        self.assertEqual(msg["distinct_id"], "distinct_id")

    def test_flush(self):
        client = self.client
        # set up the consumer with more requests than a single batch will allow
        for i in range(1000):
            success, msg = client.identify("distinct_id", {"trait": "value"})
        # We can't reliably assert that the queue is non-empty here; that's
        # a race condition. We do our best to load it up though.
        client.flush()
        # Make sure that the client queue is empty after flushing
        self.assertTrue(client.queue.empty())

    def test_shutdown(self):
        client = self.client
        # set up the consumer with more requests than a single batch will allow
        for i in range(1000):
            success, msg = client.identify("distinct_id", {"trait": "value"})
        client.shutdown()
        # we expect two things after shutdown:
        # 1. client queue is empty
        # 2. consumer thread has stopped
        self.assertTrue(client.queue.empty())
        for consumer in client.consumers:
            self.assertFalse(consumer.is_alive())

    def test_synchronous(self):
        client = Client(TEST_API_KEY, sync_mode=True)

        success, message = client.identify("distinct_id")
        self.assertFalse(client.consumers)
        self.assertTrue(client.queue.empty())
        self.assertTrue(success)

    def test_overflow(self):
        client = Client(TEST_API_KEY, max_queue_size=1)
        # Ensure consumer thread is no longer uploading
        client.join()

        for i in range(10):
            client.identify("distinct_id")

        success, msg = client.identify("distinct_id")
        # Make sure we are informed that the queue is at capacity
        self.assertFalse(success)

    def test_unicode(self):
        Client(six.u("unicode_key"))

    def test_numeric_distinct_id(self):
        self.client.capture(1234, "python event")
        self.client.flush()
        self.assertFalse(self.failed)

    def test_debug(self):
        Client("bad_key", debug=True)

    def test_gzip(self):
        client = Client(TEST_API_KEY, on_error=self.fail, gzip=True)
        for _ in range(10):
            client.identify("distinct_id", {"trait": "value"})
        client.flush()
        self.assertFalse(self.failed)

    def test_user_defined_flush_at(self):
        client = Client(TEST_API_KEY, on_error=self.fail, flush_at=10, flush_interval=3)

        def mock_post_fn(*args, **kwargs):
            self.assertEquals(len(kwargs["batch"]), 10)

        # the post function should be called 2 times, with a batch size of 10
        # each time.
        with mock.patch("posthog.consumer.batch_post", side_effect=mock_post_fn) as mock_post:
            for _ in range(20):
                client.identify("distinct_id", {"trait": "value"})
            time.sleep(1)
            self.assertEquals(mock_post.call_count, 2)

    def test_user_defined_timeout(self):
        client = Client(TEST_API_KEY, timeout=10)
        for consumer in client.consumers:
            self.assertEquals(consumer.timeout, 10)

    def test_default_timeout_15(self):
        client = Client(TEST_API_KEY)
        for consumer in client.consumers:
            self.assertEquals(consumer.timeout, 15)

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_load_feature_flags(self, patch_get, patch_poll):
        patch_get.return_value = {"results": [{"id": 1, "name": "Beta Feature", "key": "beta-feature"}]}
        client = Client(TEST_API_KEY, personal_api_key="test")
        with freeze_time("2020-01-01T12:01:00.0000Z"):
            client.load_feature_flags()
        self.assertEqual(client.feature_flags[0]["key"], "beta-feature")
        self.assertEqual(client._last_feature_flag_poll.isoformat(), "2020-01-01T12:01:00+00:00")
        self.assertEqual(patch_poll.call_count, 1)

    def test_load_feature_flags_wrong_key(self):
        client = Client(TEST_API_KEY, personal_api_key=TEST_API_KEY)
        with freeze_time("2020-01-01T12:01:00.0000Z"):
            self.assertRaises(APIError, client.load_feature_flags)

    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple(self, patch_get):
        client = Client(TEST_API_KEY)
        client.feature_flags = [
            {"id": 1, "name": "Beta Feature", "key": "beta-feature", "is_simple_flag": True, "rollout_percentage": 100}
        ]
        self.assertTrue(client.feature_enabled("beta-feature", "distinct_id"))

    @mock.patch("posthog.client.decide")
    def test_feature_enabled_request(self, patch_get):
        patch_get.return_value = {"featureFlags": ["beta-feature"]}
        client = Client(TEST_API_KEY)
        client.feature_flags = [
            {"id": 1, "name": "Beta Feature", "key": "beta-feature", "is_simple_flag": False, "rollout_percentage": 100}
        ]
        self.assertTrue(client.feature_enabled("beta-feature", "distinct_id"))

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_feature_enabled_doesnt_exist(self, patch_get, patch_poll):
        client = Client(TEST_API_KEY, personal_api_key="test")
        client.feature_flags = []

        self.assertFalse(client.feature_enabled("doesnt-exist", "distinct_id"))
        self.assertTrue(client.feature_enabled("doesnt-exist", "distinct_id", True))

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_personal_api_key_doesnt_exist(self, patch_get, patch_poll):
        client = Client(TEST_API_KEY)
        client.feature_flags = []

        self.assertFalse(client.feature_enabled("doesnt-exist", "distinct_id"))
        self.assertTrue(client.feature_enabled("doesnt-exist", "distinct_id", True))

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_load_feature_flags_error(self, patch_get, patch_poll):
        def raise_effect():
            raise Exception("http exception")

        patch_get.return_value.raiseError.side_effect = raise_effect
        client = Client(TEST_API_KEY, personal_api_key="test")
        client.feature_flags = []

        self.assertFalse(client.feature_enabled("doesnt-exist", "distinct_id"))

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_call_identify_fails(self, patch_get, patch_poll):
        def raise_effect():
            raise Exception("http exception")

        patch_get.return_value.raiseError.side_effect = raise_effect
        client = Client(TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [{"key": "example", "is_simple_flag": False}]

        self.assertFalse(client.feature_enabled("example", "distinct_id"))
