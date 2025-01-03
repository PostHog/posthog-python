# PostHog Python library example

# Import the library
# import time

import posthog

posthog.debug = True

# You can find this key on the /setup page in PostHog
posthog.project_api_key = "phc_gtWmTq3Pgl06u4sZY3TRcoQfp42yfuXHKoe8ZVSR6Kh"
posthog.personal_api_key = "phx_fiRCOQkTA3o2ePSdLrFDAILLHjMu2Mv52vUi8MNruIm"

# Where you host PostHog, with no trailing /.
# You can remove this line if you're using posthog.com
posthog.host = "http://localhost:8000"
posthog.poll_interval = 10

print(
    posthog.feature_enabled(
        "person-on-events-enabled",
        "12345",
        groups={"organization": str("0182ee91-8ef7-0000-4cb9-fedc5f00926a")},
        group_properties={
            "organization": {
                "id": "0182ee91-8ef7-0000-4cb9-fedc5f00926a",
                "created_at": "2022-06-30 11:44:52.984121+00:00",
            }
        },
        only_evaluate_locally=True,
    )
)


# Capture an event
posthog.capture("distinct_id", "event", {"property1": "value", "property2": "value"}, send_feature_flags=True)

print(posthog.feature_enabled("beta-feature", "distinct_id"))
print(posthog.feature_enabled("beta-feature-groups", "distinct_id", groups={"company": "id:5"}))

print(posthog.feature_enabled("beta-feature", "distinct_id"))

# get payload
print(posthog.get_feature_flag_payload("beta-feature", "distinct_id"))
print(posthog.get_all_flags_and_payloads("distinct_id"))
exit()
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


posthog.set_once(
    "new_distinct_id", {"self_serve_signup": False}
)  # this will not change the property (because it was already set)

posthog.set("new_distinct_id", {"current_browser": "Chrome"})
posthog.set("new_distinct_id", {"current_browser": "Firefox"})


# #############################################################################
# Make sure you have a personal API key for the examples below

# Local Evaluation

# If flag has City=Sydney, this call doesn't go to `/decide`
print(posthog.feature_enabled("test-flag", "distinct_id_random_22", person_properties={"$geoip_city_name": "Sydney"}))

print(
    posthog.feature_enabled(
        "test-flag",
        "distinct_id_random_22",
        person_properties={"$geoip_city_name": "Sydney"},
        only_evaluate_locally=True,
    )
)


print(posthog.get_all_flags("distinct_id_random_22"))
print(posthog.get_all_flags("distinct_id_random_22", only_evaluate_locally=True))
print(
    posthog.get_all_flags(
        "distinct_id_random_22", person_properties={"$geoip_city_name": "Sydney"}, only_evaluate_locally=True
    )
)


posthog.shutdown()
