import unittest

from posthog.dependency_graph import (
    DependencyGraph,
    build_dependency_graph,
    evaluate_flags_with_dependencies,
    extract_flag_dependencies,
    match_flag_dependency,
)


class TestDependencyGraph(unittest.TestCase):
    """Test dependency graph functionality."""

    def test_match_flag_dependency(self):
        """Test flag dependency value matching logic"""
        # Test boolean true matches any enabled state
        self.assertTrue(match_flag_dependency(True, True))
        self.assertTrue(match_flag_dependency(True, "variant"))
        self.assertFalse(match_flag_dependency(True, False))

        # Test boolean false matches only disabled state
        self.assertTrue(match_flag_dependency(False, False))
        self.assertFalse(match_flag_dependency(False, True))
        self.assertFalse(match_flag_dependency(False, "variant"))

        # Test string value matches exact variant
        self.assertTrue(match_flag_dependency("variant", "variant"))
        self.assertFalse(match_flag_dependency("variant", "other"))
        self.assertFalse(match_flag_dependency("variant", True))
        self.assertFalse(match_flag_dependency("variant", False))

    def test_extract_flag_dependencies(self):
        """Test extraction of flag dependencies from feature flag definition"""
        # Flag with no dependencies
        flag_no_deps = {
            "key": "flag-a",
            "filters": {
                "groups": [
                    {
                        "properties": [
                            {"key": "country", "value": "US", "type": "person"}
                        ]
                    }
                ]
            },
        }
        self.assertEqual(extract_flag_dependencies(flag_no_deps), set())

        # Flag with single dependency using flag ID
        flag_single_dep = {
            "key": "flag-b",
            "id": 2,
            "filters": {
                "groups": [
                    {
                        "properties": [{"key": "1", "value": True, "type": "flag"}]
                    }  # depends on flag ID 1
                ]
            },
        }
        self.assertEqual(extract_flag_dependencies(flag_single_dep), {"1"})

        # Flag with multiple dependencies using flag IDs
        flag_multi_deps = {
            "key": "flag-c",
            "id": 3,
            "filters": {
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "1",
                                "value": True,
                                "type": "flag",
                            },  # depends on flag ID 1
                            {
                                "key": "2",
                                "value": "variant",
                                "type": "flag",
                            },  # depends on flag ID 2
                            {"key": "country", "value": "US", "type": "person"},
                        ]
                    }
                ]
            },
        }
        self.assertEqual(extract_flag_dependencies(flag_multi_deps), {"1", "2"})

    def test_dependency_graph_basic(self):
        """Test basic dependency graph operations"""
        graph = DependencyGraph()

        # Test adding flags and dependencies
        graph.add_flag("flag-a")
        graph.add_flag("flag-b")
        graph.add_dependency("flag-b", "flag-a")  # flag-b depends on flag-a

        self.assertEqual(graph.flags, {"flag-a", "flag-b"})
        self.assertEqual(graph.get_dependencies("flag-b"), {"flag-a"})
        self.assertEqual(graph.get_dependents("flag-a"), {"flag-b"})

    def test_dependency_graph_topological_sort(self):
        """Test topological sorting of dependency graph"""
        graph = DependencyGraph()

        # Create a chain: flag-c depends on flag-b depends on flag-a
        graph.add_flag("flag-a")
        graph.add_flag("flag-b")
        graph.add_flag("flag-c")
        graph.add_dependency("flag-b", "flag-a")
        graph.add_dependency("flag-c", "flag-b")

        sorted_flags = graph.topological_sort()

        # Dependencies should come first
        self.assertEqual(sorted_flags.index("flag-a"), 0)
        self.assertEqual(sorted_flags.index("flag-b"), 1)
        self.assertEqual(sorted_flags.index("flag-c"), 2)

    def test_dependency_graph_cycle_detection(self):
        """Test cycle detection in dependency graph"""
        graph = DependencyGraph()

        # Create a cycle: flag-a depends on flag-b depends on flag-a
        graph.add_flag("flag-a")
        graph.add_flag("flag-b")
        graph.add_dependency("flag-a", "flag-b")
        graph.add_dependency("flag-b", "flag-a")

        self.assertTrue(graph.has_cycles())

        cycle_flags = graph.detect_cycles()
        self.assertIn("flag-a", cycle_flags)
        self.assertIn("flag-b", cycle_flags)

    def test_dependency_graph_remove_cycles(self):
        """Test cycle removal from dependency graph"""
        graph = DependencyGraph()

        # Create a cycle plus an independent flag
        graph.add_flag("flag-a")
        graph.add_flag("flag-b")
        graph.add_flag("flag-c")
        graph.add_dependency("flag-a", "flag-b")
        graph.add_dependency("flag-b", "flag-a")

        self.assertTrue(graph.has_cycles())

        removed_flags = graph.remove_cycles()
        self.assertEqual(len(removed_flags), 2)
        self.assertIn("flag-a", removed_flags)
        self.assertIn("flag-b", removed_flags)

        # Only flag-c should remain
        self.assertEqual(graph.flags, {"flag-c"})

    def test_dependency_graph_filter_by_keys(self):
        """Test filtering dependency graph by requested keys"""
        graph = DependencyGraph()

        # Create chain: flag-d depends on flag-c depends on flag-b depends on flag-a
        graph.add_flag("flag-a")
        graph.add_flag("flag-b")
        graph.add_flag("flag-c")
        graph.add_flag("flag-d")
        graph.add_dependency("flag-b", "flag-a")
        graph.add_dependency("flag-c", "flag-b")
        graph.add_dependency("flag-d", "flag-c")

        # Filter to only include flag-c and its dependencies
        filtered = graph.filter_by_keys({"flag-c"})

        # Should include flag-c and its dependencies (flag-a, flag-b)
        self.assertEqual(filtered.flags, {"flag-a", "flag-b", "flag-c"})

        # Dependencies should be preserved
        self.assertEqual(filtered.get_dependencies("flag-b"), {"flag-a"})
        self.assertEqual(filtered.get_dependencies("flag-c"), {"flag-b"})

    def test_dependency_graph_caching(self):
        """Test dependency graph result caching"""
        graph = DependencyGraph()

        # Test caching
        graph.cache_result("flag-a", True)
        graph.cache_result("flag-b", "variant")

        self.assertEqual(graph.get_cached_result("flag-a"), True)
        self.assertEqual(graph.get_cached_result("flag-b"), "variant")
        self.assertIsNone(graph.get_cached_result("flag-c"))

        # Test cache clearing
        graph.clear_cache()
        self.assertIsNone(graph.get_cached_result("flag-a"))
        self.assertIsNone(graph.get_cached_result("flag-b"))

    def test_build_dependency_graph(self):
        """Test building dependency graph from feature flags"""
        feature_flags = [
            {"key": "flag-a", "id": 1, "filters": {"groups": [{"properties": []}]}},
            {
                "key": "flag-b",
                "id": 2,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "1",
                                    "value": True,
                                    "type": "flag",
                                }  # depends on flag ID 1
                            ]
                        }
                    ]
                },
            },
            {
                "key": "flag-c",
                "id": 3,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "2",
                                    "value": "variant",
                                    "type": "flag",
                                }  # depends on flag ID 2
                            ]
                        }
                    ]
                },
            },
        ]

        graph, id_to_key = build_dependency_graph(feature_flags)

        self.assertEqual(graph.flags, {"flag-a", "flag-b", "flag-c"})
        self.assertEqual(graph.get_dependencies("flag-b"), {"flag-a"})
        self.assertEqual(graph.get_dependencies("flag-c"), {"flag-b"})
        self.assertEqual(id_to_key, {"1": "flag-a", "2": "flag-b", "3": "flag-c"})

    def test_build_dependency_graph_missing_dependencies(self):
        """Test building dependency graph with missing dependencies"""
        feature_flags = [
            {"key": "flag-a", "id": 1, "filters": {"groups": [{"properties": []}]}},
            {
                "key": "flag-b",
                "id": 2,
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "999",  # Non-existent flag ID
                                    "value": True,
                                    "type": "flag",
                                }
                            ]
                        }
                    ]
                },
            },
        ]

        graph, id_to_key = build_dependency_graph(feature_flags)

        # Should still build graph with available flags
        self.assertEqual(graph.flags, {"flag-a", "flag-b"})
        # No dependencies should be added for missing flag
        self.assertEqual(graph.get_dependencies("flag-b"), set())
        self.assertEqual(id_to_key, {"1": "flag-a", "2": "flag-b"})

    def test_evaluate_flags_with_dependencies(self):
        """Test end-to-end flag evaluation with dependencies"""
        feature_flags = [
            {
                "key": "base-flag",
                "filters": {"groups": [{"properties": [], "rollout_percentage": 100}]},
            },
            {
                "key": "dependent-flag",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {"key": "base-flag", "value": True, "type": "flag"}
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        # Evaluate flags
        results = evaluate_flags_with_dependencies(feature_flags, "test-user", {})

        # Both flags should be enabled
        self.assertEqual(results["base-flag"], True)
        self.assertEqual(results["dependent-flag"], True)

    def test_evaluate_flags_with_dependencies_false_dependency(self):
        """Test flag evaluation when dependency is false"""
        feature_flags = [
            {
                "key": "base-flag",
                "filters": {
                    "groups": [
                        {"properties": [], "rollout_percentage": 0}  # Always disabled
                    ]
                },
            },
            {
                "key": "dependent-flag",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {"key": "base-flag", "value": True, "type": "flag"}
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        # Evaluate flags
        results = evaluate_flags_with_dependencies(feature_flags, "test-user", {})

        # Base flag should be disabled, dependent flag should be disabled too
        self.assertEqual(results["base-flag"], False)
        self.assertEqual(results["dependent-flag"], False)

    def test_evaluate_flags_with_variant_dependency(self):
        """Test flag evaluation with variant dependency"""
        feature_flags = [
            {
                "key": "base-flag",
                "filters": {
                    "groups": [
                        {
                            "properties": [],
                            "rollout_percentage": 100,
                            "variant": "test-variant",
                        }
                    ],
                    "multivariate": {
                        "variants": [{"key": "test-variant", "rollout_percentage": 100}]
                    },
                },
            },
            {
                "key": "dependent-flag",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {
                                    "key": "base-flag",
                                    "value": "test-variant",
                                    "type": "flag",
                                }
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
        ]

        # Evaluate flags
        results = evaluate_flags_with_dependencies(feature_flags, "test-user", {})

        # Base flag should return variant, dependent flag should be enabled
        self.assertEqual(results["base-flag"], "test-variant")
        self.assertEqual(results["dependent-flag"], True)

    def test_evaluate_flags_with_dependencies_filtered_keys(self):
        """Test flag evaluation with filtered keys"""
        feature_flags = [
            {
                "key": "flag-a",
                "filters": {"groups": [{"properties": [], "rollout_percentage": 100}]},
            },
            {
                "key": "flag-b",
                "filters": {
                    "groups": [
                        {
                            "properties": [
                                {"key": "flag-a", "value": True, "type": "flag"}
                            ],
                            "rollout_percentage": 100,
                        }
                    ]
                },
            },
            {
                "key": "flag-c",
                "filters": {"groups": [{"properties": [], "rollout_percentage": 100}]},
            },
        ]

        # Evaluate only flag-b and its dependencies
        results = evaluate_flags_with_dependencies(
            feature_flags, "test-user", {}, requested_flag_keys={"flag-b"}
        )

        # Should evaluate flag-a (dependency) and flag-b, but not flag-c
        self.assertEqual(results["flag-a"], True)
        self.assertEqual(results["flag-b"], True)
        self.assertNotIn("flag-c", results)


if __name__ == "__main__":
    unittest.main()
