#!/usr/bin/env python3
"""
Simple test script for PostHog remote config endpoint.
"""

import posthog

# Initialize PostHog client
posthog.api_key = "phc_..."
posthog.personal_api_key = "phs_..."  # or "phx_..."
posthog.host = "http://localhost:8000"  # or "https://us.posthog.com"
posthog.debug = True


def test_remote_config():
    """Test remote config payload retrieval."""
    print("Testing remote config endpoint...")

    # Test feature flag key - replace with an actual flag key from your project
    flag_key = "unencrypted-remote-config-setting"

    try:
        # Get remote config payload
        payload = posthog.get_remote_config_payload(flag_key)
        print(f"✅ Success! Remote config payload for '{flag_key}': {payload}")

    except Exception as e:
        print(f"❌ Error getting remote config: {e}")


if __name__ == "__main__":
    test_remote_config()
