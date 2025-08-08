import unittest
from unittest import mock

from posthog.client import Client
from posthog.dependency_graph import DependencyGraph
from posthog.test.test_utils import FAKE_TEST_API_KEY


class TestClientFlagDependencies(unittest.TestCase):
    """Test flag dependencies in the PostHog client."""

    @classmethod
    def setUpClass(cls):
        # This ensures no real HTTP POST requests are made
        cls.client_post_patcher = mock.patch("posthog.client.batch_post")
        cls.consumer_post_patcher = mock.patch("posthog.consumer.batch_post")
        cls.client_post_patcher.start()
        cls.consumer_post_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.client_post_patcher.stop()
        cls.consumer_post_patcher.stop()

    def setUp(self):
        self.client = Client(FAKE_TEST_API_KEY, sync_mode=True)

    def test_dependency_graph_building_on_flag_assignment(self):
        """Test that dependency graph is built when flags are assigned."""
        feature_flags = [
            {
                "id": 1,
                "key": "base-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["test@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "key": "dependent-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "1",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        # Assign flags to client
        self.client.feature_flags = feature_flags

        # Check that dependency graph was built
        self.assertIsNotNone(self.client.dependency_graph)
        self.assertIsNotNone(self.client.id_to_key_mapping)
        self.assertIsInstance(self.client.dependency_graph, DependencyGraph)

        # Check that the mapping is correct
        self.assertEqual(self.client.id_to_key_mapping.get("1"), "base-flag")
        self.assertEqual(self.client.id_to_key_mapping.get("2"), "dependent-flag")

        # Check that flags are in the dependency graph
        self.assertIn("base-flag", self.client.dependency_graph.flags)
        self.assertIn("dependent-flag", self.client.dependency_graph.flags)

        # Check that the dependency relationship is correct
        dependencies = self.client.dependency_graph.get_dependencies("dependent-flag")
        self.assertIn("base-flag", dependencies)

    def test_dependency_graph_building_with_empty_flags(self):
        """Test that dependency graph handles empty flags gracefully."""
        self.client.feature_flags = []

        # Check that dependency graph was built but is empty
        self.assertIsNotNone(self.client.dependency_graph)
        self.assertIsNotNone(self.client.id_to_key_mapping)
        self.assertEqual(len(self.client.dependency_graph.flags), 0)
        self.assertEqual(len(self.client.id_to_key_mapping), 0)

    def test_dependency_graph_building_with_no_dependencies(self):
        """Test that dependency graph is built correctly when no flags have dependencies."""
        feature_flags = [
            {
                "id": 1,
                "key": "independent-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["test@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            }
        ]

        self.client.feature_flags = feature_flags

        # Check that dependency graph was built
        self.assertIsNotNone(self.client.dependency_graph)
        self.assertIn("independent-flag", self.client.dependency_graph.flags)

        # Check that no dependencies exist
        dependencies = self.client.dependency_graph.get_dependencies("independent-flag")
        self.assertEqual(len(dependencies), 0)

    @mock.patch("posthog.feature_flags.log")
    def test_dependency_graph_building_error_handling(self, mock_log):
        """Test that dependency graph building errors are handled gracefully."""
        # Create malformed flags that will cause an error
        feature_flags = [
            {
                "id": None,  # Missing ID
                "key": "malformed-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "nonexistent-flag",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            }
        ]

        # This should not raise an exception
        self.client.feature_flags = feature_flags

        # Check that dependency graph was still created (might be empty or partial)
        self.assertIsNotNone(self.client.dependency_graph)
        self.assertIsNotNone(self.client.id_to_key_mapping)

    @mock.patch("posthog.client.evaluate_flags_with_dependencies")
    def test_locally_evaluate_flag_with_dependencies(self, mock_evaluate):
        """Test that _locally_evaluate_flag uses evaluate_flags_with_dependencies when dependencies exist."""
        # Set up return value for the mock
        mock_evaluate.return_value = {"dependent-flag": True}

        # Set up flags with dependencies
        feature_flags = [
            {
                "id": 1,
                "key": "base-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["test@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "key": "dependent-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "1",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        self.client.feature_flags = feature_flags

        # Test flag evaluation
        result = self.client._locally_evaluate_flag(
            "dependent-flag", "test-user", {}, {"email": "test@example.com"}, {}
        )

        # Check that evaluate_flags_with_dependencies was called
        mock_evaluate.assert_called_once()

        # Check the arguments passed to evaluate_flags_with_dependencies
        args, kwargs = mock_evaluate.call_args
        self.assertEqual(args[0], feature_flags)  # feature_flags
        self.assertEqual(args[1], "test-user")  # distinct_id
        self.assertEqual(args[2], {"email": "test@example.com"})  # person_properties
        self.assertEqual(args[3], self.client.cohorts)  # cohort_properties
        self.assertEqual(
            kwargs["requested_flag_keys"], {"dependent-flag"}
        )  # requested_flag_keys

        # Check that the result is returned correctly
        self.assertEqual(result, True)

    def test_locally_evaluate_flag_without_dependencies(self):
        """Test that _locally_evaluate_flag can evaluate flags without dependencies."""
        # Set up flags without dependencies
        feature_flags = [
            {
                "id": 1,
                "key": "independent-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["test@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            }
        ]

        self.client.feature_flags = feature_flags

        # Test flag evaluation - should use dependency-aware evaluation since it's always available
        result = self.client._locally_evaluate_flag(
            "independent-flag", "test-user", {}, {"email": "test@example.com"}, {}
        )

        # Check that the result is correct
        self.assertEqual(result, True)

        # Test with non-matching email
        result = self.client._locally_evaluate_flag(
            "independent-flag", "test-user", {}, {"email": "other@example.com"}, {}
        )

        self.assertEqual(result, False)

    @mock.patch("posthog.client.evaluate_flags_with_dependencies")
    def test_locally_evaluate_flag_dependency_evaluation_fallback(self, mock_evaluate):
        """Test that _locally_evaluate_flag falls back to individual evaluation when dependency evaluation fails."""
        # Set up mock to raise an exception
        mock_evaluate.side_effect = Exception("Dependency evaluation failed")

        # Set up flags with dependencies
        feature_flags = [
            {
                "id": 1,
                "key": "base-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["test@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "key": "dependent-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "1",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        self.client.feature_flags = feature_flags

        # Mock _compute_flag_locally to verify fallback
        with mock.patch.object(
            self.client, "_compute_flag_locally", return_value=False
        ) as mock_compute:
            result = self.client._locally_evaluate_flag(
                "dependent-flag", "test-user", {}, {"email": "test@example.com"}, {}
            )

            # Check that evaluate_flags_with_dependencies was called and failed
            mock_evaluate.assert_called_once()

            # Check that _compute_flag_locally was called as fallback
            mock_compute.assert_called_once()
            self.assertEqual(result, False)

    def test_get_feature_flag_integration_with_dependencies(self):
        """Test the full get_feature_flag flow with flag dependencies."""
        # Set up flags with dependencies
        feature_flags = [
            {
                "id": 1,
                "key": "base-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["test@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "key": "dependent-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "1",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        self.client.feature_flags = feature_flags

        # Test case 1: User with matching email should get both flags as True
        result = self.client.get_feature_flag(
            "dependent-flag",
            "test-user",
            person_properties={"email": "test@example.com"},
            only_evaluate_locally=True,
        )
        self.assertEqual(result, True)

        # Test case 2: User with non-matching email should get both flags as False
        result = self.client.get_feature_flag(
            "dependent-flag",
            "test-user",
            person_properties={"email": "other@example.com"},
            only_evaluate_locally=True,
        )
        self.assertEqual(result, False)

        # Test case 3: Base flag should work independently
        result = self.client.get_feature_flag(
            "base-flag",
            "test-user",
            person_properties={"email": "test@example.com"},
            only_evaluate_locally=True,
        )
        self.assertEqual(result, True)

    def test_complex_dependency_chain(self):
        """Test that complex dependency chains work correctly."""
        # Set up flags with a chain of dependencies: A -> B -> C
        feature_flags = [
            {
                "id": 1,
                "key": "flag-a",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["test@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "key": "flag-b",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "1",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 3,
                "key": "flag-c",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "2",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        self.client.feature_flags = feature_flags

        # Test the end of the chain
        result = self.client.get_feature_flag(
            "flag-c",
            "test-user",
            person_properties={"email": "test@example.com"},
            only_evaluate_locally=True,
        )
        self.assertEqual(result, True)

        # Test with non-matching email (should break the chain)
        result = self.client.get_feature_flag(
            "flag-c",
            "test-user",
            person_properties={"email": "other@example.com"},
            only_evaluate_locally=True,
        )
        self.assertEqual(result, False)

    def test_circular_dependency_handling(self):
        """Test that circular dependencies are handled gracefully."""
        # Set up flags with circular dependencies: A -> B -> A
        feature_flags = [
            {
                "id": 1,
                "key": "flag-a",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "2",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "key": "flag-b",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "1",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        # This should not raise an exception
        self.client.feature_flags = feature_flags

        # The dependency graph should handle cycles gracefully
        self.assertIsNotNone(self.client.dependency_graph)

        # Flag evaluation should work without infinite loops
        result = self.client.get_feature_flag(
            "flag-a",
            "test-user",
            person_properties={"email": "test@example.com"},
            only_evaluate_locally=True,
        )
        # The result should be deterministic (likely False due to cycle handling)
        self.assertIsInstance(result, (bool, type(None)))

    def test_experience_continuity_flag_bypasses_dependency_evaluation(self):
        """Test that experience continuity flags bypass dependency evaluation and use individual evaluation."""
        # Set up flags with dependencies, but mark one with experience continuity
        feature_flags = [
            {
                "id": 1,
                "key": "base-flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "email",
                                    "type": "person",
                                    "value": ["test@example.com"],
                                    "operator": "exact",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "id": 2,
                "key": "experience-continuity-flag",
                "active": True,
                "ensure_experience_continuity": True,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "1",
                                    "type": "flag",
                                    "value": True,
                                    "operator": "flag_evaluates_to",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        self.client.feature_flags = feature_flags

        # Test that the experience continuity flag returns None (indicating it should fall back to /decide)
        result = self.client._locally_evaluate_flag(
            "experience-continuity-flag",
            "test-user",
            {},
            {"email": "test@example.com"},
            {},
        )

        # Experience continuity flags should return None to indicate fallback to /decide
        self.assertIsNone(result)

        # Test that the base flag still works normally
        result = self.client._locally_evaluate_flag(
            "base-flag", "test-user", {}, {"email": "test@example.com"}, {}
        )

        self.assertEqual(result, True)


if __name__ == "__main__":
    unittest.main()
