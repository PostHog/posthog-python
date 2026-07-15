"""Tests for the ``$feature_flag_has_experiment`` property on ``$feature_flag_called``.

The server reports ``has_experiment`` in the flag definition and in the ``/flags``
response metadata. Every ``$feature_flag_called`` event carries the signal as a
``$feature_flag_has_experiment`` boolean property. When the server does not report the
field (older deployments), it defaults to ``False``.
"""

import unittest
from unittest import mock

from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY


def _flags_response(has_experiment):
    """Build a ``/flags`` response for ``person-flag`` with the given ``has_experiment``.

    ``has_experiment=None`` omits the field entirely, simulating an older server.
    """
    metadata = {"id": 23, "version": 42, "payload": "300"}
    if has_experiment is not None:
        metadata["has_experiment"] = has_experiment
    return {
        "flags": {
            "person-flag": {
                "key": "person-flag",
                "enabled": True,
                "variant": None,
                "reason": {"description": "Matched condition set 1"},
                "metadata": metadata,
            },
        },
    }


class TestFeatureFlagHasExperimentRemoteEval(unittest.TestCase):
    def setUp(self):
        self.client = Client(FAKE_TEST_API_KEY)

    def _captured_properties(self, patch_capture):
        _, kwargs = patch_capture.call_args
        return kwargs["properties"]

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_experiment_flag_sends_true(self, patch_capture, patch_flags):
        patch_flags.return_value = _flags_response(has_experiment=True)

        self.client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._captured_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], True)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_non_experiment_flag_sends_false(self, patch_capture, patch_flags):
        patch_flags.return_value = _flags_response(has_experiment=False)

        self.client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._captured_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], False)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_missing_has_experiment_sends_false(self, patch_capture, patch_flags):
        # A server that does not report the field (older deployment) defaults to False.
        patch_flags.return_value = _flags_response(has_experiment=None)

        self.client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._captured_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], False)


class TestFeatureFlagHasExperimentLocalEval(unittest.TestCase):
    """The local-evaluation poller stores ``has_experiment`` verbatim in the flag
    definition, so locally-evaluated flags report the same signal."""

    def _local_flag(self, has_experiment):
        flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "person-flag",
            "active": True,
            "filters": {
                "groups": [{"properties": [], "rollout_percentage": 100}],
                "payloads": {"true": "300"},
            },
        }
        if has_experiment is not None:
            flag["has_experiment"] = has_experiment
        return flag

    def _captured_properties(self, patch_capture):
        _, kwargs = patch_capture.call_args
        return kwargs["properties"]

    @mock.patch.object(Client, "capture")
    def test_local_experiment_flag_sends_true(self, patch_capture):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [self._local_flag(has_experiment=True)]

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._captured_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], True)

    @mock.patch.object(Client, "capture")
    def test_local_non_experiment_flag_sends_false(self, patch_capture):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [self._local_flag(has_experiment=False)]

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._captured_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], False)

    @mock.patch.object(Client, "capture")
    def test_local_missing_has_experiment_sends_false(self, patch_capture):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [self._local_flag(has_experiment=None)]

        client.get_feature_flag_result("person-flag", "some-distinct-id")

        properties = self._captured_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], False)


class TestFeatureFlagHasExperimentEvaluateFlags(unittest.TestCase):
    """The ``evaluate_flags()`` snapshot path fires ``$feature_flag_called`` on access
    and must carry the same signal as the single-flag path."""

    def _flags_response(self, has_experiment):
        metadata = {"id": 2, "version": 23, "payload": '{"key": "value"}'}
        if has_experiment is not None:
            metadata["has_experiment"] = has_experiment
        return {
            "flags": {
                "variant-flag": {
                    "key": "variant-flag",
                    "enabled": True,
                    "variant": "variant-value",
                    "reason": {"code": "variant", "description": "Matched set 3"},
                    "metadata": metadata,
                },
            },
            "requestId": "request-id-1",
            "evaluatedAt": 1640995200000,
        }

    def _called_properties(self, patch_capture):
        for call in patch_capture.call_args_list:
            if call[0] and call[0][0] == "$feature_flag_called":
                return call[1]["properties"]
        raise AssertionError("no $feature_flag_called event captured")

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_experiment_flag_sends_true(self, patch_capture, patch_flags):
        patch_flags.return_value = self._flags_response(has_experiment=True)
        flags = Client(FAKE_TEST_API_KEY).evaluate_flags("user-1")

        flags.get_flag("variant-flag")

        properties = self._called_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], True)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_non_experiment_flag_sends_false(self, patch_capture, patch_flags):
        patch_flags.return_value = self._flags_response(has_experiment=False)
        flags = Client(FAKE_TEST_API_KEY).evaluate_flags("user-1")

        flags.get_flag("variant-flag")

        properties = self._called_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], False)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_missing_has_experiment_sends_false(self, patch_capture, patch_flags):
        patch_flags.return_value = self._flags_response(has_experiment=None)
        flags = Client(FAKE_TEST_API_KEY).evaluate_flags("user-1")

        flags.get_flag("variant-flag")

        properties = self._called_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], False)

    @mock.patch.object(Client, "capture")
    def test_local_evaluation_snapshot_sends_signal(self, patch_capture):
        # Exercises the snapshot's local-eval branch, where has_experiment is sourced
        # from the stored flag definition rather than a /flags response.
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "key": "local-flag",
                "active": True,
                "has_experiment": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 100}],
                    "payloads": {"true": "300"},
                },
            }
        ]

        flags = client.evaluate_flags("user-1")
        flags.get_flag("local-flag")

        properties = self._called_properties(patch_capture)
        self.assertIs(properties["$feature_flag_has_experiment"], True)
