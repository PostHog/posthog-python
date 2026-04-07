import unittest
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
