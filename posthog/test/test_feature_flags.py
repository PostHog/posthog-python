import unittest

import mock
from freezegun import freeze_time

from posthog.client import Client
from posthog.feature_flags import InconclusiveMatchError, match_property
from posthog.request import APIError
from posthog.test.test_utils import FAKE_TEST_API_KEY


class TestLocalEvaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This ensures no real HTTP POST requests are made
        cls.capture_patch = mock.patch.object(Client, "capture")
        cls.capture_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.capture_patch.stop()

    def set_fail(self, e, batch):
        """Mark the failure handler"""
        print("FAIL", e, batch)
        self.failed = True

    def setUp(self):
        self.failed = False
        self.client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail)

    @mock.patch("posthog.client.get")
    def test_flag_person_properties(self, patch_get):
        self.client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "person-flag",
                "is_simple_flag": True,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "region",
                                    "operator": "exact",
                                    "value": ["USA"],
                                    "type": "person",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            }
        ]

        feature_flag_match = self.client.get_feature_flag(
            "person-flag", "some-distinct-id", person_properties={"region": "USA"}
        )

        not_feature_flag_match = self.client.get_feature_flag(
            "person-flag", "some-distinct-2", person_properties={"region": "Canada"}
        )

        self.assertTrue(feature_flag_match)
        self.assertFalse(not_feature_flag_match)

    @mock.patch("posthog.client.decide")
    @mock.patch("posthog.client.get")
    def test_flag_group_properties(self, patch_get, patch_decide):
        self.client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "group-flag",
                "is_simple_flag": True,
                "active": True,
                "filters": {
                    "aggregation_group_type_index": 0,
                    "groups": [
                        {
                            "properties": [
                                {
                                    "group_type_index": 0,
                                    "key": "name",
                                    "operator": "exact",
                                    "value": ["Project Name 1"],
                                    "type": "group",
                                }
                            ],
                            "rollout_percentage": 35,
                        }
                    ],
                },
            }
        ]

        self.client.group_type_mapping = {"0": "company", "1": "project"}

        # Group names not passed in
        self.assertFalse(
            self.client.get_feature_flag(
                "group-flag", "some-distinct-id", group_properties={"company": {"name": "Project Name 1"}}
            )
        )

        self.assertFalse(
            self.client.get_feature_flag(
                "group-flag", "some-distinct-2", group_properties={"company": {"name": "Project Name 2"}}
            )
        )

        # this is good
        self.assertTrue(
            self.client.get_feature_flag(
                "group-flag",
                "some-distinct-id",
                groups={"company": "amazon_without_rollout"},
                group_properties={"company": {"name": "Project Name 1"}},
            )
        )
        # rollout %
        self.assertFalse(
            self.client.get_feature_flag(
                "group-flag",
                "some-distinct-id",
                groups={"company": "amazon"},
                group_properties={"company": {"name": "Project Name 1"}},
            )
        )

        # property mismatch
        self.assertFalse(
            self.client.get_feature_flag(
                "group-flag",
                "some-distinct-2",
                groups={"company": "amazon_without_rollout"},
                group_properties={"company": {"name": "Project Name 2"}},
            )
        )
        self.assertEqual(patch_decide.call_count, 0)

        # Now group type mappings are gone, so fall back to /decide/
        patch_decide.return_value = {"featureFlags": {"group-flag": "decide-fallback-value"}}

        self.client.group_type_mapping = {}
        self.assertEqual(
            self.client.get_feature_flag(
                "group-flag",
                "some-distinct-id",
                groups={"company": "amazon"},
                group_properties={"company": {"name": "Project Name 1"}},
            ),
            "decide-fallback-value",
        )

        self.assertEqual(patch_decide.call_count, 1)

    @mock.patch("posthog.client.decide")
    @mock.patch("posthog.client.get")
    def test_flag_with_complex_definition(self, patch_get, patch_decide):
        patch_decide.return_value = {"featureFlags": {"complex-flag": "decide-fallback-value"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "complex-flag",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "region",
                                    "operator": "exact",
                                    "value": ["USA"],
                                    "type": "person",
                                },
                                {
                                    "key": "name",
                                    "operator": "exact",
                                    "value": ["Aloha"],
                                    "type": "person",
                                },
                            ],
                            "rollout_percentage": 100,
                        },
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "operator": "exact",
                                    "value": ["a@b.com", "b@c.com"],
                                    "type": "person",
                                },
                            ],
                            "rollout_percentage": 30,
                        },
                        {
                            "properties": [
                                {
                                    "key": "doesnt_matter",
                                    "operator": "exact",
                                    "value": ["1", "2"],
                                    "type": "person",
                                },
                            ],
                            "rollout_percentage": 0,
                        },
                    ],
                },
            }
        ]

        self.assertTrue(
            client.get_feature_flag(
                "complex-flag", "some-distinct-id", person_properties={"region": "USA", "name": "Aloha"}
            )
        )
        self.assertEqual(patch_decide.call_count, 0)

        # this distinctIDs hash is < rollout %
        self.assertTrue(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id_within_rollout?",
                person_properties={"region": "USA", "email": "a@b.com"},
            )
        )
        self.assertEqual(patch_decide.call_count, 0)

        # will fall back on `/decide`, as all properties present for second group, but that group resolves to false
        self.assertEqual(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id_outside_rollout?",
                person_properties={"region": "USA", "email": "a@b.com"},
            ),
            "decide-fallback-value",
        )
        self.assertEqual(patch_decide.call_count, 1)

        patch_decide.reset_mock()

        # same as above
        self.assertEqual(
            client.get_feature_flag("complex-flag", "some-distinct-id", person_properties={"doesnt_matter": "1"}),
            "decide-fallback-value",
        )
        self.assertEqual(patch_decide.call_count, 1)

        patch_decide.reset_mock()

        # this one will need to fall back
        self.assertEqual(
            client.get_feature_flag("complex-flag", "some-distinct-id", person_properties={"region": "USA"}),
            "decide-fallback-value",
        )
        self.assertEqual(patch_decide.call_count, 1)

        patch_decide.reset_mock()

        # won't need to fall back when all values are present
        self.assertFalse(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id_outside_rollout?",
                person_properties={"region": "USA", "email": "a@b.com", "name": "X", "doesnt_matter": "1"},
            )
        )
        self.assertEqual(patch_decide.call_count, 0)

    @mock.patch("posthog.client.decide")
    @mock.patch("posthog.client.get")
    def test_feature_flags_fallback_to_decide(self, patch_get, patch_decide):
        patch_decide.return_value = {"featureFlags": {"beta-feature": "alakazam", "beta-feature2": "alakazam2"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": True,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [{"key": "id", "value": 98, "operator": None, "type": "cohort"}],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "beta-feature2",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "region",
                                    "operator": "exact",
                                    "value": ["USA"],
                                    "type": "person",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]

        # beta-feature fallbacks to decide because property type is unknown
        feature_flag_match = client.get_feature_flag("beta-feature", "some-distinct-id")

        self.assertEqual(feature_flag_match, "alakazam")
        self.assertEqual(patch_decide.call_count, 1)

        # beta-feature2 fallbacks to decide because region property not given with call
        feature_flag_match = client.get_feature_flag("beta-feature2", "some-distinct-id")

        self.assertEqual(feature_flag_match, "alakazam2")
        self.assertEqual(patch_decide.call_count, 2)

    @mock.patch("posthog.client.decide")
    @mock.patch("posthog.client.get")
    def test_feature_flag_defaults_dont_hinder_regular_evaluation(self, patch_get, patch_decide):
        patch_decide.return_value = {"featureFlags": {}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": True,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 0,
                        }
                    ],
                },
            },
        ]

        # beta-feature resolves to False, so no matter the default, stays False
        self.assertFalse(client.get_feature_flag("beta-feature", "some-distinct-id", default=True))
        self.assertFalse(client.get_feature_flag("beta-feature", "some-distinct-id", default=False))

        # beta-feature2 falls back to decide, and whatever decide returns is the value
        self.assertFalse(client.get_feature_flag("beta-feature2", "some-distinct-id", default=False))
        self.assertEqual(patch_decide.call_count, 1)

        self.assertFalse(client.get_feature_flag("beta-feature2", "some-distinct-id", default=True))
        self.assertEqual(patch_decide.call_count, 2)

    @mock.patch("posthog.client.decide")
    @mock.patch("posthog.client.get")
    def test_feature_flag_defaults_come_into_play_only_when_decide_errors_out(self, patch_get, patch_decide):
        patch_decide.side_effect = APIError(400, "Decide error")
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = []

        # beta-feature2 falls back to decide, which on error falls back to default
        self.assertFalse(client.get_feature_flag("beta-feature2", "some-distinct-id", default=False))
        self.assertEqual(patch_decide.call_count, 1)

        self.assertTrue(client.get_feature_flag("beta-feature2", "some-distinct-id", default=True))
        self.assertEqual(patch_decide.call_count, 2)

    @mock.patch("posthog.client.decide")
    def test_experience_continuity_flag_not_evaluated_locally(self, patch_decide):
        patch_decide.return_value = {"featureFlags": {"beta-feature": "decide-fallback-value"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": False,
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ]
                },
                "ensure_experience_continuity": True,
            }
        ]
        # decide called always because experience_continuity is set
        self.assertTrue(client.get_feature_flag("beta-feature", "distinct_id"), "decide-fallback-value")
        self.assertEqual(patch_decide.call_count, 1)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.decide")
    def test_get_all_flags_with_fallback(self, patch_decide, patch_capture):
        patch_decide.return_value = {"featureFlags": {"beta-feature": "variant-1", "beta-feature2": "variant-2"}}
        client = self.client
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": False,
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "disabled-feature",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 0,
                        }
                    ]
                },
            },
            {
                "id": 3,
                "name": "Beta Feature",
                "key": "beta-feature2",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [{"key": "country", "value": "US"}],
                            "rollout_percentage": 0,
                        }
                    ]
                },
            },
        ]
        # beta-feature value overridden by /decide
        self.assertEqual(
            client.get_all_flags("distinct_id"),
            {"beta-feature": "variant-1", "beta-feature2": "variant-2", "disabled-feature": False},
        )
        self.assertEqual(patch_decide.call_count, 1)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.decide")
    def test_get_all_flags_with_fallback_empty_local_flags(self, patch_decide, patch_capture):
        patch_decide.return_value = {"featureFlags": {"beta-feature": "variant-1", "beta-feature2": "variant-2"}}
        client = self.client
        client.feature_flags = []
        # beta-feature value overridden by /decide
        self.assertEqual(
            client.get_all_flags("distinct_id"), {"beta-feature": "variant-1", "beta-feature2": "variant-2"}
        )
        self.assertEqual(patch_decide.call_count, 1)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.decide")
    def test_get_all_flags_with_no_fallback(self, patch_decide, patch_capture):
        patch_decide.return_value = {"featureFlags": {"beta-feature": "variant-1", "beta-feature2": "variant-2"}}
        client = self.client
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": False,
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "disabled-feature",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 0,
                        }
                    ]
                },
            },
        ]
        self.assertEqual(client.get_all_flags("distinct_id"), {"beta-feature": True, "disabled-feature": False})
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_decide.call_count, 0)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.decide")
    def test_compute_inactive_flags_locally(self, patch_decide, patch_capture):
        client = self.client
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": False,
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "disabled-feature",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 0,
                        }
                    ]
                },
            },
        ]
        self.assertEqual(client.get_all_flags("distinct_id"), {"beta-feature": True, "disabled-feature": False})
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_decide.call_count, 0)
        self.assertEqual(patch_capture.call_count, 0)

        # Now, after a poll interval, flag 1 is inactive, and flag 2 rollout is set to 100%.
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": False,
                "active": False,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "disabled-feature",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]
        self.assertEqual(client.get_all_flags("distinct_id"), {"beta-feature": False, "disabled-feature": True})
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_decide.call_count, 0)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_load_feature_flags(self, patch_get, patch_poll):
        patch_get.return_value = {
            "flags": [
                {"id": 1, "name": "Beta Feature", "key": "beta-feature", "active": True},
                {"id": 2, "name": "Alpha Feature", "key": "alpha-feature", "active": False},
            ],
            "group_type_mapping": {"0": "company"},
        }
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        with freeze_time("2020-01-01T12:01:00.0000Z"):
            client.load_feature_flags()
        self.assertEqual(len(client.feature_flags), 2)
        self.assertEqual(client.feature_flags[0]["key"], "beta-feature")
        self.assertEqual(client.group_type_mapping, {"0": "company"})
        self.assertEqual(client._last_feature_flag_poll.isoformat(), "2020-01-01T12:01:00+00:00")
        self.assertEqual(patch_poll.call_count, 1)

    def test_load_feature_flags_wrong_key(self):
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        with freeze_time("2020-01-01T12:01:00.0000Z"):
            self.assertRaises(APIError, client.load_feature_flags)

    @mock.patch("posthog.client.decide")
    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple(self, patch_get, patch_decide):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": True,
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            }
        ]
        self.assertTrue(client.feature_enabled("beta-feature", "distinct_id"))
        self.assertEqual(patch_decide.call_count, 0)

    @mock.patch("posthog.client.decide")
    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple_is_false(self, patch_get, patch_decide):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": True,
                "active": True,
                "rollout_percentage": 0,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 0,
                        }
                    ]
                },
            }
        ]
        self.assertFalse(client.feature_enabled("beta-feature", "distinct_id"))
        self.assertEqual(patch_decide.call_count, 0)

    @mock.patch("posthog.client.decide")
    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple_is_true_when_rollout_is_undefined(self, patch_get, patch_decide):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": True,
                "active": True,
                "rollout_percentage": None,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": None,
                        }
                    ]
                },
            }
        ]
        self.assertTrue(client.feature_enabled("beta-feature", "distinct_id"))
        self.assertEqual(patch_decide.call_count, 0)

    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple_with_project_api_key(self, patch_get):
        client = Client(project_api_key=FAKE_TEST_API_KEY, on_error=self.set_fail)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": True,
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            }
        ]
        self.assertTrue(client.feature_enabled("beta-feature", "distinct_id"))

    @mock.patch("posthog.client.decide")
    def test_feature_enabled_request_multi_variate(self, patch_decide):
        patch_decide.return_value = {"featureFlags": {"beta-feature": "variant-1"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": False,
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            }
        ]
        self.assertTrue(client.feature_enabled("beta-feature", "distinct_id"))
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_decide.call_count, 0)

    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple_without_rollout_percentage(self, patch_get):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": True,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                        }
                    ]
                },
            }
        ]
        self.assertTrue(client.feature_enabled("beta-feature", "distinct_id"))

    @mock.patch("posthog.client.decide")
    def test_get_feature_flag(self, patch_decide):
        patch_decide.return_value = {"featureFlags": {"beta-feature": "variant-1"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "is_simple_flag": False,
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ],
                    "multivariate": {
                        "variants": [
                            {"key": "variant-1", "rollout_percentage": 50},
                            {"key": "variant-2", "rollout_percentage": 50},
                        ]
                    },
                },
            }
        ]
        self.assertEqual(client.get_feature_flag("beta-feature", "distinct_id"), "variant-1")
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_decide.call_count, 0)

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.decide")
    def test_feature_enabled_doesnt_exist(self, patch_decide, patch_poll):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = []

        patch_decide.return_value = {"featureFlags": {}}
        self.assertFalse(client.feature_enabled("doesnt-exist", "distinct_id"))

        patch_decide.side_effect = APIError(401, "decide error")
        self.assertTrue(client.feature_enabled("doesnt-exist", "distinct_id", True))

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.decide")
    def test_personal_api_key_doesnt_exist(self, patch_decide, patch_poll):
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = []

        patch_decide.return_value = {"featureFlags": {"feature-flag": True}}

        self.assertTrue(client.feature_enabled("feature-flag", "distinct_id"))

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_load_feature_flags_error(self, patch_get, patch_poll):
        def raise_effect():
            raise Exception("http exception")

        patch_get.return_value.raiseError.side_effect = raise_effect
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = []

        self.assertFalse(client.feature_enabled("doesnt-exist", "distinct_id"))


class TestMatchProperties(unittest.TestCase):
    def property(self, key, value, operator=None):
        result = {"key": key, "value": value}
        if operator is not None:
            result.update({"operator": operator})

        return result

    def test_match_properties_exact(self):
        property_a = self.property(key="key", value="value")

        self.assertTrue(match_property(property_a, {"key": "value"}))

        self.assertFalse(match_property(property_a, {"key": "value2"}))
        self.assertFalse(match_property(property_a, {"key": ""}))
        self.assertFalse(match_property(property_a, {"key": None}))

        with self.assertRaises(InconclusiveMatchError):
            match_property(property_a, {"key2": "value"})
            match_property(property_a, {})

        property_b = self.property(key="key", value="value", operator="exact")
        self.assertTrue(match_property(property_b, {"key": "value"}))

        self.assertFalse(match_property(property_b, {"key": "value2"}))

        property_c = self.property(key="key", value=["value1", "value2", "value3"], operator="exact")
        self.assertTrue(match_property(property_c, {"key": "value1"}))
        self.assertTrue(match_property(property_c, {"key": "value2"}))
        self.assertTrue(match_property(property_c, {"key": "value3"}))

        self.assertFalse(match_property(property_c, {"key": "value4"}))

        with self.assertRaises(InconclusiveMatchError):
            match_property(property_c, {"key2": "value"})

    def test_match_properties_not_in(self):
        property_a = self.property(key="key", value="value", operator="is_not")
        self.assertTrue(match_property(property_a, {"key": "value2"}))
        self.assertTrue(match_property(property_a, {"key": ""}))
        self.assertTrue(match_property(property_a, {"key": None}))

        property_c = self.property(key="key", value=["value1", "value2", "value3"], operator="is_not")
        self.assertTrue(match_property(property_c, {"key": "value4"}))
        self.assertTrue(match_property(property_c, {"key": "value5"}))
        self.assertTrue(match_property(property_c, {"key": "value6"}))
        self.assertTrue(match_property(property_c, {"key": ""}))
        self.assertTrue(match_property(property_c, {"key": None}))

        self.assertFalse(match_property(property_c, {"key": "value2"}))
        self.assertFalse(match_property(property_c, {"key": "value3"}))
        self.assertFalse(match_property(property_c, {"key": "value1"}))

        with self.assertRaises(InconclusiveMatchError):
            match_property(property_a, {"key2": "value"})
            match_property(property_c, {"key2": "value1"})  # overrides don't have 'key'

    def test_match_properties_is_set(self):
        property_a = self.property(key="key", value="is_set", operator="is_set")
        self.assertTrue(match_property(property_a, {"key": "value"}))
        self.assertTrue(match_property(property_a, {"key": "value2"}))
        self.assertTrue(match_property(property_a, {"key": ""}))
        self.assertTrue(match_property(property_a, {"key": None}))

        with self.assertRaises(InconclusiveMatchError):
            match_property(property_a, {"key2": "value"})
            match_property(property_a, {})

    def test_match_properties_icontains(self):
        property_a = self.property(key="key", value="valUe", operator="icontains")
        self.assertTrue(match_property(property_a, {"key": "value"}))
        self.assertTrue(match_property(property_a, {"key": "value2"}))
        self.assertTrue(match_property(property_a, {"key": "value3"}))
        self.assertTrue(match_property(property_a, {"key": "vaLue4"}))
        self.assertTrue(match_property(property_a, {"key": "343tfvalue5"}))

        self.assertFalse(match_property(property_a, {"key": "Alakazam"}))
        self.assertFalse(match_property(property_a, {"key": 123}))

        property_b = self.property(key="key", value="3", operator="icontains")
        self.assertTrue(match_property(property_b, {"key": "3"}))
        self.assertTrue(match_property(property_b, {"key": 323}))
        self.assertTrue(match_property(property_b, {"key": "val3"}))

        self.assertFalse(match_property(property_b, {"key": "three"}))

    def test_match_properties_regex(self):
        property_a = self.property(key="key", value="\.com$", operator="regex")
        self.assertTrue(match_property(property_a, {"key": "value.com"}))
        self.assertTrue(match_property(property_a, {"key": "value2.com"}))

        self.assertFalse(match_property(property_a, {"key": ".com343tfvalue5"}))
        self.assertFalse(match_property(property_a, {"key": "Alakazam"}))
        self.assertFalse(match_property(property_a, {"key": 123}))
        self.assertFalse(match_property(property_a, {"key": "valuecom"}))
        self.assertFalse(match_property(property_a, {"key": "value\com"}))

        property_b = self.property(key="key", value="3", operator="regex")
        self.assertTrue(match_property(property_b, {"key": "3"}))
        self.assertTrue(match_property(property_b, {"key": 323}))
        self.assertTrue(match_property(property_b, {"key": "val3"}))

        self.assertFalse(match_property(property_b, {"key": "three"}))

        # invalid regex
        property_c = self.property(key="key", value="?*", operator="regex")
        self.assertFalse(match_property(property_c, {"key": "value"}))
        self.assertFalse(match_property(property_c, {"key": "value2"}))

        # non string value
        property_d = self.property(key="key", value=4, operator="regex")
        self.assertTrue(match_property(property_d, {"key": "4"}))
        self.assertTrue(match_property(property_d, {"key": 4}))

        self.assertFalse(match_property(property_d, {"key": "value"}))

    def test_match_properties_math_operators(self):
        property_a = self.property(key="key", value=1, operator="gt")
        self.assertTrue(match_property(property_a, {"key": 2}))
        self.assertTrue(match_property(property_a, {"key": 3}))

        self.assertFalse(match_property(property_a, {"key": 0}))
        self.assertFalse(match_property(property_a, {"key": -1}))
        self.assertFalse(match_property(property_a, {"key": "23"}))

        property_b = self.property(key="key", value=1, operator="lt")
        self.assertTrue(match_property(property_b, {"key": 0}))
        self.assertTrue(match_property(property_b, {"key": -1}))
        self.assertTrue(match_property(property_b, {"key": -3}))

        self.assertFalse(match_property(property_b, {"key": 1}))
        self.assertFalse(match_property(property_b, {"key": "1"}))
        self.assertFalse(match_property(property_b, {"key": "3"}))

        property_c = self.property(key="key", value=1, operator="gte")
        self.assertTrue(match_property(property_c, {"key": 1}))
        self.assertTrue(match_property(property_c, {"key": 2}))

        self.assertFalse(match_property(property_c, {"key": 0}))
        self.assertFalse(match_property(property_c, {"key": -1}))
        self.assertFalse(match_property(property_c, {"key": "3"}))

        property_d = self.property(key="key", value="43", operator="lte")
        self.assertTrue(match_property(property_d, {"key": "41"}))
        self.assertTrue(match_property(property_d, {"key": "42"}))
        self.assertTrue(match_property(property_d, {"key": "43"}))

        self.assertFalse(match_property(property_d, {"key": "44"}))
        self.assertFalse(match_property(property_d, {"key": 44}))


class TestCaptureCalls(unittest.TestCase):
    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.decide")
    def test_capture_is_called(self, patch_decide, patch_capture):
        patch_decide.return_value = {"featureFlags": {"decide-flag": "decide-value"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "complex-flag",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            }
        ]

        self.assertTrue(
            client.get_feature_flag(
                "complex-flag", "some-distinct-id", person_properties={"region": "USA", "name": "Aloha"}
            )
        )
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "some-distinct-id",
            "$feature_flag_called",
            {"$feature_flag": "complex-flag", "$feature_flag_response": True},
        )
        patch_capture.reset_mock()

        # called again for same user, shouldn't call capture again
        self.assertTrue(
            client.get_feature_flag(
                "complex-flag", "some-distinct-id", person_properties={"region": "USA", "name": "Aloha"}
            )
        )
        self.assertEqual(patch_capture.call_count, 0)
        patch_capture.reset_mock()

        # called for different user, should call capture again
        self.assertTrue(
            client.get_feature_flag(
                "complex-flag", "some-distinct-id2", person_properties={"region": "USA", "name": "Aloha"}
            )
        )
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "some-distinct-id2",
            "$feature_flag_called",
            {"$feature_flag": "complex-flag", "$feature_flag_response": True},
        )
        patch_capture.reset_mock()

        # called for different flag, falls back to decide, should call capture again
        self.assertEqual(
            client.get_feature_flag(
                "decide-flag", "some-distinct-id2", person_properties={"region": "USA", "name": "Aloha"}
            ),
            "decide-value",
        )
        self.assertEqual(patch_decide.call_count, 1)
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "some-distinct-id2",
            "$feature_flag_called",
            {"$feature_flag": "decide-flag", "$feature_flag_response": "decide-value"},
        )

    @mock.patch("posthog.client.MAX_DICT_SIZE", 100)
    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.decide")
    def test_capture_multiple_users_doesnt_out_of_memory(self, patch_decide, patch_capture):
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "complex-flag",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            }
        ]

        for i in range(1000):
            distinct_id = f"some-distinct-id{i}"
            client.get_feature_flag("complex-flag", distinct_id, person_properties={"region": "USA", "name": "Aloha"})
            patch_capture.assert_called_with(
                distinct_id, "$feature_flag_called", {"$feature_flag": "complex-flag", "$feature_flag_response": True}
            )

            self.assertEqual(len(client.distinct_ids_feature_flags_reported), i % 100 + 1)


class TestConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This ensures no real HTTP POST requests are made
        cls.capture_patch = mock.patch.object(Client, "capture")
        cls.capture_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.capture_patch.stop()

    def set_fail(self, e, batch):
        """Mark the failure handler"""
        print("FAIL", e, batch)
        self.failed = True

    def setUp(self):
        self.failed = False
        self.client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail)

    @mock.patch("posthog.client.get")
    def test_simple_flag_consistency(self, patch_get):
        self.client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "simple-flag",
                "is_simple_flag": True,
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 45}],
                },
            }
        ]

        results = [
            False,
            True,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            True,
            True,
            False,
            False,
            False,
            True,
            True,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            True,
            True,
            False,
            True,
            True,
            False,
            True,
            True,
            True,
            True,
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            True,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            True,
            True,
            True,
            False,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            True,
            True,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            True,
            True,
            True,
            False,
            True,
            True,
            True,
            False,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            True,
            True,
            False,
            True,
            True,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            True,
            True,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            True,
            True,
            False,
            True,
            False,
            False,
            True,
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            True,
            True,
        ]

        for i in range(1000):
            distinctID = f"distinct_id_{i}"

            feature_flag_match = self.client.feature_enabled("simple-flag", distinctID)

            if results[i]:
                self.assertTrue(feature_flag_match)
            else:
                self.assertFalse(feature_flag_match)

    @mock.patch("posthog.client.get")
    def test_multivariate_flag_consistency(self, patch_get):
        self.client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "multivariate-flag",
                "is_simple_flag": False,
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 55}],
                    "multivariate": {
                        "variants": [
                            {"key": "first-variant", "name": "First Variant", "rollout_percentage": 50},
                            {"key": "second-variant", "name": "Second Variant", "rollout_percentage": 20},
                            {"key": "third-variant", "name": "Third Variant", "rollout_percentage": 20},
                            {"key": "fourth-variant", "name": "Fourth Variant", "rollout_percentage": 5},
                            {"key": "fifth-variant", "name": "Fifth Variant", "rollout_percentage": 5},
                        ],
                    },
                },
            }
        ]

        results = [
            "second-variant",
            "second-variant",
            "first-variant",
            False,
            False,
            "second-variant",
            "first-variant",
            False,
            False,
            False,
            "first-variant",
            "third-variant",
            False,
            "first-variant",
            "second-variant",
            "first-variant",
            False,
            False,
            "fourth-variant",
            "first-variant",
            False,
            "third-variant",
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "third-variant",
            False,
            "third-variant",
            "second-variant",
            "first-variant",
            False,
            "third-variant",
            False,
            False,
            "first-variant",
            "second-variant",
            False,
            "first-variant",
            "first-variant",
            "second-variant",
            False,
            "first-variant",
            False,
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            "second-variant",
            "first-variant",
            False,
            "second-variant",
            "second-variant",
            "third-variant",
            "second-variant",
            "first-variant",
            False,
            "first-variant",
            "second-variant",
            "fourth-variant",
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            "second-variant",
            False,
            "third-variant",
            False,
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            "fifth-variant",
            False,
            "second-variant",
            "first-variant",
            "second-variant",
            False,
            "third-variant",
            "third-variant",
            False,
            False,
            False,
            False,
            "third-variant",
            False,
            False,
            "first-variant",
            "first-variant",
            False,
            "third-variant",
            "third-variant",
            False,
            "third-variant",
            "second-variant",
            "third-variant",
            False,
            False,
            "second-variant",
            "first-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "second-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "second-variant",
            False,
            "second-variant",
            "first-variant",
            "second-variant",
            "first-variant",
            False,
            "second-variant",
            "second-variant",
            False,
            "first-variant",
            False,
            False,
            False,
            "third-variant",
            "first-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            "third-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            "fifth-variant",
            "second-variant",
            False,
            "second-variant",
            False,
            "first-variant",
            "third-variant",
            "first-variant",
            "fifth-variant",
            "third-variant",
            False,
            False,
            "fourth-variant",
            False,
            False,
            False,
            False,
            "third-variant",
            False,
            False,
            "third-variant",
            False,
            "first-variant",
            "second-variant",
            "second-variant",
            "second-variant",
            False,
            "first-variant",
            "third-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            "second-variant",
            False,
            False,
            False,
            "second-variant",
            False,
            False,
            "first-variant",
            False,
            "first-variant",
            False,
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "third-variant",
            "first-variant",
            "third-variant",
            "first-variant",
            "first-variant",
            "second-variant",
            "third-variant",
            "third-variant",
            False,
            "second-variant",
            "first-variant",
            False,
            "second-variant",
            "first-variant",
            False,
            "first-variant",
            False,
            False,
            "first-variant",
            "fifth-variant",
            "first-variant",
            False,
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            "second-variant",
            False,
            "second-variant",
            "third-variant",
            "third-variant",
            False,
            "first-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            False,
            "third-variant",
            "first-variant",
            False,
            "third-variant",
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            "second-variant",
            "second-variant",
            "first-variant",
            False,
            False,
            False,
            "second-variant",
            False,
            False,
            "first-variant",
            "first-variant",
            False,
            "third-variant",
            False,
            "first-variant",
            False,
            "third-variant",
            False,
            "third-variant",
            "second-variant",
            "first-variant",
            False,
            False,
            "first-variant",
            "third-variant",
            "first-variant",
            "second-variant",
            "fifth-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            False,
            "third-variant",
            False,
            "second-variant",
            "first-variant",
            False,
            False,
            False,
            False,
            "third-variant",
            False,
            False,
            "third-variant",
            False,
            False,
            "first-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            "fourth-variant",
            "fourth-variant",
            "third-variant",
            "second-variant",
            "first-variant",
            "third-variant",
            "fifth-variant",
            False,
            "first-variant",
            "fifth-variant",
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            "second-variant",
            "fifth-variant",
            "second-variant",
            "first-variant",
            "first-variant",
            "second-variant",
            False,
            False,
            "third-variant",
            False,
            "second-variant",
            "fifth-variant",
            False,
            "third-variant",
            "first-variant",
            False,
            False,
            "fourth-variant",
            False,
            False,
            "second-variant",
            False,
            False,
            "first-variant",
            "fourth-variant",
            "first-variant",
            "second-variant",
            False,
            False,
            False,
            "first-variant",
            "third-variant",
            "third-variant",
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            False,
            "first-variant",
            "third-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            "second-variant",
            "second-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            "fifth-variant",
            "first-variant",
            False,
            False,
            False,
            "second-variant",
            "third-variant",
            "first-variant",
            "fourth-variant",
            "first-variant",
            "third-variant",
            False,
            "first-variant",
            "first-variant",
            False,
            "third-variant",
            "first-variant",
            "first-variant",
            "third-variant",
            False,
            "fourth-variant",
            "fifth-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            "first-variant",
            "second-variant",
            "first-variant",
            False,
            "first-variant",
            "second-variant",
            "first-variant",
            False,
            "first-variant",
            "second-variant",
            False,
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            False,
            "first-variant",
            False,
            "first-variant",
            False,
            False,
            False,
            "third-variant",
            "third-variant",
            "first-variant",
            False,
            False,
            "second-variant",
            "third-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            "second-variant",
            "first-variant",
            False,
            "first-variant",
            "third-variant",
            False,
            "first-variant",
            False,
            False,
            False,
            "first-variant",
            "third-variant",
            "third-variant",
            False,
            False,
            False,
            False,
            "third-variant",
            "fourth-variant",
            "fourth-variant",
            "first-variant",
            "second-variant",
            False,
            "first-variant",
            False,
            "second-variant",
            "first-variant",
            "third-variant",
            False,
            "third-variant",
            False,
            "first-variant",
            "first-variant",
            "third-variant",
            False,
            False,
            False,
            "fourth-variant",
            "second-variant",
            "first-variant",
            False,
            False,
            "first-variant",
            "fourth-variant",
            False,
            "first-variant",
            "third-variant",
            "first-variant",
            False,
            False,
            "third-variant",
            False,
            "first-variant",
            False,
            "first-variant",
            "first-variant",
            "third-variant",
            "second-variant",
            "fourth-variant",
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            "second-variant",
            "first-variant",
            "second-variant",
            False,
            "first-variant",
            False,
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            "first-variant",
            "second-variant",
            "third-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            "third-variant",
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            "third-variant",
            "first-variant",
            "first-variant",
            "second-variant",
            "first-variant",
            "fifth-variant",
            "fourth-variant",
            "first-variant",
            "second-variant",
            False,
            "fourth-variant",
            False,
            False,
            False,
            "fourth-variant",
            False,
            False,
            "third-variant",
            False,
            False,
            False,
            "first-variant",
            "third-variant",
            "third-variant",
            "second-variant",
            "first-variant",
            "second-variant",
            "first-variant",
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            False,
            "second-variant",
            False,
            False,
            "first-variant",
            False,
            "second-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "third-variant",
            "second-variant",
            False,
            False,
            "fifth-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            False,
            "first-variant",
            "second-variant",
            "third-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            False,
            "third-variant",
            "first-variant",
            False,
            False,
            False,
            False,
            "fourth-variant",
            "first-variant",
            False,
            False,
            False,
            "third-variant",
            False,
            False,
            "second-variant",
            "first-variant",
            False,
            False,
            "second-variant",
            "third-variant",
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            "first-variant",
            False,
            False,
            "second-variant",
            "third-variant",
            "second-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            "first-variant",
            False,
            "second-variant",
            False,
            False,
            False,
            False,
            "first-variant",
            False,
            "third-variant",
            False,
            "first-variant",
            False,
            False,
            "second-variant",
            "third-variant",
            "second-variant",
            "fourth-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            False,
            "second-variant",
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            False,
            "second-variant",
            False,
            False,
            False,
            False,
            "second-variant",
            False,
            "first-variant",
            False,
            "third-variant",
            False,
            False,
            "first-variant",
            "third-variant",
            False,
            "third-variant",
            False,
            False,
            "second-variant",
            False,
            "first-variant",
            "second-variant",
            "first-variant",
            False,
            False,
            False,
            False,
            False,
            "second-variant",
            False,
            False,
            "first-variant",
            "third-variant",
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            "second-variant",
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            "fifth-variant",
            False,
            False,
            False,
            "first-variant",
            False,
            "third-variant",
            False,
            False,
            "second-variant",
            False,
            False,
            False,
            False,
            False,
            "fourth-variant",
            "second-variant",
            "first-variant",
            "second-variant",
            False,
            "second-variant",
            False,
            "second-variant",
            False,
            "first-variant",
            False,
            "first-variant",
            "first-variant",
            False,
            "second-variant",
            False,
            "first-variant",
            False,
            "fifth-variant",
            False,
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            "first-variant",
            False,
            "first-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            "first-variant",
            False,
            False,
            "fifth-variant",
            False,
            False,
            "third-variant",
            False,
            "third-variant",
            "first-variant",
            "first-variant",
            "third-variant",
            "third-variant",
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            "second-variant",
            "first-variant",
            "second-variant",
            "first-variant",
            False,
            "fifth-variant",
            "first-variant",
            False,
            False,
            "fourth-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            "fourth-variant",
            "first-variant",
            False,
            "second-variant",
            "third-variant",
            "third-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            "third-variant",
            "third-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            "first-variant",
            False,
            "second-variant",
            False,
            False,
            "second-variant",
            False,
            "third-variant",
            "first-variant",
            "second-variant",
            "fifth-variant",
            "first-variant",
            "first-variant",
            False,
            "first-variant",
            "fifth-variant",
            False,
            False,
            False,
            "third-variant",
            "first-variant",
            "first-variant",
            "second-variant",
            "fourth-variant",
            "first-variant",
            "second-variant",
            "first-variant",
            False,
            False,
            False,
            "second-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            False,
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            False,
            "third-variant",
            False,
            "first-variant",
            False,
            "third-variant",
            "third-variant",
            "first-variant",
            "first-variant",
            False,
            "second-variant",
            False,
            "second-variant",
            "first-variant",
            False,
            False,
            False,
            "second-variant",
            False,
            "third-variant",
            False,
            "first-variant",
            "fifth-variant",
            "first-variant",
            "first-variant",
            False,
            False,
            "first-variant",
            False,
            False,
            False,
            "first-variant",
            "fourth-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "fifth-variant",
            False,
            False,
            False,
            "second-variant",
            False,
            False,
            False,
            "first-variant",
            "first-variant",
            False,
            False,
            "first-variant",
            "first-variant",
            "second-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            "third-variant",
            "first-variant",
            False,
            "second-variant",
            False,
            False,
            "third-variant",
            "second-variant",
            "third-variant",
            False,
            "first-variant",
            "third-variant",
            "second-variant",
            "first-variant",
            "third-variant",
            False,
            False,
            "first-variant",
            "first-variant",
            False,
            False,
            False,
            "first-variant",
            "third-variant",
            "second-variant",
            "first-variant",
            "first-variant",
            "first-variant",
            False,
            "third-variant",
            "second-variant",
            "third-variant",
            False,
            False,
            "third-variant",
            "first-variant",
            False,
            "first-variant",
        ]

        for i in range(1000):
            distinctID = f"distinct_id_{i}"
            feature_flag_match = self.client.get_feature_flag("multivariate-flag", distinctID)

            if results[i]:
                self.assertEqual(feature_flag_match, results[i])
            else:
                self.assertFalse(feature_flag_match)
