# PostHog Python library example
import argparse

import posthog

# Add argument parsing
parser = argparse.ArgumentParser(description="PostHog Python library example")
parser.add_argument(
    "--flag", default="person-on-events-enabled", help="Feature flag key to check (default: person-on-events-enabled)"
)
args = parser.parse_args()

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
        args.flag,  # Use the flag from command line arguments
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
print(posthog.get_remote_config_payload("encrypted_payload_flag_key"))


# You can add tags to a context, and these are automatically added to any events (including exceptions) captured
# within that context.

# You can enter a new context using a with statement. Any exceptions thrown in the context will be captured,
# and tagged with the context tags. Other events captured will also be tagged with the context tags. By default,
# the new context inherits tags from the parent context.
with posthog.new_context():
    posthog.tag("transaction_id", "abc123")
    posthog.tag("some_arbitrary_value", {"tags": "can be dicts"})

    # This event will be captured with the tags set above
    posthog.capture("order_processed")
    # This exception will be captured with the tags set above
    raise Exception("Order processing failed")


# Use fresh=True to start with a clean context (no inherited tags)
with posthog.new_context(fresh=True):
    posthog.tag("session_id", "xyz789")
    # Only session_id tag will be present, no inherited tags
    raise Exception("Session handling failed")


# You can also use the `@posthog.scoped()` decorator to enter a new context.
# By default, it inherits tags from the parent context
@posthog.scoped()
def process_order(order_id):
    posthog.tag("order_id", order_id)
    # Exception will be captured and tagged automatically
    raise Exception("Order processing failed")


# Use fresh=True to start with a clean context (no inherited tags)
@posthog.scoped(fresh=True)
def process_payment(payment_id):
    posthog.tag("payment_id", payment_id)
    # Only payment_id tag will be present, no inherited tags
    raise Exception("Payment processing failed")


posthog.shutdown()
