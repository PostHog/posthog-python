# PostHog Python library example

# Import the library
import time

import posthog

# You can find this key on the /setup page in PostHog
posthog.project_api_key = ""
posthog.personal_api_key = ""

# Where you host PostHog, with no trailing /.
# You can remove this line if you're using posthog.com
posthog.host = "http://localhost:8000"

# Capture an event
posthog.capture("distinct_id", "event", {"property1": "value", "property2": "value"}, send_feature_flags=True)

print(posthog.feature_enabled("beta-feature", "distinct_id"))
print(posthog.feature_enabled("beta-feature", "distinct_id", groups={"company": "id:5"}))

print("sleeping")
time.sleep(5)

print(posthog.feature_enabled("beta-feature", "distinct_id"))

# # Alias a previous distinct id with a new one

posthog.alias("distinct_id", "new_distinct_id")

posthog.capture("new_distinct_id", "event2", {"property1": "value", "property2": "value"})
posthog.capture(
    "new_distinct_id", "event-with-groups", {"property1": "value", "property2": "value"}, groups={"company": "id:5"}
)

# # Add properties to the person
posthog.identify("new_distinct_id", {"email": "something@something.com"})

# Add properties to a group
posthog.group_identify("company", "id:5", {"employees": 11})

# properties set only once to the person
posthog.set_once("new_distinct_id", {"self_serve_signup": True})

time.sleep(3)

posthog.set_once(
    "new_distinct_id", {"self_serve_signup": False}
)  # this will not change the property (because it was already set)

posthog.set("new_distinct_id", {"current_browser": "Chrome"})
posthog.set("new_distinct_id", {"current_browser": "Firefox"})

# posthog.shutdown()

# #############################################################################
# Make sure you have a personal API key for the examples below

# Local Evaluation

# If flag has City=Sydney, this call doesn't go to `/decide`
print(posthog.feature_enabled("test-flag", "distinct_id_random_22", person_properties={"$geoip_city_name": "Sydney"}))

print(posthog.get_all_flags("distinct_id_random_22"))
