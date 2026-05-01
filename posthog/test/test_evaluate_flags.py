import unittest
import warnings
from unittest import mock

from parameterized import parameterized

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

    def tearDown(self):
        self.client.shutdown()

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

    @parameterized.expand(
        [
            ("boolean_flag_is_enabled", "boolean-flag", True),
            ("disabled_flag_is_disabled", "disabled-flag", False),
            ("variant_flag_is_enabled", "variant-flag", True),
        ]
    )
    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_is_enabled(self, _name, key, expected, patch_capture, patch_flags):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        self.assertEqual(flags.is_enabled(key), expected)

        flag_called = [
            c
            for c in patch_capture.call_args_list
            if c[0]
            and c[0][0] == "$feature_flag_called"
            and c[1]["properties"]["$feature_flag"] == key
        ]
        self.assertEqual(len(flag_called), 1)

    @parameterized.expand(
        [
            (
                "variant_flag_returns_variant_string",
                "variant-flag",
                "variant-value",
                {
                    "$feature_flag_response": "variant-value",
                    "$feature_flag_id": 2,
                    "$feature_flag_version": 23,
                    "$feature_flag_reason": "Matched condition set 3",
                    "$feature_flag_request_id": "request-id-1",
                    "locally_evaluated": False,
                },
            ),
            (
                "boolean_flag_returns_true",
                "boolean-flag",
                True,
                {
                    "$feature_flag_response": True,
                    "$feature_flag_id": 1,
                    "$feature_flag_version": 12,
                    "locally_evaluated": False,
                },
            ),
            (
                "disabled_flag_returns_false",
                "disabled-flag",
                False,
                {"$feature_flag_response": False, "locally_evaluated": False},
            ),
        ]
    )
    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_flag_known_keys(
        self, _name, key, expected, expected_props, patch_capture, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        self.assertEqual(flags.get_flag(key), expected)

        by_key = {
            c[1]["properties"]["$feature_flag"]: c[1]["properties"]
            for c in patch_capture.call_args_list
            if c[0] and c[0][0] == "$feature_flag_called"
        }
        self.assertIn(key, by_key)
        for prop, value in expected_props.items():
            self.assertEqual(by_key[key][prop], value)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_flag_missing_key_emits_flag_missing_error(
        self, patch_capture, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        self.assertIsNone(flags.get_flag("missing-flag"))

        by_key = {
            c[1]["properties"]["$feature_flag"]: c[1]["properties"]
            for c in patch_capture.call_args_list
            if c[0] and c[0][0] == "$feature_flag_called"
        }
        self.assertIsNone(by_key["missing-flag"]["$feature_flag_response"])
        self.assertEqual(by_key["missing-flag"]["$feature_flag_error"], "flag_missing")

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_errors_while_computing_flags_propagates_to_event(
        self, patch_capture, patch_flags
    ):
        # Response-level errors are combined with per-flag errors so each
        # $feature_flag_called event carries the granular error code(s).
        response = _flags_response_fixture()
        response["errorsWhileComputingFlags"] = True
        patch_flags.return_value = response

        flags = self.client.evaluate_flags("user-1")
        flags.is_enabled("boolean-flag")  # known flag — only response-level error
        flags.is_enabled("missing-flag")  # missing — both errors combined

        by_key = {
            c[1]["properties"]["$feature_flag"]: c[1]["properties"]
            for c in patch_capture.call_args_list
            if c[0] and c[0][0] == "$feature_flag_called"
        }
        self.assertEqual(
            by_key["boolean-flag"]["$feature_flag_error"],
            "errors_while_computing_flags",
        )
        self.assertEqual(
            by_key["missing-flag"]["$feature_flag_error"],
            "errors_while_computing_flags,flag_missing",
        )

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
    def test_explicit_device_id_overrides_context(self, patch_flags):
        # Parity with the deprecated single-flag methods: callers can pass an
        # explicit device_id to bypass whatever the context resolver returns.
        patch_flags.return_value = _flags_response_fixture()

        self.client.evaluate_flags("user-1", device_id="explicit-device")

        self.assertEqual(
            patch_flags.call_args.kwargs.get("device_id"), "explicit-device"
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
    def test_only_accessed_returns_empty_when_no_flags_accessed(self, patch_flags):
        # The method honors its name: nothing accessed → empty snapshot, no fallback.
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        accessed = flags.only_accessed()

        self.assertEqual(accessed.keys, [])

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

    @mock.patch("posthog.client.flags")
    def test_capture_exception_forwards_flags_snapshot(self, patch_flags):
        # Auto/manual exception captures should be able to attach a flags snapshot the
        # same way capture() does, so $exception events carry the same flag context.
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")

        with mock.patch.object(self.client, "capture") as inner_capture:
            try:
                raise ValueError("boom")
            except ValueError as exc:
                self.client.capture_exception(exc, distinct_id="user-1", flags=flags)

        self.assertEqual(inner_capture.call_count, 1)
        forwarded = inner_capture.call_args.kwargs.get("flags")
        self.assertIs(forwarded, flags)

    @mock.patch("posthog.client.flags")
    def test_capture_warns_and_uses_flags_when_both_flags_and_send_feature_flags_set(
        self, patch_flags
    ):
        # `flags` always wins regardless of `send_feature_flags`. We log a warning so
        # the precedence isn't surprising when both are provided.
        patch_flags.return_value = _flags_response_fixture()
        flags = self.client.evaluate_flags("user-1")
        calls_before = patch_flags.call_count

        with self.assertLogs("posthog", level="WARNING") as logs:
            self.client.capture(
                "page_viewed",
                distinct_id="user-1",
                flags=flags,
                send_feature_flags=True,
            )

        self.assertEqual(patch_flags.call_count, calls_before)
        self.assertTrue(
            any(
                "Both `flags` and `send_feature_flags` were passed" in m
                for m in logs.output
            )
        )


class TestDeprecationWarnings(unittest.TestCase):
    def setUp(self):
        self.client = Client(FAKE_TEST_API_KEY)

    def tearDown(self):
        self.client.shutdown()

    @parameterized.expand(
        [
            ("feature_enabled", "feature_enabled", ("boolean-flag", "user-1"), {}),
            ("get_feature_flag", "get_feature_flag", ("boolean-flag", "user-1"), {}),
            (
                "get_feature_flag_payload",
                "get_feature_flag_payload",
                ("variant-flag", "user-1"),
                {},
            ),
        ]
    )
    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_legacy_single_flag_methods_emit_deprecation_warning(
        self, _name, method_name, args, kwargs, patch_capture, patch_flags
    ):
        patch_flags.return_value = _flags_response_fixture()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            getattr(self.client, method_name)(*args, **kwargs)

        deprecation = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and method_name in str(w.message)
        ]
        self.assertEqual(len(deprecation), 1)
        self.assertIn("evaluate_flags", str(deprecation[0].message))

    @mock.patch("posthog.client.flags")
    def test_capture_send_feature_flags_emits_deprecation_warning(self, patch_flags):
        patch_flags.return_value = _flags_response_fixture()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.client.capture(
                "page_viewed", distinct_id="user-1", send_feature_flags=True
            )

        deprecation = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "send_feature_flags" in str(w.message)
        ]
        self.assertEqual(len(deprecation), 1)
        self.assertIn("evaluate_flags", str(deprecation[0].message))

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_feature_enabled_does_not_cascade_deprecation_warnings(
        self, patch_capture, patch_flags
    ):
        # `feature_enabled` calls `_get_feature_flag_result` directly so the user only
        # sees one warning, not three (one each from feature_enabled →
        # get_feature_flag → get_feature_flag_result if it had one).
        patch_flags.return_value = _flags_response_fixture()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.client.feature_enabled("boolean-flag", "user-1")

        deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        self.assertEqual(len(deprecation), 1)


if __name__ == "__main__":
    unittest.main()
