import datetime
import unittest
from typing import get_type_hints
from unittest import mock

from parameterized import parameterized

import posthog
from posthog import Posthog


class TestModule(unittest.TestCase):
    posthog = None

    def _assert_enqueue_result(self, result):
        self.assertEqual(type(result[0]), str)

    def failed(self):
        self.failed = True

    def setUp(self):
        self.failed = False
        self.posthog = Posthog(
            "testsecret", host="http://localhost:8000", on_error=self.failed
        )

    def test_track(self):
        res = self.posthog.capture("python module event", distinct_id="distinct_id")
        self._assert_enqueue_result(res)
        self.posthog.flush()

    def test_alias(self):
        res = self.posthog.alias("previousId", "distinct_id")
        self._assert_enqueue_result(res)
        self.posthog.flush()

    def test_flush(self):
        self.posthog.flush()

    def test_module_flush_forwards_timeout(self):
        with mock.patch.object(posthog, "_proxy") as proxy:
            posthog.flush(timeout_seconds=1.5)

        proxy.assert_called_once_with("flush", timeout_seconds=1.5)


class TestModuleLevelSetup(unittest.TestCase):
    def setUp(self):
        self._original_default_client = posthog.default_client
        self._original_api_key = posthog.api_key
        self._original_project_api_key = posthog.project_api_key
        self._original_project_root = posthog.project_root
        self._original_privacy_mode = posthog.privacy_mode
        self._original_disabled = posthog.disabled
        self._original_send = posthog.send
        posthog.default_client = None
        posthog.api_key = None
        posthog.project_api_key = None
        posthog.project_root = None
        posthog.privacy_mode = False
        posthog.disabled = False
        posthog.send = False

    def tearDown(self):
        posthog.default_client = self._original_default_client
        posthog.api_key = self._original_api_key
        posthog.project_api_key = self._original_project_api_key
        posthog.project_root = self._original_project_root
        posthog.privacy_mode = self._original_privacy_mode
        posthog.disabled = self._original_disabled
        posthog.send = self._original_send

    @parameterized.expand(
        [
            ("unset", None),
            ("empty", ""),
            ("whitespace", " \n\t "),
        ]
    )
    def test_setup_configures_disabled_client_when_api_key_is_blank(
        self, _name, api_key
    ):
        posthog.api_key = api_key

        client = posthog.setup()

        self.assertIs(client, posthog.default_client)
        self.assertEqual(client.api_key, "")
        self.assertTrue(client.disabled)
        self.assertIsNone(posthog.capture("Module Python Event", distinct_id="john"))

    def test_setup_uses_project_api_key_when_api_key_is_unset(self):
        posthog.project_api_key = " phc_project_key "

        client = posthog.setup()

        self.assertEqual(client.api_key, "phc_project_key")
        self.assertFalse(client.disabled)

    def test_setup_prefers_project_api_key_over_api_key(self):
        posthog.api_key = "phc_api_key"
        posthog.project_api_key = "phc_project_key"

        client = posthog.setup()

        self.assertEqual(client.api_key, "phc_project_key")

    def test_setup_uses_api_key_when_project_api_key_is_blank(self):
        posthog.api_key = " phc_api_key "
        posthog.project_api_key = " \n\t "

        client = posthog.setup()

        self.assertEqual(client.api_key, "phc_api_key")
        self.assertFalse(client.disabled)

    def test_setup_propagates_project_root(self):
        posthog.api_key = "phc_test"
        posthog.project_root = "/path/to/project"

        client = posthog.setup()

        self.assertEqual(client.project_root, "/path/to/project")

    def test_setup_propagates_and_updates_privacy_mode(self):
        posthog.api_key = "phc_test"
        posthog.privacy_mode = True

        client = posthog.setup()

        self.assertTrue(client.privacy_mode)

        posthog.privacy_mode = False

        self.assertIs(posthog.setup(), client)
        self.assertFalse(client.privacy_mode)


class TestModuleLevelWrappers(unittest.TestCase):
    """Test that module-level wrapper functions in posthog/__init__.py
    correctly propagate all parameters to the Client methods."""

    def setUp(self):
        self.mock_client = mock.MagicMock()
        self._original_client = posthog.default_client
        posthog.default_client = self.mock_client

    def tearDown(self):
        posthog.default_client = self._original_client

    def test_group_identify_propagates_distinct_id(self):
        posthog.group_identify(
            "company",
            "company_123",
            {"name": "Awesome Inc."},
            distinct_id="user_456",
        )
        self.mock_client.group_identify.assert_called_once_with(
            group_type="company",
            group_key="company_123",
            properties={"name": "Awesome Inc."},
            timestamp=None,
            uuid=None,
            disable_geoip=None,
            distinct_id="user_456",
        )

    def test_group_identify_distinct_id_defaults_to_none(self):
        posthog.group_identify("company", "company_123")
        call_kwargs = self.mock_client.group_identify.call_args[1]
        self.assertIsNone(call_kwargs["distinct_id"])

    @parameterized.expand([("group_identify",), ("alias",)])
    def test_timestamp_annotation_accepts_datetime_and_string(self, function_name):
        timestamp_type = get_type_hints(getattr(posthog, function_name))["timestamp"]
        self.assertEqual(timestamp_type, datetime.datetime | str | None)

    @parameterized.expand(
        [
            ("get_all_flags", "get_all_flags"),
            ("get_all_flags_and_payloads", "get_all_flags_and_payloads"),
        ]
    )
    def test_flag_keys_to_evaluate_propagated(self, _name, method_name):
        fn = getattr(posthog, method_name)
        fn("user_123", flag_keys_to_evaluate=["flag-1", "flag-2"])
        call_kwargs = getattr(self.mock_client, method_name).call_args[1]
        self.assertEqual(call_kwargs["flag_keys_to_evaluate"], ["flag-1", "flag-2"])

    @parameterized.expand(
        [
            ("get_all_flags", "get_all_flags"),
            ("get_all_flags_and_payloads", "get_all_flags_and_payloads"),
        ]
    )
    def test_flag_keys_to_evaluate_defaults_to_none(self, _name, method_name):
        fn = getattr(posthog, method_name)
        fn("user_123")
        call_kwargs = getattr(self.mock_client, method_name).call_args[1]
        self.assertIsNone(call_kwargs["flag_keys_to_evaluate"])
