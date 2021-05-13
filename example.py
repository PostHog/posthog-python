# PostHog Python library example

# Import the library
import time

import posthog

# You can find this key on the /setup page in PostHog
posthog.api_key = ""
posthog.personal_api_key = ""

# Where you host PostHog, with no trailing /.
# You can remove this line if you're using posthog.com
posthog.host = "http://127.0.0.1:8000"

# Capture an event
posthog.capture("distinct_id", "event", {"property1": "value", "property2": "value"})

print(posthog.feature_enabled("beta-feature", "distinct_id"))

print("sleeping")
time.sleep(5)

print(posthog.feature_enabled("beta-feature", "distinct_id"))

# # Alias a previous distinct id with a new one

posthog.alias("distinct_id", "new_distinct_id")

posthog.capture("new_distinct_id", "event2", {"property1": "value", "property2": "value"})

# # Add properties to the person
posthog.identify("new_distinct_id", {"email": "something@something.com"})

# properties set only once to the person
posthog.set_once("new_distinct_id", {"self_serve_signup": True})

time.sleep(3)

posthog.set_once(
    "new_distinct_id", {"self_serve_signup": False}
)  # this will not change the property (because it was already set)

posthog.set("new_distinct_id", {"current_browser": "Chrome"})
posthog.set("new_distinct_id", {"current_browser": "Firefox"})

# posthog.shutdown()
