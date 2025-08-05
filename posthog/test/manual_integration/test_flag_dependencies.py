#!/usr/bin/env python3
"""
Integration test for feature flag dependencies.

This test validates that flag dependencies work correctly when evaluated locally
against a real PostHog instance. It creates its own test flags and cleans them up
after the test completes, making it fully self-contained.

Requirements:
- A running PostHog instance (default: http://localhost:8000)
- Valid API keys for the test project with permissions to create/delete flags

Configuration:
- Set environment variables to override defaults:
  - POSTHOG_HOST: PostHog instance URL
  - POSTHOG_API_KEY: Project API key
  - POSTHOG_PERSONAL_API_KEY: Personal API key for feature flag management
  - POSTHOG_TEST_EMAIL: Email that should enable both flags
  - POSTHOG_TEST_EMAIL_DISABLED: Email that should disable both flags

Usage:
  python -m posthog.test.integrations.test_flag_dependencies
"""

import os
import sys
import unittest
import requests
import time
import uuid
from typing import Dict, Any, Optional, List

# Add the parent directory to the path to import posthog
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import posthog


class PostHogAPIHelper:
    """Helper class for PostHog API operations."""

    def __init__(self, host: str, personal_api_key: str, debug: bool = False):
        self.host = host.rstrip("/")
        self.personal_api_key = personal_api_key
        self.debug = debug
        self.headers = {
            "Authorization": f"Bearer {personal_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "PostHog-Python-SDK-Integration-Test",
        }
        self.project_id = None

    def get_project_id(self) -> Optional[str]:
        """Get the project ID for the current API key."""
        if self.project_id:
            return self.project_id

        try:
            url = f"{self.host}/api/projects/"
            if self.debug:
                print(f"🔍 Getting project ID from: {url}")
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()

            projects = response.json()
            if self.debug:
                print(f"🔍 Projects response: {projects}")
            if projects and "results" in projects:
                # Use the first project
                self.project_id = str(projects["results"][0]["id"])
                return self.project_id
            elif projects and len(projects) > 0:
                # Direct array response
                self.project_id = str(projects[0]["id"])
                return self.project_id
            else:
                print("❌ No projects found for this API key")
                return None

        except Exception as e:
            print(f"❌ Error getting project ID: {e}")
            if hasattr(e, "response") and e.response:
                print(f"Response status: {e.response.status_code}")
                print(f"Response text: {e.response.text}")
            return None

    def create_feature_flag(self, key: str, name: str, filters: Dict) -> Optional[Dict]:
        """Create a feature flag via PostHog API."""
        project_id = self.get_project_id()
        if not project_id:
            return None

        flag_data = {"key": key, "name": name, "active": True, "filters": filters}

        try:
            response = requests.post(
                f"{self.host}/api/projects/{project_id}/feature_flags/",
                headers=self.headers,
                json=flag_data,
                timeout=30,
            )
            response.raise_for_status()

            flag = response.json()
            if self.debug:
                print(f"✅ Created flag: {key} (ID: {flag.get('id')})")
            return flag

        except Exception as e:
            print(f"❌ Error creating flag {key}: {e}")
            if hasattr(e, "response") and e.response:
                print(f"Response: {e.response.text}")
            return None

    def delete_feature_flag(self, flag_id: str) -> bool:
        """Delete (soft delete) a feature flag via PostHog API."""
        project_id = self.get_project_id()
        if not project_id:
            return False

        try:
            # PostHog uses soft deletion - set deleted=true
            response = requests.patch(
                f"{self.host}/api/projects/{project_id}/feature_flags/{flag_id}/",
                headers=self.headers,
                json={"deleted": True},
                timeout=30,
            )
            response.raise_for_status()

            if self.debug:
                print(f"✅ Deleted flag ID: {flag_id}")
            return True

        except Exception as e:
            print(f"❌ Error deleting flag {flag_id}: {e}")
            return False

    def list_feature_flags(self) -> List[Dict]:
        """List all feature flags for the project."""
        project_id = self.get_project_id()
        if not project_id:
            return []

        try:
            response = requests.get(
                f"{self.host}/api/projects/{project_id}/feature_flags/",
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()

            flags = response.json()
            if "results" in flags:
                return flags["results"]
            return flags if isinstance(flags, list) else []

        except Exception as e:
            print(f"❌ Error listing flags: {e}")
            return []


class FlagDependenciesIntegrationTest(unittest.TestCase):
    """Integration test for feature flag dependencies."""

    @classmethod
    def setUpClass(cls):
        """Set up the test environment."""
        # Configuration with environment variable overrides
        cls.API_KEY = os.environ.get(
            "POSTHOG_API_KEY", "YOUR_API_KEY_HERE"
        )
        cls.FEATURE_FLAGS_API_KEY = os.environ.get(
            "POSTHOG_FEATURE_FLAGS_API_KEY",
            "YOUR_FEATURE_FLAGS_API_KEY_HERE",
        )
        cls.PERSONAL_API_KEY = os.environ.get("POSTHOG_PERSONAL_API_KEY", "")
        cls.POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "http://localhost:8000")
        cls.TEST_EMAIL_ENABLED = os.environ.get(
            "POSTHOG_TEST_EMAIL", "phil.h@posthog.com"
        )
        cls.TEST_EMAIL_DISABLED = os.environ.get(
            "POSTHOG_TEST_EMAIL_DISABLED", "other@example.com"
        )

        # Generate unique flag keys for this test run
        test_run_id = str(uuid.uuid4())[:8]
        cls.BASE_FLAG_KEY = f"test-base-flag-{test_run_id}"
        cls.DEPENDENT_FLAG_KEY = f"test-dependent-flag-{test_run_id}"

        # Track created flags for cleanup
        cls.created_flags = []

        # Check if debug mode is enabled
        debug_mode = os.environ.get("POSTHOG_DEBUG", "").lower() in ("true", "1", "yes")

        # Initialize API helper
        cls.api_helper = PostHogAPIHelper(
            cls.POSTHOG_HOST, cls.PERSONAL_API_KEY, debug=debug_mode
        )

        # Create PostHog client
        cls.client = posthog.Client(
            project_api_key=cls.API_KEY,
            host=cls.POSTHOG_HOST,
            debug=debug_mode,
            sync_mode=True,
            personal_api_key=cls.FEATURE_FLAGS_API_KEY,  # Use feature flags API key for local evaluation
            feature_flags_request_timeout_seconds=10,
            poll_interval=0,  # Disable polling for tests
            send=False,  # Prevent actual event sending
        )

        # Enable local evaluation
        cls.client.enable_local_evaluation = True

        if debug_mode:
            print(f"🚀 Setting up integration test")
            print(f"📍 PostHog Host: {cls.POSTHOG_HOST}")
            print(f"🔑 Project API Key: {cls.API_KEY[:20]}...")
            print(f"🔑 Feature Flags API Key: {cls.FEATURE_FLAGS_API_KEY[:8]}...")
            print(
                f"🔑 Personal API Key: {cls.PERSONAL_API_KEY[:8] if cls.PERSONAL_API_KEY else 'Not provided'}..."
            )
            print(f"📧 Test Email (enabled): {cls.TEST_EMAIL_ENABLED}")
            print(f"📧 Test Email (disabled): {cls.TEST_EMAIL_DISABLED}")
            print(f"🏷️  Base Flag Key: {cls.BASE_FLAG_KEY}")
            print(f"🏷️  Dependent Flag Key: {cls.DEPENDENT_FLAG_KEY}")

        # Validate that personal API key is provided
        if not cls.PERSONAL_API_KEY:
            raise Exception(
                "Personal API key is required for flag management operations. Please set POSTHOG_PERSONAL_API_KEY environment variable."
            )

        # Test connectivity before creating flags
        if not cls._test_connectivity(debug_mode):
            raise Exception(
                "Cannot connect to PostHog instance - check host and API keys"
            )

        # Create test flags
        cls._create_test_flags(debug_mode)

    @classmethod
    def _test_connectivity(cls, debug_mode: bool = False) -> bool:
        """Test basic connectivity to PostHog instance."""
        try:
            if debug_mode:
                print("🔍 Testing connectivity to PostHog instance...")

            # Test basic connectivity
            response = requests.get(
                f"{cls.POSTHOG_HOST}/api/projects/",
                headers={
                    "Authorization": f"Bearer {cls.PERSONAL_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )

            if response.status_code == 200:
                if debug_mode:
                    print("✅ Successfully connected to PostHog instance")
                return True
            else:
                print(f"❌ HTTP {response.status_code}: {response.text}")
                return False

        except requests.exceptions.ConnectionError:
            print(
                f"❌ Connection failed - PostHog instance may not be running at {cls.POSTHOG_HOST}"
            )
            return False
        except requests.exceptions.Timeout:
            print(
                f"❌ Connection timeout - PostHog instance at {cls.POSTHOG_HOST} is not responding"
            )
            return False
        except Exception as e:
            print(f"❌ Connectivity test failed: {e}")
            return False

    @classmethod
    def _sort_flags_for_deletion(cls, flags: List[Dict]) -> List[Dict]:
        """Sort flags for deletion to avoid dependency warnings.

        Returns flags in order where dependent flags come before their dependencies.
        For our simple two-flag case, this means: dependent flag first, then base flag.
        """
        if len(flags) <= 1:
            return flags

        # For our current test structure: base flag (index 0), dependent flag (index 1)
        # We want to delete dependent first, then base
        # Simple reversal works for our linear dependency chain
        return list(reversed(flags))

    @classmethod
    def tearDownClass(cls):
        """Clean up after all tests."""
        print(f"\n🧹 Cleaning up test flags...")

        # Sort flags for proper deletion order (dependent flags before their dependencies)
        flags_to_delete = cls._sort_flags_for_deletion(cls.created_flags)

        # Delete flags in the correct order
        for flag_info in flags_to_delete:
            if "id" in flag_info:
                cls.api_helper.delete_feature_flag(str(flag_info["id"]))

        # Shutdown client
        if hasattr(cls, "client"):
            try:
                cls.client.shutdown()
            except Exception as e:
                print(f"⚠️  Client shutdown warning: {e}")

        print("✅ Cleanup completed")

    @classmethod
    def _create_test_flags(cls, debug_mode: bool = False):
        """Create the test flags via PostHog API."""
        if debug_mode:
            print("\n📋 Creating test flags...")

        # Create base flag - enabled for specific email
        base_flag_filters = {
            "groups": [
                {
                    "properties": [
                        {
                            "key": "email",
                            "type": "person",
                            "value": [cls.TEST_EMAIL_ENABLED],
                            "operator": "exact",
                        }
                    ],
                    "rollout_percentage": 100,
                }
            ]
        }

        base_flag = cls.api_helper.create_feature_flag(
            cls.BASE_FLAG_KEY, f"Integration Test Base Flag", base_flag_filters
        )

        if base_flag:
            cls.created_flags.append(base_flag)
            base_flag_id = base_flag.get("id")

            # Create dependent flag - depends on base flag being true
            dependent_flag_filters = {
                "groups": [
                    {
                        "properties": [
                            {
                                "key": str(base_flag_id),  # Depends on base flag ID
                                "type": "flag",
                                "value": True,
                                "operator": "flag_evaluates_to",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ]
            }

            dependent_flag = cls.api_helper.create_feature_flag(
                cls.DEPENDENT_FLAG_KEY,
                f"Integration Test Dependent Flag",
                dependent_flag_filters,
            )

            if dependent_flag:
                cls.created_flags.append(dependent_flag)
                if debug_mode:
                    print(
                        f"✅ Created dependency: {cls.DEPENDENT_FLAG_KEY} depends on {cls.BASE_FLAG_KEY}"
                    )
            else:
                raise Exception("Failed to create dependent flag")
        else:
            print(
                "❌ Failed to create base flag - PostHog instance may not be running or API keys may be invalid"
            )
            print(f"   Host: {cls.POSTHOG_HOST}")
            print(f"   Personal API Key: {cls.PERSONAL_API_KEY[:8]}...")
            raise Exception(
                "Failed to create base flag - check PostHog instance and API keys"
            )

        # Wait a moment for flags to be available
        if debug_mode:
            print("⏳ Waiting for flags to be available...")
        time.sleep(2)

    def setUp(self):
        """Set up each test."""
        # Load feature flags before each test
        try:
            self.client.load_feature_flags()
            debug_mode = os.environ.get("POSTHOG_DEBUG", "").lower() in (
                "true",
                "1",
                "yes",
            )
            if debug_mode:
                print("✅ Feature flags loaded successfully")
        except Exception as e:
            self.fail(f"Failed to load feature flags: {e}")

    def test_flag_dependencies_enabled_user(self):
        """Test that flag dependencies work for a user who should have both flags enabled."""
        person_properties = {"email": self.TEST_EMAIL_ENABLED}
        distinct_id = (
            f"user_{self.TEST_EMAIL_ENABLED.replace('@', '_').replace('.', '_')}"
        )

        # Test the base flag
        base_flag_result = self.client.get_feature_flag(
            self.BASE_FLAG_KEY,
            distinct_id=distinct_id,
            person_properties=person_properties,
            only_evaluate_locally=True,
            send_feature_flag_events=False,
        )

        # Test the dependent flag
        dependent_flag_result = self.client.get_feature_flag(
            self.DEPENDENT_FLAG_KEY,
            distinct_id=distinct_id,
            person_properties=person_properties,
            only_evaluate_locally=True,
            send_feature_flag_events=False,
        )

        # Assertions
        self.assertTrue(
            base_flag_result,
            f"Base flag should be enabled for {self.TEST_EMAIL_ENABLED}",
        )
        self.assertTrue(
            dependent_flag_result,
            f"Dependent flag should be enabled for {self.TEST_EMAIL_ENABLED}",
        )

        debug_mode = os.environ.get("POSTHOG_DEBUG", "").lower() in ("true", "1", "yes")
        if debug_mode:
            print(
                f"✅ {self.TEST_EMAIL_ENABLED}: {self.BASE_FLAG_KEY}={base_flag_result}, {self.DEPENDENT_FLAG_KEY}={dependent_flag_result}"
            )

    def test_flag_dependencies_disabled_user(self):
        """Test that flag dependencies work for a user who should have both flags disabled."""
        person_properties = {"email": self.TEST_EMAIL_DISABLED}
        distinct_id = (
            f"user_{self.TEST_EMAIL_DISABLED.replace('@', '_').replace('.', '_')}"
        )

        # Test the base flag
        base_flag_result = self.client.get_feature_flag(
            self.BASE_FLAG_KEY,
            distinct_id=distinct_id,
            person_properties=person_properties,
            only_evaluate_locally=True,
            send_feature_flag_events=False,
        )

        # Test the dependent flag
        dependent_flag_result = self.client.get_feature_flag(
            self.DEPENDENT_FLAG_KEY,
            distinct_id=distinct_id,
            person_properties=person_properties,
            only_evaluate_locally=True,
            send_feature_flag_events=False,
        )

        # Assertions
        self.assertFalse(
            base_flag_result,
            f"Base flag should be disabled for {self.TEST_EMAIL_DISABLED}",
        )
        self.assertFalse(
            dependent_flag_result,
            f"Dependent flag should be disabled for {self.TEST_EMAIL_DISABLED}",
        )

        debug_mode = os.environ.get("POSTHOG_DEBUG", "").lower() in ("true", "1", "yes")
        if debug_mode:
            print(
                f"✅ {self.TEST_EMAIL_DISABLED}: {self.BASE_FLAG_KEY}={base_flag_result}, {self.DEPENDENT_FLAG_KEY}={dependent_flag_result}"
            )

    def test_dependency_graph_building(self):
        """Test that the dependency graph is properly built."""
        # Ensure flags are loaded
        self.assertIsNotNone(
            self.client.feature_flags, "Feature flags should be loaded"
        )
        self.assertIsNotNone(
            self.client.dependency_graph, "Dependency graph should be built"
        )
        self.assertIsNotNone(
            self.client.id_to_key_mapping, "ID to key mapping should be built"
        )

        # Check that our test flags are in the graph
        self.assertIn(
            self.BASE_FLAG_KEY,
            self.client.dependency_graph.flags,
            "Base flag should be in dependency graph",
        )
        self.assertIn(
            self.DEPENDENT_FLAG_KEY,
            self.client.dependency_graph.flags,
            "Dependent flag should be in dependency graph",
        )

        # Check that the dependency relationship exists
        dependencies = self.client.dependency_graph.get_dependencies(
            self.DEPENDENT_FLAG_KEY
        )
        self.assertIn(
            self.BASE_FLAG_KEY,
            dependencies,
            f"Dependent flag should depend on base flag",
        )

        debug_mode = os.environ.get("POSTHOG_DEBUG", "").lower() in ("true", "1", "yes")
        if debug_mode:
            print("✅ Dependency graph is properly built")

    def test_flag_evaluation_consistency(self):
        """Test that flag evaluation is consistent across multiple calls."""
        person_properties = {"email": self.TEST_EMAIL_ENABLED}
        distinct_id = (
            f"user_{self.TEST_EMAIL_ENABLED.replace('@', '_').replace('.', '_')}"
        )

        # Evaluate the same flag multiple times
        results = []
        for i in range(5):
            result = self.client.get_feature_flag(
                self.DEPENDENT_FLAG_KEY,
                distinct_id=distinct_id,
                person_properties=person_properties,
                only_evaluate_locally=True,
                send_feature_flag_events=False,
            )
            results.append(result)

        # All results should be the same
        self.assertTrue(
            all(r == results[0] for r in results),
            f"Flag evaluation should be consistent across calls: {results}",
        )

        debug_mode = os.environ.get("POSTHOG_DEBUG", "").lower() in ("true", "1", "yes")
        if debug_mode:
            print(f"✅ Flag evaluation is consistent: {results[0]}")

    def test_api_flag_creation_cleanup(self):
        """Test that we can create and clean up flags via API."""
        # This test verifies our API helper works correctly
        test_flag_key = f"test-cleanup-flag-{uuid.uuid4().hex[:6]}"

        # Create a temporary flag
        test_flag = self.api_helper.create_feature_flag(
            test_flag_key,
            "Test Cleanup Flag",
            {"groups": [{"properties": [], "rollout_percentage": 100}]},
        )

        self.assertIsNotNone(test_flag, "Test flag should be created")
        self.assertIn("id", test_flag, "Test flag should have an ID")

        # Clean up the temporary flag
        success = self.api_helper.delete_feature_flag(str(test_flag["id"]))
        self.assertTrue(success, "Test flag should be deleted successfully")

        debug_mode = os.environ.get("POSTHOG_DEBUG", "").lower() in ("true", "1", "yes")
        if debug_mode:
            print(f"✅ API flag creation and cleanup working correctly")


def run_integration_tests():
    """Run the integration tests."""
    # Create a test suite
    suite = unittest.TestLoader().loadTestsFromTestCase(FlagDependenciesIntegrationTest)

    # Run the tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # Print summary
    print(f"\n{'=' * 60}")
    if result.wasSuccessful():
        print("🎉 All integration tests PASSED!")
        print("✅ Flag dependencies are working correctly")
    else:
        print("💥 Some integration tests FAILED!")
        print("❌ Flag dependencies may not be working as expected")
        if result.failures:
            print("\nFailures:")
            for test, traceback in result.failures:
                print(f"  {test}: {traceback}")
        if result.errors:
            print("\nErrors:")
            for test, traceback in result.errors:
                print(f"  {test}: {traceback}")

    print(f"{'=' * 60}")

    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_integration_tests()
    sys.exit(0 if success else 1)
