import json
import time
import unittest
from threading import Thread
from unittest import mock

from posthog.client import Client
from posthog.request import GetResponse
from posthog.test.test_utils import FAKE_TEST_API_KEY


class TestRealtimeFeatureFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This ensures no real HTTP POST requests are made
        cls.capture_patch = mock.patch.object(Client, "capture")
        cls.capture_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.capture_patch.stop()

    def setUp(self):
        self.failed = False

    def set_fail(self, e, batch):
        """Mark the failure handler"""
        print("FAIL", e, batch)
        self.failed = True

    @mock.patch("posthog.client.get")
    @mock.patch("requests.get")
    def test_sse_connection_setup(self, mock_requests_get, mock_get):
        """Test that SSE connection is established when realtime_flags is enabled"""
        # Setup mock for initial flag loading
        mock_get.return_value = GetResponse(
            data={
                "flags": [
                    {
                        "id": 1,
                        "name": "Test Flag",
                        "key": "test-flag",
                        "active": True,
                        "filters": {"groups": [{"rollout_percentage": 100}]},
                    }
                ],
                "group_type_mapping": {},
                "cohorts": {},
            }
        )

        # Setup mock for SSE connection
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.iter_lines = mock.Mock(return_value=iter([]))
        mock_response.__enter__ = mock.Mock(return_value=mock_response)
        mock_response.__exit__ = mock.Mock(return_value=False)
        mock_requests_get.return_value = mock_response

        # Create client with realtime_flags enabled
        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_key",
            on_error=self.set_fail,
            realtime_flags=True,
        )

        # Load feature flags (which should trigger SSE connection)
        client.load_feature_flags()

        # Give the SSE thread a moment to start
        time.sleep(0.1)

        # Verify SSE connection was attempted
        mock_requests_get.assert_called_once()
        call_args = mock_requests_get.call_args

        # Check URL contains the stream endpoint
        self.assertIn("stream", call_args[0][0])

        # Check headers include authorization
        headers = call_args[1]["headers"]
        self.assertIn("Authorization", headers)
        self.assertEqual(headers["Accept"], "text/event-stream")

        # Cleanup
        client.shutdown()

    @mock.patch("posthog.client.get")
    def test_process_flag_update(self, mock_get):
        """Test that flag updates are processed correctly"""
        # Setup mock for initial flag loading
        mock_get.return_value = GetResponse(
            data={
                "flags": [
                    {
                        "id": 1,
                        "name": "Test Flag",
                        "key": "test-flag",
                        "active": True,
                        "filters": {"groups": [{"rollout_percentage": 100}]},
                    }
                ],
                "group_type_mapping": {},
                "cohorts": {},
            }
        )

        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_key",
            on_error=self.set_fail,
            realtime_flags=True,
        )

        # Load initial flags
        client.load_feature_flags()

        # Verify initial flag exists
        self.assertIn("test-flag", client.feature_flags_by_key)
        self.assertEqual(len(client.feature_flags), 1)

        # Simulate a flag update
        updated_flag = {
            "id": 1,
            "name": "Updated Test Flag",
            "key": "test-flag",
            "active": False,
            "filters": {"groups": [{"rollout_percentage": 50}]},
        }
        client._process_flag_update(updated_flag)

        # Verify flag was updated
        self.assertIn("test-flag", client.feature_flags_by_key)
        self.assertEqual(
            client.feature_flags_by_key["test-flag"]["name"], "Updated Test Flag"
        )
        self.assertFalse(client.feature_flags_by_key["test-flag"]["active"])

        # Cleanup
        client.shutdown()

    @mock.patch("posthog.client.get")
    def test_process_flag_deletion(self, mock_get):
        """Test that flag deletions are processed correctly"""
        # Setup mock for initial flag loading
        mock_get.return_value = GetResponse(
            data={
                "flags": [
                    {
                        "id": 1,
                        "name": "Test Flag",
                        "key": "test-flag",
                        "active": True,
                        "filters": {"groups": [{"rollout_percentage": 100}]},
                    }
                ],
                "group_type_mapping": {},
                "cohorts": {},
            }
        )

        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_key",
            on_error=self.set_fail,
            realtime_flags=True,
        )

        # Load initial flags
        client.load_feature_flags()

        # Verify initial flag exists
        self.assertIn("test-flag", client.feature_flags_by_key)
        self.assertEqual(len(client.feature_flags), 1)

        # Simulate a flag deletion
        deleted_flag = {
            "key": "test-flag",
            "deleted": True,
        }
        client._process_flag_update(deleted_flag)

        # Verify flag was deleted
        self.assertNotIn("test-flag", client.feature_flags_by_key)
        self.assertEqual(len(client.feature_flags), 0)

        # Cleanup
        client.shutdown()

    @mock.patch("posthog.client.get")
    def test_process_new_flag_addition(self, mock_get):
        """Test that new flags are added correctly"""
        # Setup mock for initial flag loading
        mock_get.return_value = GetResponse(
            data={
                "flags": [],
                "group_type_mapping": {},
                "cohorts": {},
            }
        )

        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_key",
            on_error=self.set_fail,
            realtime_flags=True,
        )

        # Load initial flags (empty)
        client.load_feature_flags()

        # Verify no flags initially
        self.assertEqual(len(client.feature_flags), 0)

        # Simulate a new flag addition
        new_flag = {
            "id": 1,
            "name": "New Test Flag",
            "key": "new-flag",
            "active": True,
            "filters": {"groups": [{"rollout_percentage": 100}]},
        }
        client._process_flag_update(new_flag)

        # Verify flag was added
        self.assertIn("new-flag", client.feature_flags_by_key)
        self.assertEqual(len(client.feature_flags), 1)
        self.assertEqual(client.feature_flags[0]["name"], "New Test Flag")

        # Cleanup
        client.shutdown()

    @mock.patch("posthog.client.get")
    def test_sse_disabled_by_default(self, mock_get):
        """Test that SSE connection is NOT established when realtime_flags is False"""
        # Setup mock for initial flag loading
        mock_get.return_value = GetResponse(
            data={
                "flags": [],
                "group_type_mapping": {},
                "cohorts": {},
            }
        )

        # Create client with realtime_flags disabled (default)
        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_key",
            on_error=self.set_fail,
            realtime_flags=False,
        )

        # Load feature flags
        client.load_feature_flags()

        # Verify SSE connection was NOT established
        self.assertFalse(client.sse_connected)
        self.assertIsNone(client.sse_connection)

        # Cleanup
        client.shutdown()

    @mock.patch("posthog.client.get")
    def test_sse_cleanup_on_shutdown(self, mock_get):
        """Test that SSE connection is properly cleaned up on shutdown"""
        # Setup mock for initial flag loading
        mock_get.return_value = GetResponse(
            data={
                "flags": [],
                "group_type_mapping": {},
                "cohorts": {},
            }
        )

        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_key",
            on_error=self.set_fail,
            realtime_flags=True,
        )

        # Manually set up a mock SSE connection
        client.sse_connection = mock.Mock()
        client.sse_connected = True

        # Shutdown the client
        client.shutdown()

        # Verify SSE connection was cleaned up
        self.assertFalse(client.sse_connected)
        self.assertIsNone(client.sse_connection)

    @mock.patch("posthog.client.get")
    def test_on_feature_flags_update_callback(self, mock_get):
        """Test that the callback is called when flags are updated"""
        # Setup mock for initial flag loading
        mock_get.return_value = GetResponse(
            data={
                "flags": [],
                "group_type_mapping": {},
                "cohorts": {},
            }
        )

        # Track callback invocations
        callback_calls = []

        def flag_update_callback(flag_key, flag_data):
            callback_calls.append({"flag_key": flag_key, "flag_data": flag_data})

        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_key",
            on_error=self.set_fail,
            realtime_flags=True,
            on_feature_flags_update=flag_update_callback,
        )

        # Load initial flags
        client.load_feature_flags()

        # Simulate a new flag addition
        new_flag = {
            "id": 1,
            "name": "New Test Flag",
            "key": "new-flag",
            "active": True,
            "filters": {"groups": [{"rollout_percentage": 100}]},
        }
        client._process_flag_update(new_flag)

        # Verify callback was called for addition
        self.assertEqual(len(callback_calls), 1)
        self.assertEqual(callback_calls[0]["flag_key"], "new-flag")
        self.assertEqual(callback_calls[0]["flag_data"]["name"], "New Test Flag")
        self.assertIsNotNone(callback_calls[0]["flag_data"])

        # Simulate a flag update
        updated_flag = {
            "id": 1,
            "name": "Updated Test Flag",
            "key": "new-flag",
            "active": False,
            "filters": {"groups": [{"rollout_percentage": 50}]},
        }
        client._process_flag_update(updated_flag)

        # Verify callback was called for update
        self.assertEqual(len(callback_calls), 2)
        self.assertEqual(callback_calls[1]["flag_key"], "new-flag")
        self.assertEqual(callback_calls[1]["flag_data"]["name"], "Updated Test Flag")
        self.assertIsNotNone(callback_calls[1]["flag_data"])

        # Simulate a flag deletion
        deleted_flag = {
            "key": "new-flag",
            "deleted": True,
        }
        client._process_flag_update(deleted_flag)

        # Verify callback was called for deletion (flag_data contains deleted=True)
        self.assertEqual(len(callback_calls), 3)
        self.assertEqual(callback_calls[2]["flag_key"], "new-flag")
        self.assertTrue(callback_calls[2]["flag_data"]["deleted"])

        # Cleanup
        client.shutdown()

    @mock.patch("posthog.client.get")
    def test_callback_exception_doesnt_break_flag_processing(self, mock_get):
        """Test that exceptions in the callback don't break flag processing"""
        # Setup mock for initial flag loading
        mock_get.return_value = GetResponse(
            data={
                "flags": [],
                "group_type_mapping": {},
                "cohorts": {},
            }
        )

        def bad_callback(flag_key, flag_data):
            raise Exception("Callback error!")

        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test_personal_key",
            on_error=self.set_fail,
            realtime_flags=True,
            on_feature_flags_update=bad_callback,
        )

        # Load initial flags
        client.load_feature_flags()

        # Simulate a new flag addition
        new_flag = {
            "id": 1,
            "name": "New Test Flag",
            "key": "new-flag",
            "active": True,
            "filters": {"groups": [{"rollout_percentage": 100}]},
        }

        # This should not raise an exception even though the callback does
        client._process_flag_update(new_flag)

        # Verify flag was still added despite callback error
        self.assertIn("new-flag", client.feature_flags_by_key)
        self.assertEqual(len(client.feature_flags), 1)

        # Cleanup
        client.shutdown()


if __name__ == "__main__":
    unittest.main()
