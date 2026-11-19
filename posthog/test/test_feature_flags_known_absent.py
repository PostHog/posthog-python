"""Known-absent property keys in local evaluation and the /flags request body."""

import itertools
import unittest
from unittest import mock

import pytest
from parameterized import parameterized

from posthog import AsyncPosthog
from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY

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
            ("unknown operator", "icontains_multi", ["pro"]),
            ("unsupported negated operator", "not_between", [1, 2]),
            ("malformed numeric value", "gt", "not-a-number"),
            ("malformed date value", "is_date_before", "not-a-date"),
        ]
    )
    def test_unsupported_filter_on_listed_key_still_falls_back(
        self, _name, operator, value
    ):
        flag = _flag("flag", [_person_filter(operator, value)])
        self.assert_remote(flag, person_property_keys_known_absent=["plan"])

    def test_group_key_applies_only_to_its_group_type(self):
        flag = _flag(
            "flag",
            [{"key": "tier", "operator": "is_not_set", "type": "group"}],
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

    def test_group_key_overrides_the_sdk_added_group_key(self):
        flag = _flag(
            "flag",
            [{"key": "$group_key", "operator": "is_set", "type": "group"}],
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
            [{"key": "tier", "operator": "is_not_set", "type": "group"}],
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
        ]
    )
    def test_wrong_type_raises_without_a_request(self, _name, kwargs):
        with self.assertRaises(TypeError):
            self.client.get_feature_flag("flag", "user-1", **kwargs)
        self.flags.assert_not_called()


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
