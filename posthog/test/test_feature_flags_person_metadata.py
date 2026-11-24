"""Local evaluation must not match `person_metadata` filters against person properties.

A cohort can filter on Person metadata > First seen (`type: "person_metadata"`, key
`created_at`). /flags reads that value from the persons table and ignores a `created_at`
person property, so a caller passing `created_at` must get /flags' answer, not a local
one computed from that property.

See https://github.com/PostHog/posthog/issues/137503
"""

import logging
import unittest
from unittest import mock

from parameterized import parameterized

from posthog.client import Client
from posthog.request import GetResponse
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.types import UnresolvedFlagReason

FIRST_SEEN_FILTER = {
    "key": "created_at",
    "type": "person_metadata",
    "operator": "is_date_after",
    "value": "2025-01-01",
}
IN_COHORT = [{"key": "id", "value": 7, "type": "cohort"}]
NOT_IN_COHORT = [{"key": "id", "value": 7, "type": "cohort", "operator": "not_in"}]
CREATED_IN_2026 = {"created_at": "2026-06-01"}
VARIANTS = {
    "variants": [
        {"key": "new-user", "rollout_percentage": 50},
        {"key": "existing-user", "rollout_percentage": 50},
    ]
}


def _cohort(*filters):
    return {"7": {"type": "AND", "values": list(filters)}}


FIRST_SEEN_COHORT = _cohort(FIRST_SEEN_FILTER)


def _condition(properties, rollout_percentage=100, **kwargs):
    return {
        "properties": properties,
        "rollout_percentage": rollout_percentage,
        **kwargs,
    }


def _flag(*conditions, key="first-seen-flag", **filters):
    return {
        "id": 1,
        "key": key,
        "active": True,
        "filters": {"groups": list(conditions), **filters},
    }


IN_COHORT_FLAG = _flag(_condition(IN_COHORT))
IN_CONDITION_FLAG = _flag(_condition([FIRST_SEEN_FILTER]))
DEPENDS_ON_FIRST_SEEN = {
    "key": "dependency",
    "type": "flag",
    "operator": "flag_evaluates_to",
    "value": True,
    "dependency_chain": ["dependency"],
}

# Flags whose first condition only /flags can check, and what /flags answers.
SERVER_ONLY_FIRST_CONDITION = [
    (
        "another_variant",
        [
            _flag(
                _condition(IN_COHORT, variant="new-user"),
                _condition([], variant="existing-user"),
                key="target",
                multivariate=VARIANTS,
            )
        ],
        "new-user",
    ),
    (
        "early_exit",
        [
            _flag(
                _condition(IN_COHORT, rollout_percentage=0),
                _condition([]),
                key="target",
                early_exit=True,
            )
        ],
        False,
    ),
    (
        "dependency",
        [
            _flag(_condition([FIRST_SEEN_FILTER]), key="dependency"),
            _flag(
                _condition([DEPENDS_ON_FIRST_SEEN], variant="new-user"),
                _condition([], variant="existing-user"),
                key="target",
                multivariate=VARIANTS,
            ),
        ],
        "new-user",
    ),
]


class TestPersonMetadataFilters(unittest.TestCase):
    def _client(self, flags, cohorts=FIRST_SEEN_COHORT, group_type_mapping=None):
        client = Client(
            FAKE_TEST_API_KEY,
            # A key no other test uses, so a poller leaked by another module never matches it.
            secret_key="test-person-metadata-filters",
            enable_local_evaluation=False,
            send=False,
        )
        self.addCleanup(client.shutdown)
        definitions = {
            "flags": flags,
            "group_type_mapping": group_type_mapping or {},
            "cohorts": cohorts,
        }
        response = GetResponse(data=definitions, etag=None, not_modified=False)
        with mock.patch("posthog.client.get", return_value=response):
            client._load_feature_flags()
        return client

    @parameterized.expand(
        [("cohort", IN_COHORT_FLAG), ("condition", IN_CONDITION_FLAG)]
    )
    @mock.patch("posthog.client.flags")
    def test_filter_falls_back_to_flags_api(self, _, flag, patch_flags):
        # The person was created in 2020, so /flags answers False.
        patch_flags.return_value = {"featureFlags": {"first-seen-flag": False}}
        client = self._client([flag])

        snapshot = client.evaluate_flags("user-123", person_properties=CREATED_IN_2026)

        self.assertIs(snapshot.get_flag("first-seen-flag"), False)
        self.assertEqual(patch_flags.call_count, 1)

    @parameterized.expand(
        [
            ("cohort", IN_COHORT_FLAG, FIRST_SEEN_COHORT),
            ("condition", IN_CONDITION_FLAG, {}),
            ("not_in_cohort", _flag(_condition(NOT_IN_COHORT)), FIRST_SEEN_COHORT),
            (
                "negated_cohort_leaf",
                IN_COHORT_FLAG,
                _cohort({**FIRST_SEEN_FILTER, "negation": True}),
            ),
        ]
    )
    @mock.patch("posthog.client.flags")
    def test_only_evaluate_locally_leaves_the_flag_unresolved(
        self, _, flag, cohorts, patch_flags
    ):
        client = self._client([flag], cohorts)

        for person_properties in (CREATED_IN_2026, {}):
            snapshot = client.evaluate_flags(
                "user-123",
                person_properties=person_properties,
                only_evaluate_locally=True,
            )
            self.assertIsNone(snapshot.get_flag("first-seen-flag"))
            self.assertEqual(
                dict(snapshot.unresolved_flags),
                {"first-seen-flag": UnresolvedFlagReason.UNSUPPORTED_DEFINITION},
            )
        self.assertEqual(patch_flags.call_count, 0)

    @parameterized.expand(
        [("cohort", IN_COHORT_FLAG), ("condition", IN_CONDITION_FLAG)]
    )
    def test_loading_the_definition_logs_a_warning(self, _, flag):
        with self.assertLogs("posthog", level=logging.WARNING) as logs:
            self._client([flag])

        self.assertTrue(
            any(
                "first-seen-flag" in record.getMessage()
                and "(reason: unsupported_definition)" in record.getMessage()
                for record in logs.records
            )
        )

    @parameterized.expand(SERVER_ONLY_FIRST_CONDITION)
    @mock.patch("posthog.client.flags")
    def test_later_condition_does_not_decide_the_flag(
        self, _, flags, remote, patch_flags
    ):
        patch_flags.return_value = {"featureFlags": {"target": remote}}
        client = self._client(flags)

        for person_properties in (CREATED_IN_2026, {}):
            patch_flags.reset_mock()
            snapshot = client.evaluate_flags(
                "user-123", person_properties=person_properties, flag_keys=["target"]
            )
            self.assertEqual(snapshot.get_flag("target"), remote)
            self.assertEqual(patch_flags.call_count, 1)

    @mock.patch("posthog.client.flags")
    def test_another_filter_still_rules_the_condition_out(self, patch_flags):
        plan = {"key": "plan", "type": "person", "operator": "exact", "value": "paid"}
        client = self._client([_flag(_condition([FIRST_SEEN_FILTER, plan]))])

        snapshot = client.evaluate_flags("user-123", person_properties={"plan": "free"})

        self.assertIs(snapshot.get_flag("first-seen-flag"), False)
        self.assertEqual(patch_flags.call_count, 0)

    @parameterized.expand([("person", {"type": "person"}), ("untyped", {})])
    @mock.patch("posthog.client.flags")
    def test_person_filter_on_created_at_is_still_local(
        self, _, filter_type, patch_flags
    ):
        prop = {**FIRST_SEEN_FILTER, **filter_type}
        if not filter_type:
            del prop["type"]
        client = self._client([_flag(_condition([prop]))])

        snapshot = client.evaluate_flags("user-123", person_properties=CREATED_IN_2026)

        self.assertIs(snapshot.get_flag("first-seen-flag"), True)
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_group_filter_is_still_local(self, patch_flags):
        prop = {
            "key": "plan",
            "type": "group",
            "group_type_index": 0,
            "operator": "exact",
            "value": "paid",
        }
        client = self._client(
            [_flag(_condition([prop]), aggregation_group_type_index=0)],
            group_type_mapping={"0": "company"},
        )

        snapshot = client.evaluate_flags(
            "user-123",
            groups={"company": "acme"},
            group_properties={"company": {"plan": "paid"}},
        )

        self.assertIs(snapshot.get_flag("first-seen-flag"), True)
        self.assertEqual(patch_flags.call_count, 0)
