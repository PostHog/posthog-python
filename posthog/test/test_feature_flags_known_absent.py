"""Known-absent property keys in local evaluation and the /flags request body."""

import itertools
import unittest
from unittest import mock

import pytest
from parameterized import parameterized

from posthog import AsyncPosthog
from posthog.client import Client
from posthog.feature_flags import (
    InconclusiveMatchError,
    _mark_known_absent_properties,
    match_cohort,
)
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.types import UnresolvedFlagReason

pytestmark = [
    pytest.mark.filterwarnings(
        r"ignore:`(feature_enabled|get_feature_flag|get_feature_flag_payload)` is deprecated:DeprecationWarning"
    ),
    pytest.mark.filterwarnings(
        r"ignore:`send_feature_flags` is deprecated:DeprecationWarning"
    ),
]

REMOTE_VALUE = "remote-value"


def _flag(key, properties, **filters):
    return {
        "id": 1,
        "key": key,
        "active": True,
        "filters": {
            "groups": [{"properties": properties, "rollout_percentage": 100}],
            **filters,
        },
    }


def _person_filter(operator, value=None, key="plan"):
    prop = {"key": key, "operator": operator, "type": "person"}
    if value is not None:
        prop["value"] = value
    return prop


def _person_metadata_filter(operator, value=None, key="created_at"):
    return {**_person_filter(operator, value, key=key), "type": "person_metadata"}


def _group_filter(operator, value=None, key="tier", group_type_index=0):
    return {
        **_person_filter(operator, value, key=key),
        "type": "group",
        "group_type_index": group_type_index,
    }


def _group_filter_without_index(operator, value=None, key="tier"):
    prop = _group_filter(operator, value, key=key)
    del prop["group_type_index"]
    return prop


class TestKnownAbsentLocalEvaluation(unittest.TestCase):
    def setUp(self):
        self.client = Client(FAKE_TEST_API_KEY, secret_key="test")
        self.client.capture = mock.Mock()
        self.client.group_type_mapping = {"0": "organization", "1": "project"}
        flags_patch = mock.patch("posthog.client.flags")
        self.flags = flags_patch.start()
        self.addCleanup(flags_patch.stop)
        self.flags.return_value = {
            "featureFlags": {"flag": REMOTE_VALUE, "beta-feature": REMOTE_VALUE}
        }

    def evaluate(self, flag, **kwargs):
        self.client.feature_flags = [flag]
        return self.client.get_feature_flag(flag["key"], "user-1", **kwargs)

    def assert_local(self, flag, expected, **kwargs):
        self.assertEqual(self.evaluate(flag, **kwargs), expected)
        self.flags.assert_not_called()

    def assert_remote(self, flag, **kwargs):
        self.assertEqual(self.evaluate(flag, **kwargs), REMOTE_VALUE)
        self.assertEqual(self.flags.call_count, 1)

    def assert_unresolved_locally(self, flag):
        self.client.feature_flags = [flag]
        snapshot = self.client.evaluate_flags(
            "user-1",
            person_property_keys_known_absent=["plan"],
            only_evaluate_locally=True,
        )
        self.assertIsNone(snapshot.get_flag("flag"))
        self.assertEqual(
            snapshot.unresolved_flags,
            {"flag": UnresolvedFlagReason.UNSUPPORTED_DEFINITION},
        )
        self.flags.assert_not_called()

    @parameterized.expand(
        [
            ("is_not_set", None, True),
            ("is_set", None, False),
            ("exact", "pro", False),
            ("exact", ["pro", "free"], False),
            ("is_not", "pro", True),
            ("icontains", "pro", False),
            ("not_icontains", "pro", True),
            ("starts_with", "pro", False),
            ("not_starts_with", "pro", True),
            ("ends_with", "pro", False),
            ("not_ends_with", "pro", True),
            ("regex", "^pro", False),
            ("not_regex", "^pro", True),
            ("gt", "10", False),
            ("gte", "10", False),
            ("lt", "10", False),
            ("lte", "10", False),
            ("is_date_before", "2025-01-01T10:00:00Z", False),
            ("is_date_after", "2025-01-01T10:00:00Z", False),
            ("semver_eq", "1.2.3", False),
            ("semver_neq", "1.2.3", False),
            ("semver_gt", "1.2.3", False),
            ("semver_tilde", "1.2.3", False),
        ]
    )
    def test_listed_key_resolves_operator_locally(self, operator, value, expected):
        flag = _flag("flag", [_person_filter(operator, value)])
        self.assert_local(flag, expected, person_property_keys_known_absent=["plan"])

    def test_listed_key_overrides_a_supplied_value(self):
        flag = _flag("flag", [_person_filter("is_not_set")])
        self.assert_local(
            flag,
            True,
            person_properties={"plan": "pro"},
            person_property_keys_known_absent=["plan"],
        )

    def test_listed_key_overrides_the_sdk_added_distinct_id(self):
        flag = _flag("flag", [_person_filter("is_not_set", key="distinct_id")])
        self.assert_local(flag, True, person_property_keys_known_absent=["distinct_id"])

    def test_omitted_key_that_is_not_listed_stays_inconclusive(self):
        flag = _flag("flag", [_person_filter("is_not_set")])
        self.assert_remote(flag, person_property_keys_known_absent=["region"])
        body = self.flags.call_args.kwargs
        self.assertEqual(body["person_property_keys_known_absent"], ["region"])

    def test_person_metadata_key_is_ignored(self):
        flag = _flag(
            "flag",
            [_person_filter("is_set", key="__posthog_person_metadata__created_at")],
        )
        self.assert_local(
            flag,
            True,
            person_properties={"__posthog_person_metadata__created_at": "2024"},
            person_property_keys_known_absent=["__posthog_person_metadata__created_at"],
        )

    @parameterized.expand(
        [
            ("icontains_multi", ["pro"]),
            ("not_icontains_multi", ["pro"]),
            ("between", [1, 2]),
            ("not_between", [1, 2]),
            ("some_future_operator", "pro"),
        ]
    )
    def test_unimplemented_operator_on_listed_key_still_falls_back(
        self, operator, value
    ):
        flag = _flag("flag", [_person_filter(operator, value)])
        self.assert_unresolved_locally(flag)
        self.assert_remote(flag, person_property_keys_known_absent=["plan"])

    @parameterized.expand(
        [
            ("not_regex invalid", "not_regex", "[", True),
            ("regex invalid", "regex", "[", False),
            ("not_regex non-portable", "not_regex", r"\p{L}", True),
            ("regex non-portable", "regex", r"\p{L}", False),
            ("not_regex inline flag", "not_regex", "(?i)enterprise", True),
            ("regex inline flag", "regex", "(?i)enterprise", False),
            ("not_regex word boundary", "not_regex", r"\bpro\b", True),
            ("gt", "gt", "not-a-number", False),
            ("gte", "gte", "not-a-number", False),
            ("lt", "lt", "not-a-number", False),
            ("lte", "lte", "not-a-number", False),
            ("semver_eq", "semver_eq", "not-a-version", False),
            ("semver_neq", "semver_neq", "not-a-version", False),
            ("semver_gt", "semver_gt", "not-a-version", False),
            ("semver_tilde", "semver_tilde", "not-a-version", False),
            ("semver_caret", "semver_caret", "not-a-version", False),
            ("semver_wildcard", "semver_wildcard", "not-a-version", False),
            ("is_date_before", "is_date_before", "not-a-date", False),
            ("is_date_after", "is_date_after", "not-a-date", False),
        ]
    )
    def test_listed_key_resolves_without_reading_the_condition_value(
        self, _name, operator, value, expected
    ):
        flag = _flag("flag", [_person_filter(operator, value)])
        self.client.feature_flags = [flag]
        snapshot = self.client.evaluate_flags(
            "user-1",
            person_property_keys_known_absent=["plan"],
            only_evaluate_locally=True,
        )
        self.assertIs(snapshot.get_flag("flag"), expected)
        self.assertEqual(snapshot.unresolved_flags, {})

        self.assert_local(flag, expected, person_property_keys_known_absent=["plan"])

    @parameterized.expand(
        [
            ("is_not omitted", "is_not", False),
            ("is_not null", "is_not", True),
            ("exact null", "exact", True),
            ("gt null", "gt", True),
            ("not_regex omitted", "not_regex", False),
            ("regex null", "regex", True),
            ("semver_gt null", "semver_gt", True),
            ("is_date_after omitted", "is_date_after", False),
        ]
    )
    def test_listed_key_with_omitted_or_null_value_stays_inconclusive(
        self, _name, operator, explicit_null
    ):
        prop = _person_filter(operator)
        if explicit_null:
            prop["value"] = None
        flag = _flag("flag", [prop])
        self.assert_unresolved_locally(flag)
        self.assert_remote(flag, person_property_keys_known_absent=["plan"])

    @parameterized.expand([("is_not_set", True), ("is_set", False)])
    def test_listed_key_with_null_value_resolves_presence_operator(
        self, operator, expected
    ):
        prop = {**_person_filter(operator), "value": None}
        flag = _flag("flag", [prop])
        self.assert_local(flag, expected, person_property_keys_known_absent=["plan"])

    @parameterized.expand(
        [
            ("is_not_set", None),
            ("is_set", None),
            ("is_date_before", "2024-01-01T00:00:00Z"),
            ("is_date_after", "2024-01-01T00:00:00Z"),
        ]
    )
    def test_listed_key_does_not_resolve_a_person_metadata_filter(
        self, operator, value
    ):
        flag = _flag(
            "flag", [_person_metadata_filter(operator, value, key="created_at")]
        )
        self.assert_remote(flag, person_property_keys_known_absent=["created_at"])

    @parameterized.expand(
        [
            ("is_not_set", None, False),
            ("is_set", None, True),
            ("is_date_before", "2024-01-01T00:00:00Z", True),
            ("is_date_after", "2024-01-01T00:00:00Z", False),
        ]
    )
    def test_person_metadata_filter_on_listed_key_reads_the_supplied_value(
        self, operator, value, expected
    ):
        flag = _flag(
            "flag", [_person_metadata_filter(operator, value, key="created_at")]
        )
        self.assert_local(
            flag,
            expected,
            person_properties={"created_at": "2020-01-01T00:00:00Z"},
            person_property_keys_known_absent=["created_at"],
        )

    def test_duplicate_person_key_still_reads_the_supplied_value_outside_its_scope(
        self,
    ):
        flag = _flag(
            "flag", [_person_metadata_filter("is_set", None, key="created_at")]
        )
        self.assert_local(
            flag,
            True,
            person_properties={"created_at": "2020-01-01T00:00:00Z"},
            person_property_keys_known_absent=["created_at", "created_at"],
        )

    def test_duplicate_group_key_still_reads_the_supplied_value_outside_its_scope(
        self,
    ):
        flag = _flag(
            "flag",
            [_group_filter("is_set", key="tier", group_type_index=1)],
            aggregation_group_type_index=0,
        )
        self.assert_local(
            flag,
            True,
            groups={"organization": "org-1"},
            group_properties={"organization": {"tier": "gold"}},
            group_property_keys_known_absent={"organization": ["tier", "tier"]},
        )

    @parameterized.expand(
        [
            ("is_not_set", None),
            ("is_date_before", "2024-01-01T00:00:00Z"),
            ("is_date_after", "2024-01-01T00:00:00Z"),
        ]
    )
    def test_listed_key_does_not_resolve_a_person_metadata_cohort_leaf(
        self, operator, value
    ):
        self.client.cohorts = {
            "5": {
                "type": "AND",
                "values": [_person_metadata_filter(operator, value, key="created_at")],
            }
        }
        flag = _flag("flag", [{"key": "id", "value": 5, "type": "cohort"}])
        self.assert_remote(flag, person_property_keys_known_absent=["created_at"])

    def test_person_keys_do_not_resolve_a_group_filter_in_a_person_flag(self):
        flag = _flag("flag", [_group_filter("is_not_set", key="tier")])
        self.assert_remote(flag, person_property_keys_known_absent=["tier"])

    def test_group_keys_do_not_resolve_a_person_filter_in_a_group_flag(self):
        flag = _flag(
            "flag", [_person_filter("is_not_set")], aggregation_group_type_index=0
        )
        self.assert_remote(
            flag,
            groups={"organization": "org-1"},
            group_property_keys_known_absent={"organization": ["plan"]},
        )

    def test_group_keys_do_not_resolve_a_person_cohort_leaf_in_a_group_flag(self):
        self.client.cohorts = {
            "5": {"type": "AND", "values": [_person_filter("is_not_set")]}
        }
        flag = _flag(
            "flag",
            [{"key": "id", "value": 5, "type": "cohort"}],
            aggregation_group_type_index=0,
        )
        self.assert_remote(
            flag,
            groups={"organization": "org-1"},
            group_property_keys_known_absent={"organization": ["plan"]},
        )

    def test_group_keys_do_not_resolve_a_filter_for_another_group_type(self):
        flag = _flag(
            "flag",
            [_group_filter("is_not_set", key="tier", group_type_index=1)],
            aggregation_group_type_index=0,
        )
        self.assert_remote(
            flag,
            groups={"organization": "org-1"},
            group_property_keys_known_absent={"organization": ["tier"]},
        )

    def test_group_key_applies_only_to_its_group_type(self):
        flag = _flag(
            "flag",
            [_group_filter("is_not_set", key="tier")],
            aggregation_group_type_index=0,
        )
        groups = {"organization": "org-1", "project": "proj-1"}
        self.assert_remote(
            flag,
            groups=groups,
            group_property_keys_known_absent={"project": ["tier"]},
        )
        self.assertEqual(
            self.flags.call_args.kwargs["group_property_keys_known_absent"],
            {"project": ["tier"]},
        )

        self.flags.reset_mock()
        self.assert_local(
            flag,
            True,
            groups=groups,
            group_properties={"organization": {"tier": "gold"}},
            group_property_keys_known_absent={"organization": ["tier"]},
        )

    @parameterized.expand(
        [
            ("exact", "enterprise", False),
            ("is_not_set", None, True),
        ]
    )
    def test_group_filter_without_index_reads_the_condition_group_type(
        self, operator, value, expected
    ):
        flag = _flag(
            "flag",
            [_group_filter_without_index(operator, value)],
            aggregation_group_type_index=0,
        )
        self.assert_local(
            flag,
            expected,
            groups={"organization": "acme"},
            group_properties={"organization": {"tier": "enterprise"}},
            group_property_keys_known_absent={"organization": ["tier"]},
        )

    def test_group_filter_without_index_reads_the_condition_level_group_type(self):
        flag = {
            "id": 1,
            "key": "flag",
            "active": True,
            "filters": {
                "aggregation_group_type_index": None,
                "groups": [
                    {
                        "properties": [_group_filter_without_index("is_not_set")],
                        "rollout_percentage": 100,
                        "aggregation_group_type_index": 0,
                    }
                ],
            },
        }
        self.assert_local(
            flag,
            True,
            groups={"organization": "acme"},
            group_properties={"organization": {"tier": "enterprise"}},
            group_property_keys_known_absent={"organization": ["tier"]},
        )

    def test_group_filter_without_index_ignores_keys_of_another_group_type(self):
        flag = _flag(
            "flag",
            [_group_filter_without_index("is_not_set")],
            aggregation_group_type_index=0,
        )
        self.assert_local(
            flag,
            False,
            groups={"organization": "acme", "project": "proj-1"},
            group_properties={"organization": {"tier": "enterprise"}},
            group_property_keys_known_absent={"project": ["tier"]},
        )

    def test_group_key_overrides_the_sdk_added_group_key(self):
        flag = _flag(
            "flag",
            [_group_filter("is_set", key="$group_key")],
            aggregation_group_type_index=0,
        )
        self.assert_local(
            flag,
            False,
            groups={"organization": "org-1"},
            group_property_keys_known_absent={"organization": ["$group_key"]},
        )

    def test_person_keys_do_not_apply_to_group_properties(self):
        flag = _flag(
            "flag",
            [_group_filter("is_not_set", key="tier")],
            aggregation_group_type_index=0,
        )
        self.assert_remote(
            flag,
            groups={"organization": "org-1"},
            person_property_keys_known_absent=["tier"],
        )

    @parameterized.expand([(False, True), (True, False)])
    def test_cohort_leaf_uses_known_absence_before_negation(self, negation, expected):
        self.client.cohorts = {
            "5": {
                "type": "AND",
                "values": [
                    {
                        "key": "plan",
                        "operator": "is_not_set",
                        "type": "person",
                        "negation": negation,
                    }
                ],
            }
        }
        flag = _flag("flag", [{"key": "id", "value": 5, "type": "cohort"}])
        self.assert_local(flag, expected, person_property_keys_known_absent=["plan"])

    @parameterized.expand([(False,), (True,)])
    def test_cohort_leaf_unsupported_operator_falls_back_before_known_absence(
        self, negation
    ):
        cohort = {
            "type": "AND",
            "values": [
                {
                    "key": "plan",
                    "operator": "not_between",
                    "value": [1, 2],
                    "type": "person",
                    "negation": negation,
                }
            ],
        }
        cohort_filter = {"key": "id", "value": 5, "type": "cohort"}
        listed_person_properties, _ = _mark_known_absent_properties(
            {}, {}, ["plan"], None, None
        )
        with self.assertRaises(InconclusiveMatchError) as ctx:
            match_cohort(cohort_filter, listed_person_properties, {"5": cohort})
        self.assertEqual(
            ctx.exception.reason, UnresolvedFlagReason.UNSUPPORTED_DEFINITION
        )

        self.client.cohorts = {"5": cohort}
        flag = _flag("flag", [cohort_filter])
        self.assert_remote(flag, person_property_keys_known_absent=["plan"])

    def test_flag_dependency_uses_the_same_listing(self):
        dependency = _flag("dependency", [_person_filter("is_not_set")])
        dependency["id"] = 2
        dependent = _flag(
            "flag",
            [
                {
                    "key": "dependency",
                    "operator": "flag_evaluates_to",
                    "value": True,
                    "type": "flag",
                    "dependency_chain": ["dependency"],
                }
            ],
        )
        self.client.feature_flags = [dependency, dependent]
        value = self.client.get_feature_flag(
            "flag", "user-1", person_property_keys_known_absent=["plan"]
        )
        self.assertIs(value, True)
        self.flags.assert_not_called()

    @parameterized.expand(
        [
            (
                "group dependency of a person flag",
                None,
                [_group_filter("is_not_set", key="tier")],
                0,
                {"person_property_keys_known_absent": ["tier"]},
            ),
            (
                "person dependency of a group flag",
                0,
                [_person_filter("is_not_set")],
                None,
                {"group_property_keys_known_absent": {"organization": ["plan"]}},
            ),
            (
                "dependency on another group type",
                0,
                [_group_filter("is_not_set", key="tier", group_type_index=1)],
                1,
                {"group_property_keys_known_absent": {"organization": ["tier"]}},
            ),
        ]
    )
    def test_flag_dependency_does_not_read_keys_of_another_aggregation(
        self,
        _name,
        parent_aggregation,
        dependency_properties,
        dependency_aggregation,
        listing,
    ):
        dependency = _flag(
            "dependency",
            dependency_properties,
            aggregation_group_type_index=dependency_aggregation,
        )
        dependency["id"] = 2
        dependent = _flag(
            "flag",
            [
                {
                    "key": "dependency",
                    "operator": "flag_evaluates_to",
                    "value": True,
                    "type": "flag",
                    "dependency_chain": ["dependency"],
                }
            ],
            aggregation_group_type_index=parent_aggregation,
        )
        self.client.feature_flags = [dependency, dependent]
        value = self.client.get_feature_flag(
            "flag",
            "user-1",
            groups={"organization": "org-1", "project": "proj-1"},
            **listing,
        )
        self.assertEqual(value, REMOTE_VALUE)
        self.assertEqual(self.flags.call_count, 1)

    def test_listed_enrollment_key_continues_to_release_conditions(self):
        flag = _flag("beta-feature", [], feature_enrollment=True)
        self.assert_local(
            flag,
            True,
            person_properties={"$feature_enrollment/beta-feature": True},
            person_property_keys_known_absent=["$feature_enrollment/beta-feature"],
        )

    def test_listed_enrollment_key_continues_to_holdouts(self):
        flag = _flag(
            "beta-feature",
            [],
            feature_enrollment=True,
            holdout={"id": 1, "exclusion_percentage": 100},
        )
        self.assert_local(
            flag,
            "holdout-1",
            person_property_keys_known_absent=["$feature_enrollment/beta-feature"],
        )

    def test_listed_enrollment_key_still_reports_unsupported_group_enrollment(self):
        flag = _flag(
            "beta-feature",
            [],
            feature_enrollment=True,
            aggregation_group_type_index=0,
        )
        self.assert_remote(
            flag,
            groups={"organization": "org-1"},
            person_property_keys_known_absent=["$feature_enrollment/beta-feature"],
        )

    def test_evaluate_flags_and_all_flags_honor_listing(self):
        self.client.feature_flags = [_flag("flag", [_person_filter("is_not_set")])]
        snapshot = self.client.evaluate_flags(
            "user-1",
            person_properties={"plan": "pro"},
            person_property_keys_known_absent=["plan"],
            only_evaluate_locally=True,
        )
        self.assertIs(snapshot.is_enabled("flag"), True)
        self.assertEqual(
            self.client.get_all_flags(
                "user-1", person_property_keys_known_absent=["plan"]
            ),
            {"flag": True},
        )
        self.flags.assert_not_called()


class TestKnownAbsentRemoteBody(unittest.TestCase):
    def setUp(self):
        self.client = Client(FAKE_TEST_API_KEY)
        self.client.capture = mock.Mock()
        flags_patch = mock.patch("posthog.client.flags")
        self.flags = flags_patch.start()
        self.addCleanup(flags_patch.stop)
        self.flags.return_value = {"featureFlags": {"flag": True}}

    def test_fallback_sends_listed_keys(self):
        self.client.get_feature_flag(
            "flag",
            "user-1",
            groups={"organization": "org-1"},
            person_property_keys_known_absent=["plan"],
            group_property_keys_known_absent={"organization": ["tier"]},
        )
        body = self.flags.call_args.kwargs
        self.assertEqual(body["person_property_keys_known_absent"], ["plan"])
        self.assertEqual(
            body["group_property_keys_known_absent"], {"organization": ["tier"]}
        )

    @parameterized.expand(
        [
            ("not provided", {}),
            (
                "empty",
                {
                    "person_property_keys_known_absent": [],
                    "group_property_keys_known_absent": {"organization": []},
                },
            ),
        ]
    )
    def test_omits_fields_without_listed_keys(self, _name, kwargs):
        self.client.get_feature_flag("flag", "user-1", **kwargs)
        body = self.flags.call_args.kwargs
        self.assertNotIn("person_property_keys_known_absent", body)
        self.assertNotIn("group_property_keys_known_absent", body)

    def test_every_remote_entry_point_sends_listed_keys(self):
        kwargs = {"person_property_keys_known_absent": ["plan"]}
        calls = [
            lambda: self.client.evaluate_flags("user-1", **kwargs),
            lambda: self.client.get_all_flags_and_payloads("user-1", **kwargs),
            lambda: self.client.get_feature_flag_result("flag", "user-1", **kwargs),
            lambda: self.client.get_feature_flag_payload("flag", "user-1", **kwargs),
            lambda: self.client.feature_enabled("flag", "user-1", **kwargs),
            lambda: self.client.get_feature_variants("user-1", **kwargs),
            lambda: self.client.get_flags_decision("user-1", **kwargs),
        ]
        for call in calls:
            self.flags.reset_mock()
            call()
            self.assertEqual(
                self.flags.call_args.kwargs["person_property_keys_known_absent"],
                ["plan"],
            )

    def test_send_feature_flags_options_send_listed_keys(self):
        client = Client(FAKE_TEST_API_KEY, send=False)
        client.capture(
            "event",
            distinct_id="user-1",
            send_feature_flags={
                "only_evaluate_locally": False,
                "person_property_keys_known_absent": ["plan"],
            },
        )
        self.assertEqual(
            self.flags.call_args.kwargs["person_property_keys_known_absent"], ["plan"]
        )

    @parameterized.expand(
        [
            ("person", {"person_property_keys_known_absent": ["plan"] * 1001}),
            (
                "group",
                {"group_property_keys_known_absent": {"organization": ["t"] * 1001}},
            ),
        ]
    )
    def test_over_limit_raises_without_a_request(self, _name, kwargs):
        for call in (
            lambda: self.client.get_feature_flag("flag", "user-1", **kwargs),
            lambda: self.client.evaluate_flags("user-1", **kwargs),
            lambda: self.client.get_all_flags("user-1", **kwargs),
            lambda: self.client.get_flags_decision("user-1", **kwargs),
        ):
            with self.assertRaisesRegex(ValueError, "at most 1000 keys"):
                call()
        self.flags.assert_not_called()

    def test_send_feature_flags_over_limit_raises_without_a_request(self):
        client = Client(FAKE_TEST_API_KEY, send=False, debug=True)
        with self.assertRaisesRegex(ValueError, "at most 1000 keys"):
            client.capture(
                "event",
                distinct_id="user-1",
                send_feature_flags={
                    "only_evaluate_locally": False,
                    "person_property_keys_known_absent": ["plan"] * 1001,
                },
            )
        self.flags.assert_not_called()

    def test_send_feature_flags_over_limit_drops_event_with_default_settings(self):
        client = Client(FAKE_TEST_API_KEY)
        with mock.patch.object(client, "_enqueue") as enqueue:
            result = client.capture(
                "event",
                distinct_id="user-1",
                send_feature_flags={
                    "only_evaluate_locally": False,
                    "person_property_keys_known_absent": ["plan"] * 1001,
                },
            )
        self.assertIsNone(result)
        enqueue.assert_not_called()
        self.flags.assert_not_called()

    def test_unbounded_iterable_raises_without_a_request(self):
        with self.assertRaisesRegex(ValueError, "at most 1000 keys"):
            self.client.get_flags_decision(
                "user-1", person_property_keys_known_absent=itertools.repeat("plan")
            )
        self.flags.assert_not_called()

    def test_get_flags_decision_sends_iterator_inputs(self):
        self.client.get_flags_decision(
            "user-1",
            groups={"organization": "org-1"},
            person_property_keys_known_absent=iter(["plan"]),
            group_property_keys_known_absent={"organization": iter(["tier"])},
        )
        body = self.flags.call_args.kwargs
        self.assertEqual(body["person_property_keys_known_absent"], ["plan"])
        self.assertEqual(
            body["group_property_keys_known_absent"], {"organization": ["tier"]}
        )

    def test_limit_counts_duplicates_and_is_per_group_type(self):
        self.client.get_feature_flag(
            "flag",
            "user-1",
            person_property_keys_known_absent=["plan"] * 1000,
            group_property_keys_known_absent={
                "organization": ["t"] * 1000,
                "project": ["t"] * 1000,
            },
        )
        body = self.flags.call_args.kwargs
        self.assertEqual(len(body["person_property_keys_known_absent"]), 1000)
        self.assertEqual(len(body["group_property_keys_known_absent"]["project"]), 1000)

    @parameterized.expand(
        [
            ("string", {"person_property_keys_known_absent": "plan"}),
            ("non-string key", {"person_property_keys_known_absent": [1]}),
            ("group list", {"group_property_keys_known_absent": ["tier"]}),
            ("non-string group type", {"group_property_keys_known_absent": {1: ["t"]}}),
            ("None group keys", {"group_property_keys_known_absent": {"org": None}}),
            ("bytes", {"person_property_keys_known_absent": b"plan"}),
            ("bytearray", {"person_property_keys_known_absent": bytearray()}),
            ("person dict", {"person_property_keys_known_absent": {"plan": "pro"}}),
            ("empty person dict", {"person_property_keys_known_absent": {}}),
            (
                "group dict",
                {"group_property_keys_known_absent": {"org": {"tier": "gold"}}},
            ),
            (
                "group bytearray",
                {"group_property_keys_known_absent": {"org": bytearray(b"t")}},
            ),
        ]
    )
    def test_wrong_type_raises_without_a_request(self, _name, kwargs):
        with self.assertRaises(TypeError):
            self.client.get_feature_flag("flag", "user-1", **kwargs)
        self.flags.assert_not_called()

    def test_dict_keys_view_is_accepted(self):
        self.client.get_feature_flag(
            "flag",
            "user-1",
            person_property_keys_known_absent={"plan": "pro"}.keys(),
            group_property_keys_known_absent={"org": {"tier": "gold"}.keys()},
        )
        body = self.flags.call_args.kwargs
        self.assertEqual(body["person_property_keys_known_absent"], ["plan"])
        self.assertEqual(body["group_property_keys_known_absent"], {"org": ["tier"]})


@pytest.mark.asyncio
async def test_async_evaluate_flags_sends_listed_keys():
    client = AsyncPosthog(FAKE_TEST_API_KEY, send=False)
    with mock.patch(
        "posthog.async_client._async_flags",
        new=mock.AsyncMock(return_value={"flags": {}}),
    ) as async_flags:
        await client.evaluate_flags(
            "user-1",
            person_property_keys_known_absent=["plan"],
            group_property_keys_known_absent={"organization": ["tier"], "x": []},
        )
        await client.evaluate_flags("user-1")
    first, second = async_flags.call_args_list
    assert first.kwargs["person_property_keys_known_absent"] == ["plan"]
    assert first.kwargs["group_property_keys_known_absent"] == {
        "organization": ["tier"]
    }
    assert "person_property_keys_known_absent" not in second.kwargs
    assert "group_property_keys_known_absent" not in second.kwargs
    await client.shutdown()


@pytest.mark.asyncio
async def test_async_evaluate_flags_rejects_over_limit_lists():
    client = AsyncPosthog(FAKE_TEST_API_KEY, send=False)
    with mock.patch(
        "posthog.async_client._async_flags", new=mock.AsyncMock()
    ) as async_flags:
        with pytest.raises(ValueError, match="at most 1000 keys"):
            await client.evaluate_flags(
                "user-1", person_property_keys_known_absent=["plan"] * 1001
            )
    async_flags.assert_not_called()
    await client.shutdown()
