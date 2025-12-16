"""
Example demonstrating real-time feature flags with callbacks.

This example shows how to:
1. Enable real-time feature flags
2. Listen to flag updates with a callback
3. React to flag changes in your application
"""

from posthog import Posthog
import time


def on_flag_update(flag_key, flag_data):
    """
    Callback function that gets called whenever a feature flag is updated.

    Args:
        flag_key: The key of the flag that was updated
        flag_data: The full flag data (includes 'deleted' field if deleted)
    """
    if flag_data.get("deleted"):
        print(f"🗑️  Flag '{flag_key}' was deleted")
    else:
        is_active = flag_data.get("active", False)
        status = "✅ active" if is_active else "❌ inactive"
        print(f"🔄 Flag '{flag_key}' was updated - {status}")
        print(f"   Name: {flag_data.get('name')}")
        print(f"   ID: {flag_data.get('id')}")


# Initialize PostHog with real-time flags enabled
posthog = Posthog(
    project_api_key="<your_project_api_key>",
    personal_api_key="<your_personal_api_key>",  # Required for real-time flags
    host="https://us.i.posthog.com",  # Or your self-hosted instance
    realtime_flags=True,
    on_feature_flags_update=on_flag_update,
    debug=True,  # Enable debug logging to see connection status
)

# Load feature flags (this will also establish the SSE connection)
posthog.load_feature_flags()

print("🚀 Real-time feature flags enabled!")
print("📡 Listening for flag updates...")
print("💡 Try updating a flag in the PostHog UI and watch it update here in real-time!")
print("\nPress Ctrl+C to stop\n")

try:
    # Keep the script running to receive updates
    while True:
        # Your application logic here
        # You can check flags as normal, they will be updated automatically
        time.sleep(1)
except KeyboardInterrupt:
    print("\n\n👋 Shutting down...")
    posthog.shutdown()
    print("✅ Cleanup complete")
