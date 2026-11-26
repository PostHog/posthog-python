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
from posthog.feature_flags import (
    InconclusiveMatchError,
    RequiresServerEvaluation,
    match_property_group,
)
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
PLAN_PAID = {"key": "plan", "type": "person", "operator": "exact", "value": "paid"}
VARIANTS = {
    "variants": [
        {"key": "new-user", "rollout_percentage": 50},
        {"key": "existing-user", "rollout_percentage": 50},
    ]
}


def _cohort(*filters, group_type="AND"):
    return {"7": {"type": group_type, "values": list(filters)}}


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

# The regex engines of Python and /flags disagree on this filter for "foo\n", so it is
# unsupported for that value. The definition alone doesn't tell.
UNSUPPORTED_REGEX = {
    "key": "name",
    "type": "person",
    "operator": "regex",
    "value": "foo.*$",
}
IN_STATIC_COHORT = {"key": "id", "value": 404, "type": "cohort"}

# Flags whose first condition can't be checked locally, followed by a catch-all.
LATER_CONDITION_DECIDES = [
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
        "existing-user",
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
        "existing-user",
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
                "another_unsupported_type",
                _flag(_condition([{**FIRST_SEEN_FILTER, "type": "behavioral"}])),
                {},
            ),
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

    @parameterized.expand(LATER_CONDITION_DECIDES)
    @mock.patch("posthog.client.flags")
    def test_later_condition_decides_the_flag(self, _, flags, expected, patch_flags):
        client = self._client(flags)

        for person_properties in (CREATED_IN_2026, {}):
            snapshot = client.evaluate_flags(
                "user-123", person_properties=person_properties, flag_keys=["target"]
            )
            self.assertEqual(snapshot.get_flag("target"), expected)
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_early_exit_after_unsupported_condition_stays_inconclusive(
        self, patch_flags
    ):
        patch_flags.return_value = {"featureFlags": {"target": True}}
        client = self._client(
            [
                _flag(
                    _condition(IN_COHORT),
                    _condition([], rollout_percentage=0),
                    _condition([]),
                    key="target",
                    early_exit=True,
                )
            ]
        )

        snapshot = client.evaluate_flags(
            "user-123", person_properties=CREATED_IN_2026, flag_keys=["target"]
        )

        self.assertIs(snapshot.get_flag("target"), True)
        self.assertEqual(patch_flags.call_count, 1)

    @parameterized.expand(
        [
            ("and", "AND", IN_COHORT, "free", False),
            ("and_not_in", "AND", NOT_IN_COHORT, "free", True),
            ("or", "OR", IN_COHORT, "paid", True),
            ("or_not_in", "OR", NOT_IN_COHORT, "paid", False),
        ]
    )
    @mock.patch("posthog.client.flags")
    def test_another_filter_still_decides_the_cohort(
        self, _, group_type, flag_properties, plan, expected, patch_flags
    ):
        dependency = _flag(_condition([FIRST_SEEN_FILTER]), key="dependency")
        for filters in (
            (PLAN_PAID, FIRST_SEEN_FILTER),
            (FIRST_SEEN_FILTER, PLAN_PAID),
            ({"type": "AND", "values": [FIRST_SEEN_FILTER]}, PLAN_PAID),
            (DEPENDS_ON_FIRST_SEEN, PLAN_PAID),
            ({"key": "id", "value": 8, "type": "cohort"}, PLAN_PAID),
        ):
            client = self._client(
                [_flag(_condition(flag_properties)), dependency],
                {
                    **_cohort(*filters, group_type=group_type),
                    "8": {"type": "AND", "values": [FIRST_SEEN_FILTER]},
                },
            )
            for created_at in (CREATED_IN_2026, {}):
                with self.subTest(filters=filters, created_at=created_at):
                    snapshot = client.evaluate_flags(
                        "user-123",
                        person_properties={"plan": plan, **created_at},
                        flag_keys=["first-seen-flag"],
                        only_evaluate_locally=True,
                    )
                    self.assertIs(snapshot.get_flag("first-seen-flag"), expected)
                    self.assertEqual(dict(snapshot.unresolved_flags), {})
        self.assertEqual(patch_flags.call_count, 0)

    @parameterized.expand(
        [
            ("and", "AND", IN_COHORT, "paid"),
            ("and_not_in", "AND", NOT_IN_COHORT, "paid"),
            ("or", "OR", IN_COHORT, "free"),
            ("or_not_in", "OR", NOT_IN_COHORT, "free"),
        ]
    )
    def test_undecided_cohort_is_unsupported(
        self, _, group_type, flag_properties, plan
    ):
        for filters in (
            (PLAN_PAID, FIRST_SEEN_FILTER),
            (FIRST_SEEN_FILTER, PLAN_PAID),
        ):
            client = self._client(
                [_flag(_condition(flag_properties))],
                _cohort(*filters, group_type=group_type),
            )

            snapshot = client.evaluate_flags(
                "user-123",
                person_properties={"plan": plan},
                only_evaluate_locally=True,
            )

            self.assertEqual(
                dict(snapshot.unresolved_flags),
                {"first-seen-flag": UnresolvedFlagReason.UNSUPPORTED_DEFINITION},
            )

    @parameterized.expand(
        [
            (
                "cohort",
                [_flag(_condition(IN_COHORT), key="target")],
                _cohort(DEPENDS_ON_FIRST_SEEN, UNSUPPORTED_REGEX),
            ),
            (
                "cohort_other_order",
                [_flag(_condition(IN_COHORT), key="target")],
                _cohort(UNSUPPORTED_REGEX, DEPENDS_ON_FIRST_SEEN),
            ),
            (
                "condition",
                [
                    _flag(
                        _condition([DEPENDS_ON_FIRST_SEEN, UNSUPPORTED_REGEX]),
                        key="target",
                    )
                ],
                {},
            ),
            (
                "earlier_condition",
                [
                    _flag(
                        _condition([UNSUPPORTED_REGEX]),
                        _condition([DEPENDS_ON_FIRST_SEEN]),
                        key="target",
                    )
                ],
                {},
            ),
        ]
    )
    def test_the_highest_precedence_reason_is_reported(self, _, flags, cohorts):
        dependency = _flag(_condition([FIRST_SEEN_FILTER]), key="dependency")
        client = self._client([*flags, dependency], cohorts)

        snapshot = client.evaluate_flags(
            "user-123",
            person_properties={"name": "foo\n"},
            flag_keys=["target"],
            only_evaluate_locally=True,
        )

        self.assertEqual(
            dict(snapshot.unresolved_flags),
            {"target": UnresolvedFlagReason.UNSUPPORTED_DEFINITION},
        )

    @parameterized.expand(
        [
            (
                "earlier_condition",
                [_condition([UNSUPPORTED_REGEX]), _condition([DEPENDS_ON_FIRST_SEEN])],
            ),
            (
                "same_condition",
                [_condition([DEPENDS_ON_FIRST_SEEN, UNSUPPORTED_REGEX])],
            ),
        ]
    )
    def test_static_cohort_dependency_keeps_the_higher_reason(self, _, conditions):
        # The dependency needs the server, which ends evaluation. The earlier
        # unsupported filter's reason still outranks unresolved_dependency.
        dependency = _flag(_condition([IN_STATIC_COHORT]), key="dependency")
        client = self._client([_flag(*conditions, key="target"), dependency], {})

        snapshot = client.evaluate_flags(
            "user-123",
            person_properties={"name": "foo\n"},
            flag_keys=["target"],
            only_evaluate_locally=True,
        )

        self.assertEqual(
            dict(snapshot.unresolved_flags),
            {"target": UnresolvedFlagReason.UNSUPPORTED_DEFINITION},
        )

    def test_a_cohort_that_stops_early_keeps_the_higher_reason(self):
        # A dependency on a static cohort ends the cohort at once, whatever comes after it.
        flags_by_key = {
            "dependency": _flag(_condition([IN_STATIC_COHORT]), key="dependency")
        }
        with self.assertRaises(RequiresServerEvaluation) as raised:
            match_property_group(
                {"type": "AND", "values": [UNSUPPORTED_REGEX, DEPENDS_ON_FIRST_SEEN]},
                {"name": "foo\n"},
                {},
                flags_by_key=flags_by_key,
                evaluation_cache={},
                distinct_id="user-123",
            )
        self.assertEqual(
            raised.exception.reason, UnresolvedFlagReason.UNSUPPORTED_DEFINITION
        )

    @parameterized.expand(
        [
            ("server_filters", [DEPENDS_ON_FIRST_SEEN, FIRST_SEEN_FILTER]),
            ("inconclusive_filter", [DEPENDS_ON_FIRST_SEEN, UNSUPPORTED_REGEX]),
        ]
    )
    def test_the_reason_does_not_depend_on_filter_order(self, _, filters):
        flags_by_key = {
            "dependency": _flag(_condition([FIRST_SEEN_FILTER]), key="dependency")
        }
        for ordered in (filters, filters[::-1]):
            with self.assertRaises(InconclusiveMatchError) as raised:
                match_property_group(
                    {"type": "AND", "values": ordered},
                    {"name": "foo\n"},
                    {},
                    flags_by_key=flags_by_key,
                    evaluation_cache={},
                    distinct_id="user-123",
                )
            self.assertEqual(
                raised.exception.reason, UnresolvedFlagReason.UNSUPPORTED_DEFINITION
            )

    @parameterized.expand(
        [
            ("and", "AND", "paid"),
            ("or", "OR", "free"),
        ]
    )
    @mock.patch("posthog.client.flags")
    def test_undecided_cohort_lets_a_later_condition_decide(
        self, _, group_type, plan, patch_flags
    ):
        for filters in (
            (PLAN_PAID, FIRST_SEEN_FILTER),
            (FIRST_SEEN_FILTER, PLAN_PAID),
        ):
            client = self._client(
                LATER_CONDITION_DECIDES[0][1],
                _cohort(*filters, group_type=group_type),
            )

            snapshot = client.evaluate_flags(
                "user-123", person_properties={"plan": plan}, flag_keys=["target"]
            )

            self.assertEqual(snapshot.get_flag("target"), "existing-user")
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_another_filter_still_rules_the_condition_out(self, patch_flags):
        client = self._client([_flag(_condition([FIRST_SEEN_FILTER, PLAN_PAID]))])

        snapshot = client.evaluate_flags("user-123", person_properties={"plan": "free"})

        self.assertIs(snapshot.get_flag("first-seen-flag"), False)
        self.assertEqual(patch_flags.call_count, 0)

    @parameterized.expand(
        [
            ("person", {"type": "person"}),
            ("untyped", {}),
            ("null_type", {"type": None}),
            ("empty_type", {"type": ""}),
        ]
    )
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
