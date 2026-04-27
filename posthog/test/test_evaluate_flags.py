import unittest
import warnings
from unittest import mock

from posthog.client import Client
from posthog.feature_flag_evaluations import FeatureFlagEvaluations
from posthog.test.test_utils import FAKE_TEST_API_KEY


def _flags_response_fixture():
    return {
        "flags": {
            "variant-flag": {
                "key": "variant-flag",
                "enabled": True,
                "variant": "variant-value",
                "reason": {"code": "variant", "description": "Matched condition set 3"},
                "metadata": {"id": 2, "version": 23, "payload": '{"key": "value"}'},
            },
            "boolean-flag": {
                "key": "boolean-flag",
                "enabled": True,
                "variant": None,
                "reason": {"code": "boolean", "description": "Matched condition set 1"},
                "metadata": {"id": 1, "version": 12},
            },
            "disabled-flag": {
                "key": "disabled-flag",
                "enabled": False,
                "variant": None,
                "reason": {
                    "code": "boolean",
                    "description": "Did not match any condition",
                },
                "metadata": {"id": 3, "version": 2},
            },
        },
        "requestId": "request-id-1",
        "evaluatedAt": 1640995200000,
    }


class TestEvaluateFlagsRemote(unittest.TestCase):
    def setUp(self):
        self.client = Client(FAKE_TEST_API_KEY)

    @mock.patch("posthog.client.flags")
    def test_returns_a_FeatureFlagEvaluations_instance(self, patch_flags):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")
        self.assertIsInstance(flags, FeatureFlagEvaluations)
        self.assertEqual(patch_flags.call_count, 1)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_does_not_fire_events_for_unaccessed_flags(
        self, patch_capture, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()
        self.client.evaluate_flags("user-1")
        feature_flag_called = [
            c
            for c in patch_capture.call_args_list
            if c[0] and c[0][0] == "$feature_flag_called"
        ]
        self.assertEqual(len(feature_flag_called), 0)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_is_enabled_returns_correct_values_and_fires_events(
        self, patch_capture, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        self.assertTrue(flags.is_enabled("boolean-flag"))
        self.assertFalse(flags.is_enabled("disabled-flag"))
        self.assertTrue(flags.is_enabled("variant-flag"))

        feature_flag_called = [
            c
            for c in patch_capture.call_args_list
            if c[0] and c[0][0] == "$feature_flag_called"
        ]
        self.assertEqual(len(feature_flag_called), 3)
        keys = sorted(c[1]["properties"]["$feature_flag"] for c in feature_flag_called)
        self.assertEqual(keys, ["boolean-flag", "disabled-flag", "variant-flag"])

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_flag_returns_variant_or_bool_with_full_metadata(
        self, patch_capture, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        self.assertEqual(flags.get_flag("variant-flag"), "variant-value")
        self.assertEqual(flags.get_flag("boolean-flag"), True)
        self.assertEqual(flags.get_flag("disabled-flag"), False)
        self.assertIsNone(flags.get_flag("missing-flag"))

        by_key = {
            c[1]["properties"]["$feature_flag"]: c[1]["properties"]
            for c in patch_capture.call_args_list
            if c[0] and c[0][0] == "$feature_flag_called"
        }
        self.assertEqual(
            by_key["variant-flag"]["$feature_flag_response"], "variant-value"
        )
        self.assertEqual(by_key["variant-flag"]["$feature_flag_id"], 2)
        self.assertEqual(by_key["variant-flag"]["$feature_flag_version"], 23)
        self.assertEqual(
            by_key["variant-flag"]["$feature_flag_reason"], "Matched condition set 3"
        )
        self.assertEqual(
            by_key["variant-flag"]["$feature_flag_request_id"], "request-id-1"
        )
        self.assertFalse(by_key["variant-flag"]["locally_evaluated"])

        self.assertIsNone(by_key["missing-flag"]["$feature_flag_response"])
        self.assertEqual(by_key["missing-flag"]["$feature_flag_error"], "flag_missing")

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_dedupes_repeated_access(self, patch_capture, patch_flags):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        flags.is_enabled("boolean-flag")
        flags.is_enabled("boolean-flag")
        flags.get_flag("boolean-flag")

        boolean_calls = [
            c
            for c in patch_capture.call_args_list
            if c[0]
            and c[0][0] == "$feature_flag_called"
            and c[1]["properties"]["$feature_flag"] == "boolean-flag"
        ]
        self.assertEqual(len(boolean_calls), 1)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_flag_payload_does_not_fire_event(self, patch_capture, patch_flags):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        self.assertEqual(flags.get_flag_payload("variant-flag"), {"key": "value"})
        self.assertIsNone(flags.get_flag_payload("missing-flag"))

        feature_flag_called = [
            c
            for c in patch_capture.call_args_list
            if c[0] and c[0][0] == "$feature_flag_called"
        ]
        self.assertEqual(len(feature_flag_called), 0)

    @mock.patch("posthog.client.flags")
    def test_forwards_flag_keys_to_request(self, patch_flags):
        patch_flags.return_value = _flags_response_fixture()

        self.client.evaluate_flags("user-1", flag_keys=["boolean-flag", "variant-flag"])

        kwargs = patch_flags.call_args.kwargs
        self.assertEqual(
            kwargs.get("flag_keys_to_evaluate"),
            ["boolean-flag", "variant-flag"],
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_empty_distinct_id_returns_empty_snapshot_without_events(
        self, patch_capture, patch_flags
    ):
        flags = self.client.evaluate_flags()  # no distinct_id, no context
        self.assertEqual(flags.keys, [])
        flags.is_enabled("any-flag")
        flags.get_flag("any-flag")

        feature_flag_called = [
            c
            for c in patch_capture.call_args_list
            if c[0] and c[0][0] == "$feature_flag_called"
        ]
        self.assertEqual(len(feature_flag_called), 0)


class TestEvaluateFlagsFiltering(unittest.TestCase):
    def setUp(self):
        self.client = Client(FAKE_TEST_API_KEY)

    def tearDown(self):
        self.client.shutdown()

    @mock.patch("posthog.client.flags")
    def test_only_accessed_returns_only_accessed_flags(self, patch_flags):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")
        flags.is_enabled("boolean-flag")
        flags.get_flag("variant-flag")

        accessed = flags.only_accessed()
        self.assertEqual(sorted(accessed.keys), ["boolean-flag", "variant-flag"])

    @mock.patch("posthog.client.flags")
    def test_only_accessed_falls_back_with_warning_when_empty(self, patch_flags):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        with self.assertLogs("posthog", level="WARNING") as logs:
            accessed = flags.only_accessed()

        self.assertEqual(
            sorted(accessed.keys),
            ["boolean-flag", "disabled-flag", "variant-flag"],
        )
        self.assertTrue(
            any(
                "only_accessed() was called before any flags were accessed" in m
                for m in logs.output
            )
        )

    @mock.patch("posthog.client.flags")
    def test_only_drops_unknown_keys_with_warning(self, patch_flags):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        with self.assertLogs("posthog", level="WARNING") as logs:
            only = flags.only(["boolean-flag", "does-not-exist"])

        self.assertEqual(only.keys, ["boolean-flag"])
        self.assertTrue(any("does-not-exist" in m for m in logs.output))

    @mock.patch("posthog.client.flags")
    def test_filtered_snapshots_do_not_back_propagate_access(self, patch_flags):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")
        flags.is_enabled("boolean-flag")
        filtered = flags.only_accessed()

        filtered.is_enabled("variant-flag")

        self.assertEqual(flags.only_accessed().keys, ["boolean-flag"])


class TestCaptureWithFlagsSnapshot(unittest.TestCase):
    def setUp(self):
        self.client = Client(FAKE_TEST_API_KEY)

    def tearDown(self):
        self.client.shutdown()

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "_enqueue")
    def test_capture_with_flags_attaches_feature_properties(
        self, patch_enqueue, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        self.client.capture("page_viewed", distinct_id="user-1", flags=flags)

        # Find the page_viewed enqueue (skip $feature_flag_called events from access)
        page_viewed = next(
            (
                call
                for call in patch_enqueue.call_args_list
                if call[0][0]["event"] == "page_viewed"
            ),
            None,
        )
        self.assertIsNotNone(page_viewed)
        properties = page_viewed[0][0]["properties"]
        self.assertEqual(properties["$feature/variant-flag"], "variant-value")
        self.assertEqual(properties["$feature/boolean-flag"], True)
        self.assertEqual(properties["$feature/disabled-flag"], False)
        self.assertEqual(
            sorted(properties["$active_feature_flags"]),
            ["boolean-flag", "variant-flag"],
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "_enqueue")
    def test_capture_with_only_accessed_attaches_only_those_flags(
        self, patch_enqueue, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")
        flags.is_enabled("boolean-flag")

        self.client.capture(
            "page_viewed", distinct_id="user-1", flags=flags.only_accessed()
        )

        page_viewed = next(
            (
                call
                for call in patch_enqueue.call_args_list
                if call[0][0]["event"] == "page_viewed"
            ),
            None,
        )
        properties = page_viewed[0][0]["properties"]
        self.assertEqual(properties["$feature/boolean-flag"], True)
        self.assertNotIn("$feature/variant-flag", properties)
        self.assertNotIn("$feature/disabled-flag", properties)
        self.assertEqual(properties["$active_feature_flags"], ["boolean-flag"])

    @mock.patch("posthog.client.flags")
    def test_capture_with_flags_does_not_make_extra_flags_request(self, patch_flags):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")
        calls_before = patch_flags.call_count

        self.client.capture("page_viewed", distinct_id="user-1", flags=flags)

        self.assertEqual(patch_flags.call_count, calls_before)


class TestDeprecationWarnings(unittest.TestCase):
    def setUp(self):
        self.client = Client(FAKE_TEST_API_KEY)

    def tearDown(self):
        self.client.shutdown()

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_feature_enabled_emits_deprecation_warning(
        self, patch_capture, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.client.feature_enabled("boolean-flag", "user-1")

        self.assertTrue(
            any(
                issubclass(w.category, DeprecationWarning)
                and "feature_enabled" in str(w.message)
                for w in caught
            )
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_emits_deprecation_warning(
        self, patch_capture, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.client.get_feature_flag("boolean-flag", "user-1")

        self.assertTrue(
            any(
                issubclass(w.category, DeprecationWarning)
                and "get_feature_flag" in str(w.message)
                for w in caught
            )
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_payload_emits_deprecation_warning(
        self, patch_capture, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.client.get_feature_flag_payload("variant-flag", "user-1")

        self.assertTrue(
            any(
                issubclass(w.category, DeprecationWarning)
                and "get_feature_flag_payload" in str(w.message)
                for w in caught
            )
        )

    @mock.patch("posthog.client.flags")
    def test_capture_send_feature_flags_true_emits_deprecation_warning(
        self, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.client.capture(
                "page_viewed", distinct_id="user-1", send_feature_flags=True
            )

        self.assertTrue(
            any(
                issubclass(w.category, DeprecationWarning)
                and "send_feature_flags" in str(w.message)
                for w in caught
            )
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_feature_enabled_does_not_emit_nested_warnings(
        self, patch_capture, patch_flags
    ):
        """feature_enabled should emit exactly one warning, not cascade through
        get_feature_flag → get_feature_flag_result.
        """
        patch_flags.return_value = _flags_response_fixture()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.client.feature_enabled("boolean-flag", "user-1")

        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        self.assertEqual(len(deprecation_warnings), 1)


if __name__ == "__main__":
    unittest.main()
