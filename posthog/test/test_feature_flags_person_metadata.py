"""Local evaluation must not match `person_metadata` filters against person properties.

A cohort can filter on Person metadata > First seen (`type: "person_metadata"`, key
`created_at`). /flags reads that value from the persons table and ignores a `created_at`
person property, so a caller passing `created_at` must get /flags' answer, not a local
one computed from that property.

See https://github.com/PostHog/posthog/issues/137503
"""

import unittest
from unittest import mock

from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY

FIRST_SEEN_FILTER = {
    "key": "created_at",
    "type": "person_metadata",
    "operator": "is_date_after",
    "value": "2025-01-01",
}


def _flag(properties):
    return {
        "id": 1,
        "key": "first-seen-flag",
        "active": True,
        "filters": {"groups": [{"properties": properties, "rollout_percentage": 100}]},
    }


IN_COHORT = [{"key": "id", "value": 7, "type": "cohort"}]
FIRST_SEEN_COHORT = {"7": {"type": "AND", "values": [FIRST_SEEN_FILTER]}}


class TestPersonMetadataFilters(unittest.TestCase):
    def _client(self, properties, cohorts=None):
        client = Client(FAKE_TEST_API_KEY, secret_key=FAKE_TEST_API_KEY)
        client.feature_flags = [_flag(properties)]
        client.cohorts = cohorts or {}
        return client

    @mock.patch("posthog.client.flags")
    def test_cohort_filter_falls_back_to_flags_api(self, patch_flags):
        # The person was created in 2020, so /flags answers False.
        patch_flags.return_value = {"featureFlags": {"first-seen-flag": False}}
        client = self._client(IN_COHORT, FIRST_SEEN_COHORT)

        result = client.get_feature_flag(
            "first-seen-flag",
            "user-123",
            person_properties={"created_at": "2026-06-01"},
        )

        self.assertFalse(result)
        self.assertEqual(patch_flags.call_count, 1)

    @mock.patch("posthog.client.flags")
    def test_condition_filter_falls_back_to_flags_api(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"first-seen-flag": False}}
        client = self._client([FIRST_SEEN_FILTER])

        result = client.get_feature_flag(
            "first-seen-flag",
            "user-123",
            person_properties={"created_at": "2026-06-01"},
        )

        self.assertFalse(result)
        self.assertEqual(patch_flags.call_count, 1)

    @mock.patch("posthog.client.flags")
    def test_only_evaluate_locally_leaves_the_flag_unresolved(self, patch_flags):
        client = self._client(IN_COHORT, FIRST_SEEN_COHORT)

        result = client.get_feature_flag(
            "first-seen-flag",
            "user-123",
            person_properties={"created_at": "2026-06-01"},
            only_evaluate_locally=True,
        )

        self.assertIsNone(result)
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_person_filter_on_created_at_is_still_local(self, patch_flags):
        client = self._client([{**FIRST_SEEN_FILTER, "type": "person"}])

        result = client.get_feature_flag(
            "first-seen-flag",
            "user-123",
            person_properties={"created_at": "2026-06-01"},
        )

        self.assertTrue(result)
        self.assertEqual(patch_flags.call_count, 0)
