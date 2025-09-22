import datetime
import unittest

import mock
from dateutil import parser, tz
from freezegun import freeze_time

from posthog.client import Client
from posthog.feature_flags import (
    InconclusiveMatchError,
    match_property,
    relative_date_parse_for_feature_flag_matching,
)
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
        print("FAIL", e, batch)  # noqa: T201
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

    def test_case_insensitive_matching(self):
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
                                    "key": "location",
                                    "operator": "exact",
                                    "value": ["Straße"],
                                    "type": "person",
                                }
                            ],
                            "rollout_percentage": 100,
                        },
                        {
                            "properties": [
                                {
                                    "key": "star",
                                    "operator": "exact",
                                    "value": ["ſun"],
                                    "type": "person",
                                }
                            ],
                            "rollout_percentage": 100,
                        },
                    ],
                },
            }
        ]

        self.assertTrue(
            self.client.get_feature_flag(
                "person-flag",
                "some-distinct-id",
                person_properties={"location": "straße"},
            )
        )

        self.assertTrue(
            self.client.get_feature_flag(
                "person-flag",
                "some-distinct-id",
                person_properties={"location": "strasse"},
            )
        )

        self.assertTrue(
            self.client.get_feature_flag(
                "person-flag", "some-distinct-id", person_properties={"star": "ſun"}
            )
        )

        self.assertTrue(
            self.client.get_feature_flag(
                "person-flag", "some-distinct-id", person_properties={"star": "sun"}
            )
        )

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_flag_group_properties(self, patch_get, patch_flags):
        self.client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "group-flag",
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
                "group-flag",
                "some-distinct-id",
                group_properties={"company": {"name": "Project Name 1"}},
            )
        )

        self.assertFalse(
            self.client.get_feature_flag(
                "group-flag",
                "some-distinct-2",
                group_properties={"company": {"name": "Project Name 2"}},
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
        self.assertEqual(patch_flags.call_count, 0)

        # Now group type mappings are gone, so fall back to /flags/
        patch_flags.return_value = {
            "featureFlags": {"group-flag": "decide-fallback-value"}
        }

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

        self.assertEqual(patch_flags.call_count, 1)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_flag_with_complex_definition(self, patch_get, patch_flags):
        patch_flags.return_value = {
            "featureFlags": {"complex-flag": "decide-fallback-value"}
        }
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "complex-flag",
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
                "complex-flag",
                "some-distinct-id",
                person_properties={"region": "USA", "name": "Aloha"},
            )
        )
        self.assertEqual(patch_flags.call_count, 0)

        # this distinctIDs hash is < rollout %
        self.assertTrue(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id_within_rollout?",
                person_properties={"region": "USA", "email": "a@b.com"},
            )
        )
        self.assertEqual(patch_flags.call_count, 0)

        # will fall back on `/flags`, as all properties present for second group, but that group resolves to false
        self.assertEqual(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id_outside_rollout?",
                person_properties={"region": "USA", "email": "a@b.com"},
            ),
            "decide-fallback-value",
        )
        self.assertEqual(patch_flags.call_count, 1)

        patch_flags.reset_mock()

        # same as above
        self.assertEqual(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id",
                person_properties={"doesnt_matter": "1"},
            ),
            "decide-fallback-value",
        )
        self.assertEqual(patch_flags.call_count, 1)

        patch_flags.reset_mock()

        # this one will need to fall back
        self.assertEqual(
            client.get_feature_flag(
                "complex-flag", "some-distinct-id", person_properties={"region": "USA"}
            ),
            "decide-fallback-value",
        )
        self.assertEqual(patch_flags.call_count, 1)

        patch_flags.reset_mock()

        # won't need to fall back when all values are present
        self.assertFalse(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id_outside_rollout?",
                person_properties={
                    "region": "USA",
                    "email": "a@b.com",
                    "name": "X",
                    "doesnt_matter": "1",
                },
            )
        )
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_flags_fallback_to_flags(self, patch_get, patch_flags):
        patch_flags.return_value = {
            "featureFlags": {"beta-feature": "alakazam", "beta-feature2": "alakazam2"}
        }
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "id",
                                    "value": 98,
                                    "operator": None,
                                    "type": "cohort",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "beta-feature2",
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
        self.assertEqual(patch_flags.call_count, 1)

        # beta-feature2 fallbacks to decide because region property not given with call
        feature_flag_match = client.get_feature_flag(
            "beta-feature2", "some-distinct-id"
        )

        self.assertEqual(feature_flag_match, "alakazam2")
        self.assertEqual(patch_flags.call_count, 2)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_flags_dont_fallback_to_flags_when_only_local_evaluation_is_true(
        self, patch_get, patch_flags
    ):
        patch_flags.return_value = {
            "featureFlags": {"beta-feature": "alakazam", "beta-feature2": "alakazam2"}
        }
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "id",
                                    "value": 98,
                                    "operator": None,
                                    "type": "cohort",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "beta-feature2",
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

        # beta-feature should fallback to decide because property type is unknown,
        # but doesn't because only_evaluate_locally is true
        feature_flag_match = client.get_feature_flag(
            "beta-feature", "some-distinct-id", only_evaluate_locally=True
        )

        self.assertEqual(feature_flag_match, None)
        self.assertEqual(patch_flags.call_count, 0)

        feature_flag_match = client.feature_enabled(
            "beta-feature", "some-distinct-id", only_evaluate_locally=True
        )

        self.assertEqual(feature_flag_match, None)
        self.assertEqual(patch_flags.call_count, 0)

        # beta-feature2 should fallback to decide because region property not given with call
        # but doesn't because only_evaluate_locally is true
        feature_flag_match = client.get_feature_flag(
            "beta-feature2", "some-distinct-id", only_evaluate_locally=True
        )
        self.assertEqual(feature_flag_match, None)

        feature_flag_match = client.feature_enabled(
            "beta-feature2", "some-distinct-id", only_evaluate_locally=True
        )
        self.assertEqual(feature_flag_match, None)

        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_flag_never_returns_undefined_during_regular_evaluation(
        self, patch_get, patch_flags
    ):
        patch_flags.return_value = {"featureFlags": {}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertFalse(client.get_feature_flag("beta-feature", "some-distinct-id"))
        self.assertFalse(client.feature_enabled("beta-feature", "some-distinct-id"))

        # beta-feature2 falls back to decide, and whatever decide returns is the value
        self.assertFalse(client.get_feature_flag("beta-feature2", "some-distinct-id"))
        self.assertEqual(patch_flags.call_count, 1)

        self.assertFalse(client.feature_enabled("beta-feature2", "some-distinct-id"))
        self.assertEqual(patch_flags.call_count, 2)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_flag_return_none_when_decide_errors_out(
        self, patch_get, patch_flags
    ):
        patch_flags.side_effect = APIError(400, "Decide error")
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = []

        # beta-feature2 falls back to decide, which on error returns None
        self.assertIsNone(client.get_feature_flag("beta-feature2", "some-distinct-id"))
        self.assertEqual(patch_flags.call_count, 1)

        self.assertIsNone(client.feature_enabled("beta-feature2", "some-distinct-id"))
        self.assertEqual(patch_flags.call_count, 2)

    @mock.patch("posthog.client.flags")
    def test_experience_continuity_flag_not_evaluated_locally(self, patch_flags):
        patch_flags.return_value = {
            "featureFlags": {"beta-feature": "decide-fallback-value"}
        }
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertEqual(
            client.get_feature_flag("beta-feature", "distinct_id"),
            "decide-fallback-value",
        )
        self.assertEqual(patch_flags.call_count, 1)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_get_all_flags_with_fallback(self, patch_flags, patch_capture):
        patch_flags.return_value = {
            "featureFlags": {
                "beta-feature": "variant-1",
                "beta-feature2": "variant-2",
                "disabled-feature": False,
            }
        }  # decide should return the same flags
        client = self.client
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        # beta-feature value overridden by /flags
        self.assertEqual(
            client.get_all_flags("distinct_id"),
            {
                "beta-feature": "variant-1",
                "beta-feature2": "variant-2",
                "disabled-feature": False,
            },
        )
        self.assertEqual(patch_flags.call_count, 1)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_get_all_flags_and_payloads_with_fallback(self, patch_flags, patch_capture):
        patch_flags.return_value = {
            "featureFlags": {"beta-feature": "variant-1", "beta-feature2": "variant-2"},
            "featureFlagPayloads": {"beta-feature": 100, "beta-feature2": 300},
        }
        client = self.client
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ],
                    "payloads": {
                        "true": "some-payload",
                    },
                },
            },
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "disabled-feature",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 0,
                        }
                    ],
                    "payloads": {
                        "true": "another-payload",
                    },
                },
            },
            {
                "id": 3,
                "name": "Beta Feature",
                "key": "beta-feature2",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [{"key": "country", "value": "US"}],
                            "rollout_percentage": 0,
                        }
                    ],
                    "payloads": {
                        "true": "payload-3",
                    },
                },
            },
        ]
        # beta-feature value overridden by /flags
        self.assertEqual(
            client.get_all_flags_and_payloads("distinct_id")["featureFlagPayloads"],
            {
                "beta-feature": 100,
                "beta-feature2": 300,
            },
        )
        self.assertEqual(patch_flags.call_count, 1)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_get_all_flags_with_fallback_empty_local_flags(
        self, patch_flags, patch_capture
    ):
        patch_flags.return_value = {
            "featureFlags": {"beta-feature": "variant-1", "beta-feature2": "variant-2"}
        }
        client = self.client
        client.feature_flags = []
        # beta-feature value overridden by /flags
        self.assertEqual(
            client.get_all_flags("distinct_id"),
            {"beta-feature": "variant-1", "beta-feature2": "variant-2"},
        )
        self.assertEqual(patch_flags.call_count, 1)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_get_all_flags_and_payloads_with_fallback_empty_local_flags(
        self, patch_flags, patch_capture
    ):
        patch_flags.return_value = {
            "featureFlags": {"beta-feature": "variant-1", "beta-feature2": "variant-2"},
            "featureFlagPayloads": {"beta-feature": 100, "beta-feature2": 300},
        }
        client = self.client
        client.feature_flags = []
        # beta-feature value overridden by /flags
        self.assertEqual(
            client.get_all_flags_and_payloads("distinct_id")["featureFlagPayloads"],
            {"beta-feature": 100, "beta-feature2": 300},
        )
        self.assertEqual(patch_flags.call_count, 1)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_get_all_flags_with_no_fallback(self, patch_flags, patch_capture):
        patch_flags.return_value = {
            "featureFlags": {"beta-feature": "variant-1", "beta-feature2": "variant-2"}
        }
        client = self.client
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertEqual(
            client.get_all_flags("distinct_id"),
            {"beta-feature": True, "disabled-feature": False},
        )
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_get_all_flags_and_payloads_with_no_fallback(
        self, patch_flags, patch_capture
    ):
        client = self.client
        basic_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "beta-feature",
            "active": True,
            "rollout_percentage": 100,
            "filters": {
                "groups": [
                    {
                        "properties": [],
                        "rollout_percentage": 100,
                    }
                ],
                "payloads": {
                    "true": "new",
                },
            },
        }
        disabled_flag = {
            "id": 2,
            "name": "Beta Feature",
            "key": "disabled-feature",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "properties": [],
                        "rollout_percentage": 0,
                    }
                ],
                "payloads": {
                    "true": "some-payload",
                },
            },
        }
        client.feature_flags = [
            basic_flag,
            disabled_flag,
        ]
        self.assertEqual(
            client.get_all_flags_and_payloads("distinct_id")["featureFlagPayloads"],
            {"beta-feature": "new"},
        )
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_get_all_flags_with_fallback_but_only_local_evaluation_set(
        self, patch_flags, patch_capture
    ):
        patch_flags.return_value = {
            "featureFlags": {"beta-feature": "variant-1", "beta-feature2": "variant-2"}
        }
        client = self.client
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        # beta-feature2 has no value
        self.assertEqual(
            client.get_all_flags("distinct_id", only_evaluate_locally=True),
            {"beta-feature": True, "disabled-feature": False},
        )
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_get_all_flags_and_payloads_with_fallback_but_only_local_evaluation_set(
        self, patch_flags, patch_capture
    ):
        patch_flags.return_value = {
            "featureFlags": {"beta-feature": "variant-1", "beta-feature2": "variant-2"},
            "featureFlagPayloads": {"beta-feature": 100, "beta-feature2": 300},
        }
        client = self.client
        flag_1 = {
            "id": 1,
            "name": "Beta Feature",
            "key": "beta-feature",
            "active": True,
            "rollout_percentage": 100,
            "filters": {
                "groups": [
                    {
                        "properties": [],
                        "rollout_percentage": 100,
                    }
                ],
                "payloads": {
                    "true": "some-payload",
                },
            },
        }
        flag_2 = {
            "id": 2,
            "name": "Beta Feature",
            "key": "disabled-feature",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "properties": [],
                        "rollout_percentage": 0,
                    }
                ],
                "payloads": {
                    "true": "another-payload",
                },
            },
        }
        flag_3 = {
            "id": 3,
            "name": "Beta Feature",
            "key": "beta-feature2",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "properties": [{"key": "country", "value": "US"}],
                        "rollout_percentage": 0,
                    }
                ],
                "payloads": {
                    "true": "payload-3",
                },
            },
        }
        client.feature_flags = [
            flag_1,
            flag_2,
            flag_3,
        ]
        # beta-feature2 has no value
        self.assertEqual(
            client.get_all_flags_and_payloads(
                "distinct_id", only_evaluate_locally=True
            )["featureFlagPayloads"],
            {"beta-feature": "some-payload"},
        )
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_compute_inactive_flags_locally(self, patch_flags, patch_capture):
        client = self.client
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertEqual(
            client.get_all_flags("distinct_id"),
            {"beta-feature": True, "disabled-feature": False},
        )
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_capture.call_count, 0)

        # Now, after a poll interval, flag 1 is inactive, and flag 2 rollout is set to 100%.
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertEqual(
            client.get_all_flags("distinct_id"),
            {"beta-feature": False, "disabled-feature": True},
        )
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_capture.call_count, 0)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_flags_local_evaluation_None_values(self, patch_get, patch_flags):
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                id: 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "variant": None,
                            "properties": [
                                {
                                    "key": "latestBuildVersion",
                                    "type": "person",
                                    "value": ".+",
                                    "operator": "regex",
                                },
                                {
                                    "key": "latestBuildVersionMajor",
                                    "type": "person",
                                    "value": "23",
                                    "operator": "gt",
                                },
                                {
                                    "key": "latestBuildVersionMinor",
                                    "type": "person",
                                    "value": "31",
                                    "operator": "gt",
                                },
                                {
                                    "key": "latestBuildVersionPatch",
                                    "type": "person",
                                    "value": "0",
                                    "operator": "gt",
                                },
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]

        feature_flag_match = client.get_feature_flag(
            "beta-feature",
            "some-distinct-id",
            person_properties={
                "latestBuildVersion": None,
                "latestBuildVersionMajor": None,
                "latestBuildVersionMinor": None,
                "latestBuildVersionPatch": None,
            },
        )

        self.assertEqual(feature_flag_match, False)
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_get.call_count, 0)

        feature_flag_match = client.get_feature_flag(
            "beta-feature",
            "some-distinct-id",
            person_properties={
                "latestBuildVersion": "24.32.1",
                "latestBuildVersionMajor": "24",
                "latestBuildVersionMinor": "32",
                "latestBuildVersionPatch": "1",
            },
        )

        self.assertEqual(feature_flag_match, True)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_flags_local_evaluation_for_cohorts(self, patch_get, patch_flags):
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "beta-feature",
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
                                    "key": "id",
                                    "value": 98,
                                    "operator": None,
                                    "type": "cohort",
                                },
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]
        client.cohorts = {
            "98": {
                "type": "OR",
                "values": [
                    {"key": "id", "value": 1, "type": "cohort"},
                    {
                        "key": "nation",
                        "operator": "exact",
                        "value": ["UK"],
                        "type": "person",
                    },
                ],
            },
            "1": {
                "type": "AND",
                "values": [
                    {
                        "key": "other",
                        "operator": "exact",
                        "value": ["thing"],
                        "type": "person",
                    }
                ],
            },
        }

        feature_flag_match = client.get_feature_flag(
            "beta-feature", "some-distinct-id", person_properties={"region": "UK"}
        )

        self.assertEqual(feature_flag_match, False)
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_get.call_count, 0)

        feature_flag_match = client.get_feature_flag(
            "beta-feature",
            "some-distinct-id",
            person_properties={"region": "USA", "nation": "UK"},
        )
        # even though 'other' property is not present, the cohort should still match since it's an OR condition
        self.assertEqual(feature_flag_match, True)
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_get.call_count, 0)

        feature_flag_match = client.get_feature_flag(
            "beta-feature",
            "some-distinct-id",
            person_properties={"region": "USA", "other": "thing"},
        )
        self.assertEqual(feature_flag_match, True)
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_get.call_count, 0)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_flags_local_evaluation_for_negated_cohorts(
        self, patch_get, patch_flags
    ):
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 2,
                "name": "Beta Feature",
                "key": "beta-feature",
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
                                    "key": "id",
                                    "value": 98,
                                    "operator": None,
                                    "type": "cohort",
                                },
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]
        client.cohorts = {
            "98": {
                "type": "OR",
                "values": [
                    {"key": "id", "value": 1, "type": "cohort"},
                    {
                        "key": "nation",
                        "operator": "exact",
                        "value": ["UK"],
                        "type": "person",
                    },
                ],
            },
            "1": {
                "type": "AND",
                "values": [
                    {
                        "key": "other",
                        "operator": "exact",
                        "value": ["thing"],
                        "type": "person",
                        "negation": True,
                    }
                ],
            },
        }

        feature_flag_match = client.get_feature_flag(
            "beta-feature", "some-distinct-id", person_properties={"region": "UK"}
        )

        self.assertEqual(feature_flag_match, False)
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_get.call_count, 0)

        feature_flag_match = client.get_feature_flag(
            "beta-feature",
            "some-distinct-id",
            person_properties={"region": "USA", "nation": "UK"},
        )
        # even though 'other' property is not present, the cohort should still match since it's an OR condition
        self.assertEqual(feature_flag_match, True)
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_get.call_count, 0)

        feature_flag_match = client.get_feature_flag(
            "beta-feature",
            "some-distinct-id",
            person_properties={"region": "USA", "other": "thing"},
        )
        # since 'other' is negated, we return False. Since 'nation' is not present, we can't tell whether the flag should be true or false, so go to decide
        self.assertEqual(patch_flags.call_count, 1)
        self.assertEqual(patch_get.call_count, 0)

        patch_flags.reset_mock()

        feature_flag_match = client.get_feature_flag(
            "beta-feature",
            "some-distinct-id",
            person_properties={"region": "USA", "other": "thing2"},
        )
        self.assertEqual(feature_flag_match, True)
        self.assertEqual(patch_flags.call_count, 0)
        self.assertEqual(patch_get.call_count, 0)

    @mock.patch("posthog.feature_flags.log")
    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_flags_with_flag_dependencies(
        self, patch_get, patch_flags, mock_log
    ):
        # Mock remote flags call to return empty for this flag (fallback returns None)
        patch_flags.return_value = {"featureFlags": {}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Flag with Dependencies",
                "key": "flag-with-dependencies",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "beta-feature",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    "dependency_chain": ["beta-feature"],
                                },
                                {
                                    "key": "email",
                                    "operator": "icontains",
                                    "value": "@example.com",
                                    "type": "person",
                                },
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            }
        ]

        # Test that flag evaluation handles flag dependencies properly
        # The flag has a dependency on "beta-feature" which doesn't exist locally
        # Since the dependency doesn't exist, local evaluation should fail and fall back to remote
        # Remote returns empty result, so final result is None
        feature_flag_match = client.get_feature_flag(
            "flag-with-dependencies",
            "test-user",
            person_properties={"email": "test@example.com"},
        )
        self.assertIsNone(feature_flag_match)
        self.assertEqual(patch_flags.call_count, 1)
        self.assertEqual(patch_get.call_count, 0)

        # Test with email that doesn't match (should also fall back to remote due to missing dependency)
        feature_flag_match = client.get_feature_flag(
            "flag-with-dependencies",
            "test-user-2",
            person_properties={"email": "test@other.com"},
        )
        self.assertIsNone(feature_flag_match)
        self.assertEqual(patch_flags.call_count, 2)  # Called twice now
        self.assertEqual(patch_get.call_count, 0)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_flag_dependencies_simple_chain(self, patch_get, patch_flags):
        """Test basic flag dependency: flag-b depends on flag-a"""
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Flag A",
                "key": "flag-a",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "operator": "icontains",
                                    "value": "@example.com",
                                    "type": "person",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 2,
                "name": "Flag B",
                "key": "flag-b",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "flag-a",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    "dependency_chain": ["flag-a"],
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]

        # Test when dependency is satisfied
        result = client.get_feature_flag(
            "flag-b",
            "test-user",
            person_properties={"email": "test@example.com"},
        )
        self.assertEqual(result, True)

        # Test when dependency is not satisfied
        result = client.get_feature_flag(
            "flag-b",
            "test-user-2",
            person_properties={"email": "test@other.com"},
        )
        self.assertEqual(result, False)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_flag_dependencies_circular_dependency(self, patch_get, patch_flags):
        """Test circular dependency handling: flag-a depends on flag-b, flag-b depends on flag-a"""
        # Mock remote flags call to return empty for these flags (fallback returns None)
        patch_flags.return_value = {"featureFlags": {}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Flag A",
                "key": "flag-a",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "flag-b",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    "dependency_chain": [],  # Empty chain indicates circular dependency
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 2,
                "name": "Flag B",
                "key": "flag-b",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "flag-a",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    "dependency_chain": [],  # Empty chain indicates circular dependency
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]

        # Both flags should fall back to remote evaluation due to circular dependency
        # Since we're not mocking the remote call, both should return None
        result_a = client.get_feature_flag("flag-a", "test-user")
        self.assertIsNone(result_a)

        result_b = client.get_feature_flag("flag-b", "test-user")
        self.assertIsNone(result_b)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_flag_dependencies_missing_flag(self, patch_get, patch_flags):
        """Test handling of missing flag dependency"""
        # Mock remote flags call to return empty for this flag (fallback returns None)
        patch_flags.return_value = {"featureFlags": {}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Flag A",
                "key": "flag-a",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "non-existent-flag",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    "dependency_chain": ["non-existent-flag"],
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            }
        ]

        # Should fall back to remote evaluation because dependency doesn't exist
        # Since we're not mocking the remote call, should return None
        result = client.get_feature_flag("flag-a", "test-user")
        self.assertIsNone(result)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_flag_dependencies_complex_chain(self, patch_get, patch_flags):
        """Test complex dependency chain: flag-d -> flag-c -> [flag-a, flag-b]"""
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Flag A",
                "key": "flag-a",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 2,
                "name": "Flag B",
                "key": "flag-b",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 3,
                "name": "Flag C",
                "key": "flag-c",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "flag-a",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    "dependency_chain": ["flag-a"],
                                },
                                {
                                    "key": "flag-b",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    "dependency_chain": ["flag-b"],
                                },
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 4,
                "name": "Flag D",
                "key": "flag-d",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "flag-c",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    "dependency_chain": ["flag-a", "flag-b", "flag-c"],
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]

        # All dependencies satisfied - should return True
        result = client.get_feature_flag("flag-d", "test-user")
        self.assertEqual(result, True)

        # Make flag-a inactive - should break the chain
        client.feature_flags[0]["active"] = False
        result = client.get_feature_flag("flag-d", "test-user")
        self.assertEqual(result, False)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_flag_dependencies_mixed_conditions(self, patch_get, patch_flags):
        """Test flag dependency mixed with other property conditions"""
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Base Flag",
                "key": "base-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 2,
                "name": "Mixed Flag",
                "key": "mixed-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "base-flag",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    "dependency_chain": ["base-flag"],
                                },
                                {
                                    "key": "email",
                                    "operator": "icontains",
                                    "value": "@example.com",
                                    "type": "person",
                                },
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]

        # Both flag dependency and email condition satisfied
        result = client.get_feature_flag(
            "mixed-flag",
            "test-user",
            person_properties={"email": "test@example.com"},
        )
        self.assertEqual(result, True)

        # Flag dependency satisfied but email condition not satisfied
        result = client.get_feature_flag(
            "mixed-flag",
            "test-user-2",
            person_properties={"email": "test@other.com"},
        )
        self.assertEqual(result, False)

        # Email condition satisfied but flag dependency not satisfied
        client.feature_flags[0]["active"] = False
        result = client.get_feature_flag(
            "mixed-flag",
            "test-user-3",
            person_properties={"email": "test@example.com"},
        )
        self.assertEqual(result, False)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_flag_dependencies_malformed_chain(self, patch_get, patch_flags):
        """Test handling of malformed dependency chains"""
        # Mock remote flags call to return empty for this flag (fallback returns None)
        patch_flags.return_value = {"featureFlags": {}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Base Flag",
                "key": "base-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
            {
                "id": 2,
                "name": "Missing Chain Flag",
                "key": "missing-chain-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "base-flag",
                                    "operator": "flag_evaluates_to",
                                    "value": True,
                                    "type": "flag",
                                    # No dependency_chain property - should evaluate as inconclusive
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]

        # Should fall back to remote evaluation when dependency_chain is missing
        # Since we're not mocking the remote call, should return None
        result = client.get_feature_flag("missing-chain-flag", "test-user")
        self.assertIsNone(result)

    def test_flag_dependencies_without_context_raises_inconclusive(self):
        """Test that missing flags_by_key raises InconclusiveMatchError"""
        from posthog.feature_flags import (
            evaluate_flag_dependency,
            InconclusiveMatchError,
        )

        property_with_flag_dep = {
            "key": "some-flag",
            "operator": "flag_evaluates_to",
            "value": True,
            "type": "flag",
            "dependency_chain": ["some-flag"],
        }

        # Should raise InconclusiveMatchError when flags_by_key is None
        with self.assertRaises(InconclusiveMatchError) as cm:
            evaluate_flag_dependency(
                property_with_flag_dep,
                flags_by_key=None,  # This should trigger the error
                evaluation_cache={},
                distinct_id="test-user",
                properties={},
                cohort_properties={},
            )

        self.assertIn("Cannot evaluate flag dependency", str(cm.exception))
        self.assertIn("some-flag", str(cm.exception))

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_multi_level_multivariate_dependency_chain(self, patch_get, patch_flags):
        """Test multi-level multivariate dependency chain: dependent-flag -> intermediate-flag -> leaf-flag"""
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            # Leaf flag: multivariate with "control" and "test" variants using person property overrides
            {
                "id": 1,
                "name": "Leaf Flag",
                "key": "leaf-flag",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "control@example.com",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "control",
                        },
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "test@example.com",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "test",
                        },
                        {
                            "rollout_percentage": 50,
                            "variant": "control",
                        },  # Default fallback
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "control",
                                "name": "Control",
                                "rollout_percentage": 50,
                            },
                            {"key": "test", "name": "Test", "rollout_percentage": 50},
                        ]
                    },
                },
            },
            # Intermediate flag: multivariate with "blue" and "green" variants, depends on leaf-flag="control"
            {
                "id": 2,
                "name": "Intermediate Flag",
                "key": "intermediate-flag",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "leaf-flag",
                                    "operator": "flag_evaluates_to",
                                    "value": "control",
                                    "type": "flag",
                                    "dependency_chain": ["leaf-flag"],
                                },
                                {
                                    "key": "variant_type",
                                    "type": "person",
                                    "value": "blue",
                                    "operator": "exact",
                                },
                            ],
                            "rollout_percentage": 100,
                            "variant": "blue",
                        },
                        {
                            "properties": [
                                {
                                    "key": "leaf-flag",
                                    "operator": "flag_evaluates_to",
                                    "value": "control",
                                    "type": "flag",
                                    "dependency_chain": ["leaf-flag"],
                                },
                                {
                                    "key": "variant_type",
                                    "type": "person",
                                    "value": "green",
                                    "operator": "exact",
                                },
                            ],
                            "rollout_percentage": 100,
                            "variant": "green",
                        },
                    ],
                    "multivariate": {
                        "variants": [
                            {"key": "blue", "name": "Blue", "rollout_percentage": 50},
                            {"key": "green", "name": "Green", "rollout_percentage": 50},
                        ]
                    },
                },
            },
            # Dependent flag: boolean flag that depends on intermediate-flag="blue"
            {
                "id": 3,
                "name": "Dependent Flag",
                "key": "dependent-flag",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "intermediate-flag",
                                    "operator": "flag_evaluates_to",
                                    "value": "blue",
                                    "type": "flag",
                                    "dependency_chain": [
                                        "leaf-flag",
                                        "intermediate-flag",
                                    ],
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            },
        ]

        # Test using person properties and variant overrides to ensure predictable variants

        # Test 1: Make sure the leaf flag evaluates to the variant we expect using email overrides
        self.assertEqual(
            "control",
            client.get_feature_flag(
                "leaf-flag",
                "any-user",
                person_properties={"email": "control@example.com"},
            ),
        )
        self.assertEqual(
            "test",
            client.get_feature_flag(
                "leaf-flag",
                "any-user",
                person_properties={"email": "test@example.com"},
            ),
        )

        # Test 2: Make sure the intermediate flag evaluates to the expected variants when dependency is satisfied
        self.assertEqual(
            "blue",
            client.get_feature_flag(
                "intermediate-flag",
                "any-user",
                person_properties={
                    "email": "control@example.com",
                    "variant_type": "blue",
                },
            ),
        )

        self.assertEqual(
            "green",
            client.get_feature_flag(
                "intermediate-flag",
                "any-user",
                person_properties={
                    "email": "control@example.com",
                    "variant_type": "green",
                },
            ),
        )

        # Test 3: Make sure the intermediate flag evaluates to false when leaf dependency fails
        self.assertEqual(
            False,
            client.get_feature_flag(
                "intermediate-flag",
                "any-user",
                person_properties={
                    "email": "test@example.com",  # This makes leaf-flag="test", breaking dependency
                    "variant_type": "blue",
                },
            ),
        )

        # Test 4: When leaf-flag="control", intermediate="blue", dependent should be true
        self.assertEqual(
            True,
            client.get_feature_flag(
                "dependent-flag",
                "any-user",
                person_properties={
                    "email": "control@example.com",
                    "variant_type": "blue",
                },
            ),
        )

        # Test 5: When leaf-flag="control", intermediate="green", dependent should be false
        self.assertEqual(
            False,
            client.get_feature_flag(
                "dependent-flag",
                "any-user",
                person_properties={
                    "email": "control@example.com",
                    "variant_type": "green",
                },
            ),
        )

        # Test 6: When leaf-flag="test", intermediate is False, dependent should be false
        self.assertEqual(
            False,
            client.get_feature_flag(
                "dependent-flag",
                "any-user",
                person_properties={"email": "test@example.com", "variant_type": "blue"},
            ),
        )

    def test_matches_dependency_value(self):
        """Test the matches_dependency_value function logic"""
        from posthog.feature_flags import matches_dependency_value

        # String variant matches string exactly (case-sensitive)
        self.assertTrue(matches_dependency_value("control", "control"))
        self.assertTrue(matches_dependency_value("Control", "Control"))
        self.assertFalse(matches_dependency_value("control", "Control"))
        self.assertFalse(matches_dependency_value("Control", "CONTROL"))
        self.assertFalse(matches_dependency_value("control", "test"))

        # String variant matches boolean true (any variant is truthy)
        self.assertTrue(matches_dependency_value(True, "control"))
        self.assertTrue(matches_dependency_value(True, "test"))
        self.assertFalse(matches_dependency_value(False, "control"))

        # Boolean matches boolean exactly
        self.assertTrue(matches_dependency_value(True, True))
        self.assertTrue(matches_dependency_value(False, False))
        self.assertFalse(matches_dependency_value(False, True))
        self.assertFalse(matches_dependency_value(True, False))

        # Empty string doesn't match
        self.assertFalse(matches_dependency_value(True, ""))
        self.assertFalse(matches_dependency_value("control", ""))

        # Type mismatches
        self.assertFalse(matches_dependency_value(123, "control"))
        self.assertFalse(matches_dependency_value("control", True))

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_production_style_multivariate_dependency_chain(
        self, patch_get, patch_flags
    ):
        """Test production-style multivariate dependency chain: multivariate-root-flag -> multivariate-intermediate-flag -> multivariate-leaf-flag"""
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            # Leaf flag: multivariate with fruit variants
            {
                "id": 451,
                "name": "Multivariate Leaf Flag (Base)",
                "key": "multivariate-leaf-flag",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["pineapple@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "pineapple",
                        },
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["mango@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "mango",
                        },
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["papaya@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "papaya",
                        },
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["kiwi@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "kiwi",
                        },
                        {
                            "properties": [],
                            "rollout_percentage": 0,  # Force default to false for unknown emails
                        },
                    ],
                    "multivariate": {
                        "variants": [
                            {"key": "pineapple", "rollout_percentage": 25},
                            {"key": "mango", "rollout_percentage": 25},
                            {"key": "papaya", "rollout_percentage": 25},
                            {"key": "kiwi", "rollout_percentage": 25},
                        ]
                    },
                },
            },
            # Intermediate flag: multivariate with color variants, depends on fruit
            {
                "id": 467,
                "name": "Multivariate Intermediate Flag (Depends on fruit)",
                "key": "multivariate-intermediate-flag",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "multivariate-leaf-flag",
                                    "type": "flag",
                                    "value": "pineapple",
                                    "operator": "flag_evaluates_to",
                                    "dependency_chain": ["multivariate-leaf-flag"],
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "blue",
                        },
                        {
                            "properties": [
                                {
                                    "key": "multivariate-leaf-flag",
                                    "type": "flag",
                                    "value": "mango",
                                    "operator": "flag_evaluates_to",
                                    "dependency_chain": ["multivariate-leaf-flag"],
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "red",
                        },
                    ],
                    "multivariate": {
                        "variants": [
                            {"key": "blue", "rollout_percentage": 100},
                            {"key": "red", "rollout_percentage": 0},
                            {"key": "green", "rollout_percentage": 0},
                            {"key": "black", "rollout_percentage": 0},
                        ]
                    },
                },
            },
            # Root flag: multivariate with show variants, depends on color
            {
                "id": 468,
                "name": "Multivariate Root Flag (Depends on color)",
                "key": "multivariate-root-flag",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "multivariate-intermediate-flag",
                                    "type": "flag",
                                    "value": "blue",
                                    "operator": "flag_evaluates_to",
                                    "dependency_chain": [
                                        "multivariate-leaf-flag",
                                        "multivariate-intermediate-flag",
                                    ],
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "breaking-bad",
                        },
                        {
                            "properties": [
                                {
                                    "key": "multivariate-intermediate-flag",
                                    "type": "flag",
                                    "value": "red",
                                    "operator": "flag_evaluates_to",
                                    "dependency_chain": [
                                        "multivariate-leaf-flag",
                                        "multivariate-intermediate-flag",
                                    ],
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "the-wire",
                        },
                    ],
                    "multivariate": {
                        "variants": [
                            {"key": "breaking-bad", "rollout_percentage": 100},
                            {"key": "the-wire", "rollout_percentage": 0},
                            {"key": "game-of-thrones", "rollout_percentage": 0},
                            {"key": "the-expanse", "rollout_percentage": 0},
                        ]
                    },
                },
            },
        ]

        # Test successful pineapple -> blue -> breaking-bad chain
        leaf_result = client.get_feature_flag(
            "multivariate-leaf-flag",
            "test-user",
            person_properties={"email": "pineapple@example.com"},
        )
        intermediate_result = client.get_feature_flag(
            "multivariate-intermediate-flag",
            "test-user",
            person_properties={"email": "pineapple@example.com"},
        )
        root_result = client.get_feature_flag(
            "multivariate-root-flag",
            "test-user",
            person_properties={"email": "pineapple@example.com"},
        )

        self.assertEqual(leaf_result, "pineapple")
        self.assertEqual(intermediate_result, "blue")
        self.assertEqual(root_result, "breaking-bad")

        # Test successful mango -> red -> the-wire chain
        mango_leaf_result = client.get_feature_flag(
            "multivariate-leaf-flag",
            "test-user",
            person_properties={"email": "mango@example.com"},
        )
        mango_intermediate_result = client.get_feature_flag(
            "multivariate-intermediate-flag",
            "test-user",
            person_properties={"email": "mango@example.com"},
        )
        mango_root_result = client.get_feature_flag(
            "multivariate-root-flag",
            "test-user",
            person_properties={"email": "mango@example.com"},
        )

        self.assertEqual(mango_leaf_result, "mango")
        self.assertEqual(mango_intermediate_result, "red")
        self.assertEqual(mango_root_result, "the-wire")

        # Test broken chain - user without matching email gets default/false results
        unknown_leaf_result = client.get_feature_flag(
            "multivariate-leaf-flag",
            "test-user",
            person_properties={"email": "unknown@example.com"},
        )
        unknown_intermediate_result = client.get_feature_flag(
            "multivariate-intermediate-flag",
            "test-user",
            person_properties={"email": "unknown@example.com"},
        )
        unknown_root_result = client.get_feature_flag(
            "multivariate-root-flag",
            "test-user",
            person_properties={"email": "unknown@example.com"},
        )

        self.assertEqual(
            unknown_leaf_result, False
        )  # No matching email -> null variant -> false
        self.assertEqual(unknown_intermediate_result, False)  # Dependency not satisfied
        self.assertEqual(unknown_root_result, False)  # Chain broken

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.get")
    def test_load_feature_flags(self, patch_get, patch_poll):
        patch_get.return_value = {
            "flags": [
                {
                    "id": 1,
                    "name": "Beta Feature",
                    "key": "beta-feature",
                    "active": True,
                },
                {
                    "id": 2,
                    "name": "Alpha Feature",
                    "key": "alpha-feature",
                    "active": False,
                },
            ],
            "group_type_mapping": {"0": "company"},
        }
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        with freeze_time("2020-01-01T12:01:00.0000Z"):
            client.load_feature_flags()
        self.assertEqual(len(client.feature_flags), 2)
        self.assertEqual(client.feature_flags[0]["key"], "beta-feature")
        self.assertEqual(client.group_type_mapping, {"0": "company"})
        self.assertEqual(
            client._last_feature_flag_poll.isoformat(), "2020-01-01T12:01:00+00:00"
        )
        self.assertEqual(patch_poll.call_count, 1)

    def test_load_feature_flags_wrong_key(self):
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)

        with self.assertLogs("posthog", level="ERROR") as logs:
            client.load_feature_flags()
            self.assertEqual(
                logs.output[0],
                "ERROR:posthog:[FEATURE FLAGS] Error loading feature flags: To use feature flags, please set a valid personal_api_key. More information: https://posthog.com/docs/api/overview",
            )
        client.debug = True
        self.assertRaises(APIError, client.load_feature_flags)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple(self, patch_get, patch_flags):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple_is_false(self, patch_get, patch_flags):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple_is_true_when_rollout_is_undefined(
        self, patch_get, patch_flags
    ):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple_with_project_api_key(self, patch_get):
        client = Client(project_api_key=FAKE_TEST_API_KEY, on_error=self.set_fail)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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

    @mock.patch("posthog.client.flags")
    def test_feature_enabled_request_multi_variate(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"beta-feature": "variant-1"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.get")
    def test_feature_enabled_simple_without_rollout_percentage(self, patch_get):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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

    @mock.patch("posthog.client.flags")
    def test_get_feature_flag(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"beta-feature": "variant-1"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
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
        self.assertEqual(
            client.get_feature_flag("beta-feature", "distinct_id"), "variant-1"
        )
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.flags")
    def test_feature_enabled_doesnt_exist(self, patch_flags, patch_poll):
        client = Client(FAKE_TEST_API_KEY)
        client.feature_flags = []

        patch_flags.return_value = {"featureFlags": {}}
        self.assertFalse(client.feature_enabled("doesnt-exist", "distinct_id"))

        patch_flags.side_effect = APIError(401, "decide error")
        self.assertIsNone(client.feature_enabled("doesnt-exist", "distinct_id"))

    @mock.patch("posthog.client.Poller")
    @mock.patch("posthog.client.flags")
    def test_personal_api_key_doesnt_exist(self, patch_flags, patch_poll):
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = []

        patch_flags.return_value = {"featureFlags": {"feature-flag": True}}

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

    @mock.patch("posthog.client.flags")
    def test_get_feature_flag_with_variant_overrides(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"beta-feature": "variant-1"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "test@posthog.com",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "second-variant",
                        },
                        {"rollout_percentage": 50, "variant": "first-variant"},
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            }
        ]
        self.assertEqual(
            client.get_feature_flag(
                "beta-feature",
                "test_id",
                person_properties={"email": "test@posthog.com"},
            ),
            "second-variant",
        )
        self.assertEqual(
            client.get_feature_flag("beta-feature", "example_id"), "first-variant"
        )
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_flag_with_clashing_variant_overrides(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"beta-feature": "variant-1"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "test@posthog.com",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "second-variant",
                        },
                        # since second-variant comes first in the list, it will be the one that gets picked
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "test@posthog.com",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "first-variant",
                        },
                        {"rollout_percentage": 50, "variant": "first-variant"},
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            }
        ]
        self.assertEqual(
            client.get_feature_flag(
                "beta-feature",
                "test_id",
                person_properties={"email": "test@posthog.com"},
            ),
            "second-variant",
        )
        self.assertEqual(
            client.get_feature_flag(
                "beta-feature",
                "example_id",
                person_properties={"email": "test@posthog.com"},
            ),
            "second-variant",
        )
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_flag_with_invalid_variant_overrides(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"beta-feature": "variant-1"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "beta-feature",
                "active": True,
                "rollout_percentage": 100,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "test@posthog.com",
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "second???",
                        },
                        {"rollout_percentage": 50, "variant": "first??"},
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 25,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 25,
                            },
                        ]
                    },
                },
            }
        ]
        self.assertEqual(
            client.get_feature_flag(
                "beta-feature",
                "test_id",
                person_properties={"email": "test@posthog.com"},
            ),
            "third-variant",
        )
        self.assertEqual(
            client.get_feature_flag("beta-feature", "example_id"), "second-variant"
        )
        # decide not called because this can be evaluated locally
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_conditions_evaluated_in_order(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"order-test": "server-variant"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key="test")
        client.feature_flags = [
            {
                "id": 1,
                "name": "Order Test Flag",
                "key": "order-test",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 100,
                        },
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": "@vip.com",
                                    "operator": "icontains",
                                }
                            ],
                            "rollout_percentage": 100,
                            "variant": "vip-variant",
                        },
                    ],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "control",
                                "name": "Control",
                                "rollout_percentage": 100,
                            },
                            {
                                "key": "vip-variant",
                                "name": "VIP Variant",
                                "rollout_percentage": 0,
                            },
                        ]
                    },
                },
            }
        ]

        # Even though user@vip.com would match the second condition with variant override,
        # they should match the first condition and get control
        result = client.get_feature_flag(
            "order-test",
            "user123",
            person_properties={"email": "user@vip.com"},
        )
        self.assertEqual(result, "control")

        # server not called because this can be evaluated locally
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch("posthog.client.flags")
    def test_boolean_feature_flag_payloads_local(self, patch_flags):
        basic_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "person-flag",
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
                "payloads": {"true": 300},
            },
        }
        self.client.feature_flags = [basic_flag]

        self.assertEqual(
            self.client.get_feature_flag_payload(
                "person-flag", "some-distinct-id", person_properties={"region": "USA"}
            ),
            300,
        )

        self.assertEqual(
            self.client.get_feature_flag_payload(
                "person-flag",
                "some-distinct-id",
                match_value=True,
                person_properties={"region": "USA"},
            ),
            300,
        )
        self.assertEqual(patch_flags.call_count, 0)

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_boolean_feature_flag_payload_decide(self, patch_flags, patch_capture):
        patch_flags.return_value = {
            "featureFlags": {"person-flag": True},
            "featureFlagPayloads": {"person-flag": 300},
        }
        self.assertEqual(
            self.client.get_feature_flag_payload(
                "person-flag", "some-distinct-id", person_properties={"region": "USA"}
            ),
            300,
        )

        self.assertEqual(
            self.client.get_feature_flag_payload(
                "person-flag",
                "some-distinct-id",
                match_value=True,
                person_properties={"region": "USA"},
            ),
            300,
        )
        self.assertEqual(patch_flags.call_count, 2)
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.reset_mock()

    @mock.patch("posthog.client.flags")
    def test_multivariate_feature_flag_payloads(self, patch_flags):
        multivariate_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "beta-feature",
            "active": True,
            "rollout_percentage": 100,
            "filters": {
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "email",
                                "type": "person",
                                "value": "test@posthog.com",
                                "operator": "exact",
                            }
                        ],
                        "rollout_percentage": 100,
                        "variant": "second???",
                    },
                    {"rollout_percentage": 50, "variant": "first??"},
                ],
                "multivariate": {
                    "variants": [
                        {
                            "key": "first-variant",
                            "name": "First Variant",
                            "rollout_percentage": 50,
                        },
                        {
                            "key": "second-variant",
                            "name": "Second Variant",
                            "rollout_percentage": 25,
                        },
                        {
                            "key": "third-variant",
                            "name": "Third Variant",
                            "rollout_percentage": 25,
                        },
                    ]
                },
                "payloads": {
                    "first-variant": '"some-payload"',
                    "third-variant": '{"a": "json"}',
                },
            },
        }
        self.client.feature_flags = [multivariate_flag]

        self.assertEqual(
            self.client.get_feature_flag_payload(
                "beta-feature",
                "test_id",
                person_properties={"email": "test@posthog.com"},
            ),
            {"a": "json"},
        )
        self.assertEqual(
            self.client.get_feature_flag_payload(
                "beta-feature",
                "test_id",
                match_value="third-variant",
                person_properties={"email": "test@posthog.com"},
            ),
            {"a": "json"},
        )

        # Force different match value
        self.assertEqual(
            self.client.get_feature_flag_payload(
                "beta-feature",
                "test_id",
                match_value="first-variant",
                person_properties={"email": "test@posthog.com"},
            ),
            "some-payload",
        )
        self.assertEqual(patch_flags.call_count, 0)


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

        property_c = self.property(
            key="key", value=["value1", "value2", "value3"], operator="exact"
        )
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

        property_c = self.property(
            key="key", value=["value1", "value2", "value3"], operator="is_not"
        )
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
        self.assertFalse(match_property(property_a, {"key": None}))

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
        property_a = self.property(key="key", value=r"\.com$", operator="regex")
        self.assertTrue(match_property(property_a, {"key": "value.com"}))
        self.assertTrue(match_property(property_a, {"key": "value2.com"}))
        self.assertFalse(match_property(property_a, {"key": "value2com"}))

        self.assertFalse(match_property(property_a, {"key": ".com343tfvalue5"}))
        self.assertFalse(match_property(property_a, {"key": "Alakazam"}))
        self.assertFalse(match_property(property_a, {"key": 123}))
        self.assertFalse(match_property(property_a, {"key": "valuecom"}))
        self.assertFalse(match_property(property_a, {"key": r"value\com"}))

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
        # now we handle type mismatches so this should be true
        self.assertTrue(match_property(property_a, {"key": "23"}))

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
        # now we handle type mismatches so this should be true
        self.assertTrue(match_property(property_c, {"key": "3"}))

        property_d = self.property(key="key", value="43", operator="lte")
        self.assertTrue(match_property(property_d, {"key": "41"}))
        self.assertTrue(match_property(property_d, {"key": "42"}))
        self.assertTrue(match_property(property_d, {"key": "43"}))

        self.assertFalse(match_property(property_d, {"key": "44"}))
        self.assertFalse(match_property(property_d, {"key": 44}))
        self.assertTrue(match_property(property_d, {"key": 42}))

        property_e = self.property(key="key", value="30", operator="lt")
        self.assertTrue(match_property(property_e, {"key": "29"}))

        # depending on the type of override, we adjust type comparison
        self.assertTrue(match_property(property_e, {"key": "100"}))
        self.assertFalse(match_property(property_e, {"key": 100}))

        property_f = self.property(key="key", value="123aloha", operator="gt")
        self.assertFalse(match_property(property_f, {"key": "123"}))
        self.assertFalse(match_property(property_f, {"key": 122}))

        # this turns into a string comparison
        self.assertTrue(match_property(property_f, {"key": 129}))

    def test_match_property_date_operators(self):
        property_a = self.property(
            key="key", value="2022-05-01", operator="is_date_before"
        )
        self.assertTrue(match_property(property_a, {"key": "2022-03-01"}))
        self.assertTrue(match_property(property_a, {"key": "2022-04-30"}))
        self.assertTrue(match_property(property_a, {"key": datetime.date(2022, 4, 30)}))
        self.assertTrue(
            match_property(property_a, {"key": datetime.datetime(2022, 4, 30, 1, 2, 3)})
        )
        self.assertTrue(
            match_property(
                property_a,
                {
                    "key": datetime.datetime(
                        2022, 4, 30, 1, 2, 3, tzinfo=tz.gettz("Europe/Madrid")
                    )
                },
            )
        )
        self.assertTrue(match_property(property_a, {"key": parser.parse("2022-04-30")}))
        self.assertFalse(match_property(property_a, {"key": "2022-05-30"}))

        # Can't be a number
        with self.assertRaises(InconclusiveMatchError):
            match_property(property_a, {"key": 1})

        # can't be invalid string
        with self.assertRaises(InconclusiveMatchError):
            match_property(property_a, {"key": "abcdef"})

        property_b = self.property(
            key="key", value="2022-05-01", operator="is_date_after"
        )
        self.assertTrue(match_property(property_b, {"key": "2022-05-02"}))
        self.assertTrue(match_property(property_b, {"key": "2022-05-30"}))
        self.assertTrue(
            match_property(property_b, {"key": datetime.datetime(2022, 5, 30)})
        )
        self.assertTrue(match_property(property_b, {"key": parser.parse("2022-05-30")}))
        self.assertFalse(match_property(property_b, {"key": "2022-04-30"}))

        # can't be invalid string
        with self.assertRaises(InconclusiveMatchError):
            match_property(property_b, {"key": "abcdef"})

        # Invalid flag property
        property_c = self.property(key="key", value=1234, operator="is_date_before")

        with self.assertRaises(InconclusiveMatchError):
            match_property(property_c, {"key": 1})

        # Timezone aware property
        property_d = self.property(
            key="key", value="2022-04-05 12:34:12 +01:00", operator="is_date_before"
        )
        self.assertFalse(match_property(property_d, {"key": "2022-05-30"}))

        self.assertTrue(match_property(property_d, {"key": "2022-03-30"}))
        self.assertTrue(
            match_property(property_d, {"key": "2022-04-05 12:34:11 +01:00"})
        )
        self.assertTrue(
            match_property(property_d, {"key": "2022-04-05 12:34:11 +01:00"})
        )

        self.assertFalse(
            match_property(property_d, {"key": "2022-04-05 12:34:13 +01:00"})
        )

        self.assertTrue(
            match_property(property_d, {"key": "2022-04-05 11:34:11 +00:00"})
        )
        self.assertFalse(
            match_property(property_d, {"key": "2022-04-05 11:34:13 +00:00"})
        )

    @freeze_time("2022-05-01")
    def test_match_property_relative_date_operators(self):
        property_a = self.property(key="key", value="-6h", operator="is_date_before")
        self.assertTrue(match_property(property_a, {"key": "2022-03-01"}))
        self.assertTrue(match_property(property_a, {"key": "2022-04-30"}))
        self.assertTrue(
            match_property(property_a, {"key": datetime.datetime(2022, 4, 30, 1, 2, 3)})
        )
        # false because date comparison, instead of datetime, so reduces to same date
        self.assertFalse(
            match_property(property_a, {"key": datetime.date(2022, 4, 30)})
        )

        self.assertFalse(
            match_property(
                property_a, {"key": datetime.datetime(2022, 4, 30, 19, 2, 3)}
            )
        )
        self.assertTrue(
            match_property(
                property_a,
                {
                    "key": datetime.datetime(
                        2022, 4, 30, 1, 2, 3, tzinfo=tz.gettz("Europe/Madrid")
                    )
                },
            )
        )
        self.assertTrue(match_property(property_a, {"key": parser.parse("2022-04-30")}))
        self.assertFalse(match_property(property_a, {"key": "2022-05-30"}))

        # Can't be a number
        with self.assertRaises(InconclusiveMatchError):
            match_property(property_a, {"key": 1})

        # can't be invalid string
        with self.assertRaises(InconclusiveMatchError):
            match_property(property_a, {"key": "abcdef"})

        property_b = self.property(key="key", value="1h", operator="is_date_after")
        self.assertTrue(match_property(property_b, {"key": "2022-05-02"}))
        self.assertTrue(match_property(property_b, {"key": "2022-05-30"}))
        self.assertTrue(
            match_property(property_b, {"key": datetime.datetime(2022, 5, 30)})
        )
        self.assertTrue(match_property(property_b, {"key": parser.parse("2022-05-30")}))
        self.assertFalse(match_property(property_b, {"key": "2022-04-30"}))

        # can't be invalid string
        with self.assertRaises(InconclusiveMatchError):
            self.assertFalse(match_property(property_b, {"key": "abcdef"}))

        # Invalid flag property
        property_c = self.property(key="key", value=1234, operator="is_date_after")

        with self.assertRaises(InconclusiveMatchError):
            self.assertFalse(match_property(property_c, {"key": 1}))

        # parsed as 1234-05-01 for some reason?
        self.assertTrue(match_property(property_c, {"key": "2022-05-30"}))

        # # Timezone aware property
        property_d = self.property(key="key", value="12d", operator="is_date_before")
        self.assertFalse(match_property(property_d, {"key": "2022-05-30"}))

        self.assertTrue(match_property(property_d, {"key": "2022-03-30"}))
        self.assertTrue(
            match_property(property_d, {"key": "2022-04-05 12:34:11+01:00"})
        )
        self.assertTrue(
            match_property(property_d, {"key": "2022-04-19 01:34:11+02:00"})
        )

        self.assertFalse(
            match_property(property_d, {"key": "2022-04-19 02:00:01+02:00"})
        )

        # Try all possible relative dates
        property_e = self.property(key="key", value="1h", operator="is_date_before")
        self.assertFalse(match_property(property_e, {"key": "2022-05-01 00:00:00"}))
        self.assertTrue(match_property(property_e, {"key": "2022-04-30 22:00:00"}))

        property_f = self.property(key="key", value="-1d", operator="is_date_before")
        self.assertTrue(match_property(property_f, {"key": "2022-04-29 23:59:00"}))
        self.assertFalse(match_property(property_f, {"key": "2022-04-30 00:00:01"}))

        property_g = self.property(key="key", value="1w", operator="is_date_before")
        self.assertTrue(match_property(property_g, {"key": "2022-04-23 00:00:00"}))
        self.assertFalse(match_property(property_g, {"key": "2022-04-24 00:00:00"}))
        self.assertFalse(match_property(property_g, {"key": "2022-04-24 00:00:01"}))

        property_h = self.property(key="key", value="1m", operator="is_date_before")
        self.assertTrue(match_property(property_h, {"key": "2022-03-01 00:00:00"}))
        self.assertFalse(match_property(property_h, {"key": "2022-04-05 00:00:00"}))

        property_i = self.property(key="key", value="1y", operator="is_date_before")
        self.assertTrue(match_property(property_i, {"key": "2021-04-28 00:00:00"}))
        self.assertFalse(match_property(property_i, {"key": "2021-05-01 00:00:01"}))

        property_j = self.property(key="key", value="122h", operator="is_date_after")
        self.assertTrue(match_property(property_j, {"key": "2022-05-01 00:00:00"}))
        self.assertFalse(match_property(property_j, {"key": "2022-04-23 01:00:00"}))

        property_k = self.property(key="key", value="2d", operator="is_date_after")
        self.assertTrue(match_property(property_k, {"key": "2022-05-01 00:00:00"}))
        self.assertTrue(match_property(property_k, {"key": "2022-04-29 00:00:01"}))
        self.assertFalse(match_property(property_k, {"key": "2022-04-29 00:00:00"}))

        property_l = self.property(key="key", value="-02w", operator="is_date_after")
        self.assertTrue(match_property(property_l, {"key": "2022-05-01 00:00:00"}))
        self.assertFalse(match_property(property_l, {"key": "2022-04-16 00:00:00"}))

        property_m = self.property(key="key", value="1m", operator="is_date_after")
        self.assertTrue(match_property(property_m, {"key": "2022-04-01 00:00:01"}))
        self.assertFalse(match_property(property_m, {"key": "2022-04-01 00:00:00"}))

        property_n = self.property(key="key", value="1y", operator="is_date_after")
        self.assertTrue(match_property(property_n, {"key": "2022-05-01 00:00:00"}))
        self.assertTrue(match_property(property_n, {"key": "2021-05-01 00:00:01"}))
        self.assertFalse(match_property(property_n, {"key": "2021-05-01 00:00:00"}))
        self.assertFalse(match_property(property_n, {"key": "2021-04-30 00:00:00"}))
        self.assertFalse(match_property(property_n, {"key": "2021-03-01 12:13:00"}))

    def test_none_property_value_with_all_operators(self):
        property_a = self.property(key="key", value="none", operator="is_not")
        self.assertFalse(match_property(property_a, {"key": None}))
        self.assertTrue(match_property(property_a, {"key": "non"}))

        property_b = self.property(key="key", value=None, operator="is_set")
        self.assertFalse(match_property(property_b, {"key": None}))

        property_c = self.property(key="key", value="no", operator="icontains")
        self.assertFalse(match_property(property_c, {"key": None}))
        self.assertFalse(match_property(property_c, {"key": "smh"}))

        property_d = self.property(key="key", value="No", operator="regex")
        self.assertFalse(match_property(property_d, {"key": None}))

        property_d_lower_case = self.property(key="key", value="no", operator="regex")
        self.assertFalse(match_property(property_d_lower_case, {"key": None}))

        property_e = self.property(key="key", value=1, operator="gt")
        self.assertFalse(match_property(property_e, {"key": None}))

        property_f = self.property(key="key", value=1, operator="lt")
        self.assertFalse(match_property(property_f, {"key": None}))

        property_g = self.property(key="key", value="xyz", operator="gte")
        self.assertFalse(match_property(property_g, {"key": None}))

        property_h = self.property(key="key", value="Oo", operator="lte")
        self.assertFalse(match_property(property_h, {"key": None}))

        property_i = self.property(
            key="key", value="2022-05-01", operator="is_date_before"
        )
        self.assertFalse(match_property(property_i, {"key": None}))

        property_j = self.property(
            key="key", value="2022-05-01", operator="is_date_after"
        )
        self.assertFalse(match_property(property_j, {"key": None}))

        property_k = self.property(
            key="key", value="2022-05-01", operator="is_date_before"
        )
        with self.assertRaises(InconclusiveMatchError):
            self.assertFalse(match_property(property_k, {"key": "random"}))

    def test_unknown_operator(self):
        property_a = self.property(key="key", value="2022-05-01", operator="is_unknown")
        with self.assertRaises(InconclusiveMatchError) as exception_context:
            match_property(property_a, {"key": "random"})
        self.assertEqual(
            str(exception_context.exception), "Unknown operator is_unknown"
        )


class TestRelativeDateParsing(unittest.TestCase):
    def test_invalid_input(self):
        with freeze_time("2020-01-01T12:01:20.1340Z"):
            assert relative_date_parse_for_feature_flag_matching("1") is None
            assert relative_date_parse_for_feature_flag_matching("1x") is None
            assert relative_date_parse_for_feature_flag_matching("1.2y") is None
            assert relative_date_parse_for_feature_flag_matching("1z") is None
            assert relative_date_parse_for_feature_flag_matching("1s") is None
            assert (
                relative_date_parse_for_feature_flag_matching("123344000.134m") is None
            )
            assert relative_date_parse_for_feature_flag_matching("bazinga") is None
            assert relative_date_parse_for_feature_flag_matching("000bello") is None
            assert relative_date_parse_for_feature_flag_matching("000hello") is None

            assert relative_date_parse_for_feature_flag_matching("000h") is not None
            assert relative_date_parse_for_feature_flag_matching("1000h") is not None

    def test_overflow(self):
        assert relative_date_parse_for_feature_flag_matching("1000000h") is None
        assert (
            relative_date_parse_for_feature_flag_matching("100000000000000000y") is None
        )

    def test_hour_parsing(self):
        with freeze_time("2020-01-01T12:01:20.1340Z"):
            assert relative_date_parse_for_feature_flag_matching(
                "1h"
            ) == datetime.datetime(
                2020, 1, 1, 11, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "2h"
            ) == datetime.datetime(
                2020, 1, 1, 10, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "24h"
            ) == datetime.datetime(
                2019, 12, 31, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "30h"
            ) == datetime.datetime(
                2019, 12, 31, 6, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "48h"
            ) == datetime.datetime(
                2019, 12, 30, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )

            assert relative_date_parse_for_feature_flag_matching(
                "24h"
            ) == relative_date_parse_for_feature_flag_matching("1d")
            assert relative_date_parse_for_feature_flag_matching(
                "48h"
            ) == relative_date_parse_for_feature_flag_matching("2d")

    def test_day_parsing(self):
        with freeze_time("2020-01-01T12:01:20.1340Z"):
            assert relative_date_parse_for_feature_flag_matching(
                "1d"
            ) == datetime.datetime(
                2019, 12, 31, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "2d"
            ) == datetime.datetime(
                2019, 12, 30, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "7d"
            ) == datetime.datetime(
                2019, 12, 25, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "14d"
            ) == datetime.datetime(
                2019, 12, 18, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "30d"
            ) == datetime.datetime(
                2019, 12, 2, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )

            assert relative_date_parse_for_feature_flag_matching(
                "7d"
            ) == relative_date_parse_for_feature_flag_matching("1w")

    def test_week_parsing(self):
        with freeze_time("2020-01-01T12:01:20.1340Z"):
            assert relative_date_parse_for_feature_flag_matching(
                "1w"
            ) == datetime.datetime(
                2019, 12, 25, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "2w"
            ) == datetime.datetime(
                2019, 12, 18, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "4w"
            ) == datetime.datetime(
                2019, 12, 4, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "8w"
            ) == datetime.datetime(
                2019, 11, 6, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )

            assert relative_date_parse_for_feature_flag_matching(
                "1m"
            ) == datetime.datetime(
                2019, 12, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "4w"
            ) != relative_date_parse_for_feature_flag_matching("1m")

    def test_month_parsing(self):
        with freeze_time("2020-01-01T12:01:20.1340Z"):
            assert relative_date_parse_for_feature_flag_matching(
                "1m"
            ) == datetime.datetime(
                2019, 12, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "2m"
            ) == datetime.datetime(
                2019, 11, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "4m"
            ) == datetime.datetime(
                2019, 9, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "8m"
            ) == datetime.datetime(
                2019, 5, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )

            assert relative_date_parse_for_feature_flag_matching(
                "1y"
            ) == datetime.datetime(
                2019, 1, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "12m"
            ) == relative_date_parse_for_feature_flag_matching("1y")

        with freeze_time("2020-04-03T00:00:00"):
            assert relative_date_parse_for_feature_flag_matching(
                "1m"
            ) == datetime.datetime(2020, 3, 3, 0, 0, 0, tzinfo=tz.gettz("UTC"))
            assert relative_date_parse_for_feature_flag_matching(
                "2m"
            ) == datetime.datetime(2020, 2, 3, 0, 0, 0, tzinfo=tz.gettz("UTC"))
            assert relative_date_parse_for_feature_flag_matching(
                "4m"
            ) == datetime.datetime(2019, 12, 3, 0, 0, 0, tzinfo=tz.gettz("UTC"))
            assert relative_date_parse_for_feature_flag_matching(
                "8m"
            ) == datetime.datetime(2019, 8, 3, 0, 0, 0, tzinfo=tz.gettz("UTC"))

            assert relative_date_parse_for_feature_flag_matching(
                "1y"
            ) == datetime.datetime(2019, 4, 3, 0, 0, 0, tzinfo=tz.gettz("UTC"))
            assert relative_date_parse_for_feature_flag_matching(
                "12m"
            ) == relative_date_parse_for_feature_flag_matching("1y")

    def test_year_parsing(self):
        with freeze_time("2020-01-01T12:01:20.1340Z"):
            assert relative_date_parse_for_feature_flag_matching(
                "1y"
            ) == datetime.datetime(
                2019, 1, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "2y"
            ) == datetime.datetime(
                2018, 1, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "4y"
            ) == datetime.datetime(
                2016, 1, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )
            assert relative_date_parse_for_feature_flag_matching(
                "8y"
            ) == datetime.datetime(
                2012, 1, 1, 12, 1, 20, 134000, tzinfo=tz.gettz("UTC")
            )


class TestCaptureCalls(unittest.TestCase):
    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_capture_is_called(self, patch_flags, patch_capture):
        patch_flags.return_value = {"featureFlags": {"decide-flag": "decide-value"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "complex-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [{"key": "region", "value": "USA"}],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            }
        ]

        self.assertTrue(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id",
                person_properties={"region": "USA", "name": "Aloha"},
            )
        )
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "complex-flag",
                "$feature_flag_response": True,
                "locally_evaluated": True,
                "$feature/complex-flag": True,
            },
            groups={},
            disable_geoip=None,
        )
        patch_capture.reset_mock()

        # called again for same user, shouldn't call capture again
        self.assertTrue(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id",
                person_properties={"region": "USA", "name": "Aloha"},
            )
        )
        self.assertEqual(patch_capture.call_count, 0)
        patch_capture.reset_mock()

        # called for different user, should call capture again
        self.assertTrue(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id2",
                person_properties={"region": "USA", "name": "Aloha"},
            )
        )
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id2",
            properties={
                "$feature_flag": "complex-flag",
                "$feature_flag_response": True,
                "locally_evaluated": True,
                "$feature/complex-flag": True,
            },
            groups={},
            disable_geoip=None,
        )
        patch_capture.reset_mock()

        # called for different user, but send configuration is false, so should NOT call capture again
        self.assertTrue(
            client.get_feature_flag(
                "complex-flag",
                "some-distinct-id345",
                person_properties={"region": "USA", "name": "Aloha"},
                send_feature_flag_events=False,
            )
        )
        self.assertEqual(patch_capture.call_count, 0)
        patch_capture.reset_mock()

        # called for different flag, falls back to decide, should call capture again
        self.assertEqual(
            client.get_feature_flag(
                "decide-flag",
                "some-distinct-id2",
                person_properties={"region": "USA", "name": "Aloha"},
                groups={"organization": "org1"},
            ),
            "decide-value",
        )
        self.assertEqual(patch_flags.call_count, 1)
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id2",
            properties={
                "$feature_flag": "decide-flag",
                "$feature_flag_response": "decide-value",
                "locally_evaluated": False,
                "$feature/decide-flag": "decide-value",
            },
            groups={"organization": "org1"},
            disable_geoip=None,
        )

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_capture_is_called_with_flag_details(self, patch_flags, patch_capture):
        patch_flags.return_value = {
            "flags": {
                "decide-flag": {
                    "key": "decide-flag",
                    "enabled": True,
                    "variant": "decide-variant",
                    "reason": {
                        "description": "Matched condition set 1",
                    },
                    "metadata": {
                        "id": 23,
                        "version": 42,
                    },
                },
                "false-flag": {
                    "key": "false-flag",
                    "enabled": False,
                    "variant": None,
                    "reason": {
                        "code": "no_matching_condition",
                        "description": "No matching condition",
                        "condition_index": None,
                    },
                    "metadata": {
                        "id": 1,
                        "version": 2,
                    },
                },
            },
            "requestId": "18043bf7-9cf6-44cd-b959-9662ee20d371",
        }
        client = Client(FAKE_TEST_API_KEY)

        self.assertEqual(
            client.get_feature_flag("decide-flag", "some-distinct-id"), "decide-variant"
        )
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "decide-flag",
                "$feature_flag_response": "decide-variant",
                "locally_evaluated": False,
                "$feature/decide-flag": "decide-variant",
                "$feature_flag_reason": "Matched condition set 1",
                "$feature_flag_id": 23,
                "$feature_flag_version": 42,
                "$feature_flag_request_id": "18043bf7-9cf6-44cd-b959-9662ee20d371",
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_capture_is_called_with_flag_details_and_payload(
        self, patch_flags, patch_capture
    ):
        patch_flags.return_value = {
            "flags": {
                "decide-flag-with-payload": {
                    "key": "decide-flag-with-payload",
                    "enabled": True,
                    "variant": None,
                    "reason": {
                        "code": "matched_condition",
                        "condition_index": 0,
                        "description": "Matched condition set 1",
                    },
                    "metadata": {
                        "id": 23,
                        "version": 42,
                        "payload": '{"foo": "bar"}',
                    },
                }
            },
            "requestId": "18043bf7-9cf6-44cd-b959-9662ee20d371",
        }
        client = Client(FAKE_TEST_API_KEY)

        self.assertEqual(
            client.get_feature_flag_payload(
                "decide-flag-with-payload", "some-distinct-id"
            ),
            {"foo": "bar"},
        )
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "decide-flag-with-payload",
                "$feature_flag_response": True,
                "locally_evaluated": False,
                "$feature/decide-flag-with-payload": True,
                "$feature_flag_reason": "Matched condition set 1",
                "$feature_flag_id": 23,
                "$feature_flag_version": 42,
                "$feature_flag_request_id": "18043bf7-9cf6-44cd-b959-9662ee20d371",
                "$feature_flag_payload": {"foo": "bar"},
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    def test_capture_is_called_but_does_not_add_all_flags(self, patch_flags):
        patch_flags.return_value = {"featureFlags": {"decide-flag": "decide-value"}}
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "complex-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [{"key": "region", "value": "USA"}],
                            "rollout_percentage": 100,
                        },
                    ],
                },
            },
            {
                "id": 2,
                "name": "Gamma Feature",
                "key": "simple-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                        },
                    ],
                },
            },
        ]

        self.assertTrue(
            client.get_feature_flag(
                "complex-flag", "some-distinct-id", person_properties={"region": "USA"}
            )
        )

        # Grab the capture message that was just added to the queue
        msg = client.queue.get(block=False)
        assert msg["event"] == "$feature_flag_called"
        assert msg["properties"]["$feature_flag"] == "complex-flag"
        assert msg["properties"]["$feature_flag_response"] is True
        assert msg["properties"]["locally_evaluated"] is True
        assert msg["properties"]["$feature/complex-flag"] is True
        assert "$feature/simple-flag" not in msg["properties"]
        assert "$active_feature_flags" not in msg["properties"]

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_capture_is_called_in_get_feature_flag_payload(
        self, patch_flags, patch_capture
    ):
        patch_flags.return_value = {
            "featureFlags": {"person-flag": True},
            "featureFlagPayloads": {"person-flag": 300},
        }
        client = Client(
            project_api_key=FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY
        )

        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "person-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [{"key": "region", "value": "USA"}],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            }
        ]

        # Call get_feature_flag_payload with match_value=None to trigger get_feature_flag
        client.get_feature_flag_payload(
            key="person-flag",
            distinct_id="some-distinct-id",
            person_properties={"region": "USA", "name": "Aloha"},
        )

        # Assert that capture was called once, with the correct parameters
        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "person-flag",
                "$feature_flag_response": True,
                "locally_evaluated": True,
                "$feature/person-flag": True,
            },
            groups={},
            disable_geoip=None,
        )

        # Reset mocks for further tests
        patch_capture.reset_mock()
        patch_flags.reset_mock()

        # Call get_feature_flag_payload again for the same user; capture should not be called again because we've already reported an event for this distinct_id + flag
        client.get_feature_flag_payload(
            key="person-flag",
            distinct_id="some-distinct-id",
            person_properties={"region": "USA", "name": "Aloha"},
        )

        self.assertEqual(patch_capture.call_count, 0)
        patch_capture.reset_mock()

        # Call get_feature_flag_payload for a different user; capture should be called
        client.get_feature_flag_payload(
            key="person-flag",
            distinct_id="some-distinct-id2",
            person_properties={"region": "USA", "name": "Aloha"},
        )

        self.assertEqual(patch_capture.call_count, 1)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id2",
            properties={
                "$feature_flag": "person-flag",
                "$feature_flag_response": True,
                "locally_evaluated": True,
                "$feature/person-flag": True,
            },
            groups={},
            disable_geoip=None,
        )

        patch_capture.reset_mock()

    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_disable_geoip_get_flag_capture_call(self, patch_flags, patch_capture):
        patch_flags.return_value = {"featureFlags": {"decide-flag": "decide-value"}}
        client = Client(
            FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY, disable_geoip=True
        )
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "complex-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [{"key": "region", "value": "USA"}],
                            "rollout_percentage": 100,
                        }
                    ],
                },
            }
        ]

        client.get_feature_flag(
            "complex-flag",
            "some-distinct-id",
            person_properties={"region": "USA", "name": "Aloha"},
            disable_geoip=False,
        )

        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "complex-flag",
                "$feature_flag_response": True,
                "locally_evaluated": True,
                "$feature/complex-flag": True,
            },
            groups={},
            disable_geoip=False,
        )

    @mock.patch("posthog.client.MAX_DICT_SIZE", 100)
    @mock.patch.object(Client, "capture")
    @mock.patch("posthog.client.flags")
    def test_capture_multiple_users_doesnt_out_of_memory(
        self, patch_flags, patch_capture
    ):
        client = Client(FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY)
        client.feature_flags = [
            {
                "id": 1,
                "name": "Beta Feature",
                "key": "complex-flag",
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
            client.get_feature_flag(
                "complex-flag",
                distinct_id,
                person_properties={"region": "USA", "name": "Aloha"},
            )
            patch_capture.assert_called_with(
                "$feature_flag_called",
                distinct_id=distinct_id,
                properties={
                    "$feature_flag": "complex-flag",
                    "$feature_flag_response": True,
                    "locally_evaluated": True,
                    "$feature/complex-flag": True,
                },
                groups={},
                disable_geoip=None,
            )

            self.assertEqual(
                len(client.distinct_ids_feature_flags_reported), i % 100 + 1
            )


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
        print("FAIL", e, batch)  # noqa: T201
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
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 55}],
                    "multivariate": {
                        "variants": [
                            {
                                "key": "first-variant",
                                "name": "First Variant",
                                "rollout_percentage": 50,
                            },
                            {
                                "key": "second-variant",
                                "name": "Second Variant",
                                "rollout_percentage": 20,
                            },
                            {
                                "key": "third-variant",
                                "name": "Third Variant",
                                "rollout_percentage": 20,
                            },
                            {
                                "key": "fourth-variant",
                                "name": "Fourth Variant",
                                "rollout_percentage": 5,
                            },
                            {
                                "key": "fifth-variant",
                                "name": "Fifth Variant",
                                "rollout_percentage": 5,
                            },
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
            feature_flag_match = self.client.get_feature_flag(
                "multivariate-flag", distinctID
            )

            if results[i]:
                self.assertEqual(feature_flag_match, results[i])
            else:
                self.assertFalse(feature_flag_match)

    @mock.patch("posthog.client.flags")
    def test_feature_flag_case_sensitive(self, mock_decide):
        mock_decide.return_value = {
            "featureFlags": {}
        }  # Ensure decide returns empty flags

        client = Client(
            project_api_key=FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY
        )
        client.feature_flags = [
            {
                "id": 1,
                "key": "Beta-Feature",
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 100}],
                },
            }
        ]

        # Test that flag evaluation is case-sensitive
        self.assertTrue(client.feature_enabled("Beta-Feature", "user1"))
        self.assertFalse(client.feature_enabled("beta-feature", "user1"))
        self.assertFalse(client.feature_enabled("BETA-FEATURE", "user1"))

    @mock.patch("posthog.client.flags")
    def test_feature_flag_payload_case_sensitive(self, mock_decide):
        mock_decide.return_value = {
            "featureFlags": {"Beta-Feature": True},
            "featureFlagPayloads": {"Beta-Feature": {"some": "value"}},
        }

        client = Client(
            project_api_key=FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY
        )
        client.feature_flags = [
            {
                "id": 1,
                "key": "Beta-Feature",
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 100}],
                    "payloads": {
                        "true": {"some": "value"},
                    },
                },
            }
        ]

        # Test that payload retrieval is case-sensitive
        self.assertEqual(
            client.get_feature_flag_payload("Beta-Feature", "user1"), {"some": "value"}
        )
        self.assertIsNone(client.get_feature_flag_payload("beta-feature", "user1"))
        self.assertIsNone(client.get_feature_flag_payload("BETA-FEATURE", "user1"))

    @mock.patch("posthog.client.flags")
    def test_feature_flag_case_sensitive_consistency(self, mock_decide):
        mock_decide.return_value = {
            "featureFlags": {"Beta-Feature": True},
            "featureFlagPayloads": {"Beta-Feature": {"some": "value"}},
        }

        client = Client(
            project_api_key=FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY
        )
        client.feature_flags = [
            {
                "id": 1,
                "key": "Beta-Feature",
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 100}],
                    "payloads": {
                        "true": {"some": "value"},
                    },
                },
            }
        ]

        # Test that flag evaluation and payload retrieval are consistently case-sensitive
        # Only exact match should work
        self.assertTrue(client.feature_enabled("Beta-Feature", "user1"))
        self.assertEqual(
            client.get_feature_flag_payload("Beta-Feature", "user1"), {"some": "value"}
        )

        # Different cases should not match
        test_cases = ["beta-feature", "BETA-FEATURE", "bEtA-FeAtUrE"]
        for case in test_cases:
            self.assertFalse(client.feature_enabled(case, "user1"))

    @mock.patch("posthog.client.flags")
    def test_get_all_flags_with_flag_keys_to_evaluate(self, mock_flags):
        """Test that get_all_flags with flag_keys_to_evaluate only evaluates specified flags"""
        mock_flags.return_value = {
            "featureFlags": {
                "flag1": "value1",
                "flag2": True,
            }
        }

        client = Client(
            project_api_key=FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY
        )

        # Call get_all_flags with flag_keys_to_evaluate
        result = client.get_all_flags(
            "user123",
            flag_keys_to_evaluate=["flag1", "flag2"],
            person_properties={"region": "USA"},
        )

        # Verify flags() was called with flag_keys_to_evaluate
        mock_flags.assert_called_once()
        call_args = mock_flags.call_args[1]
        self.assertEqual(call_args["flag_keys_to_evaluate"], ["flag1", "flag2"])
        self.assertEqual(
            call_args["person_properties"], {"distinct_id": "user123", "region": "USA"}
        )

        # Check the result
        self.assertEqual(result, {"flag1": "value1", "flag2": True})

    @mock.patch("posthog.client.flags")
    def test_get_all_flags_and_payloads_with_flag_keys_to_evaluate(self, mock_flags):
        """Test that get_all_flags_and_payloads with flag_keys_to_evaluate only evaluates specified flags"""
        mock_flags.return_value = {
            "featureFlags": {
                "flag1": "variant1",
                "flag3": True,
            },
            "featureFlagPayloads": {
                "flag1": {"data": "payload1"},
                "flag3": {"data": "payload3"},
            },
        }

        client = Client(
            project_api_key=FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY
        )

        # Call get_all_flags_and_payloads with flag_keys_to_evaluate
        result = client.get_all_flags_and_payloads(
            "user123",
            flag_keys_to_evaluate=["flag1", "flag3"],
            person_properties={"subscription": "pro"},
        )

        # Verify flags() was called with flag_keys_to_evaluate
        mock_flags.assert_called_once()
        call_args = mock_flags.call_args[1]
        self.assertEqual(call_args["flag_keys_to_evaluate"], ["flag1", "flag3"])
        self.assertEqual(
            call_args["person_properties"],
            {"distinct_id": "user123", "subscription": "pro"},
        )

        # Check the result
        self.assertEqual(result["featureFlags"], {"flag1": "variant1", "flag3": True})
        self.assertEqual(
            result["featureFlagPayloads"],
            {"flag1": {"data": "payload1"}, "flag3": {"data": "payload3"}},
        )

    def test_get_all_flags_locally_with_flag_keys_to_evaluate(self):
        """Test that local evaluation with flag_keys_to_evaluate only evaluates specified flags"""
        client = Client(
            project_api_key=FAKE_TEST_API_KEY, personal_api_key=FAKE_TEST_API_KEY
        )

        # Set up multiple flags
        client.feature_flags = [
            {
                "id": 1,
                "key": "flag1",
                "active": True,
                "filters": {"groups": [{"properties": [], "rollout_percentage": 100}]},
            },
            {
                "id": 2,
                "key": "flag2",
                "active": True,
                "filters": {"groups": [{"properties": [], "rollout_percentage": 100}]},
            },
            {
                "id": 3,
                "key": "flag3",
                "active": True,
                "filters": {"groups": [{"properties": [], "rollout_percentage": 100}]},
            },
        ]

        # Call get_all_flags with flag_keys_to_evaluate
        result = client.get_all_flags(
            "user123",
            flag_keys_to_evaluate=["flag1", "flag3"],
            only_evaluate_locally=True,
        )

        # Should only return flag1 and flag3
        self.assertEqual(result, {"flag1": True, "flag3": True})
        self.assertNotIn("flag2", result)
