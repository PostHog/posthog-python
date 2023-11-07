# PostHog Python library example

# Import the library
import time
import uuid

import posthog

posthog.debug = True

api_key = "sTMFPsFhdP1Ssg"
host = "https://app.dev.posthog.dev"

# You can find this key on the /setup page in PostHog
# posthog.project_api_key = "sTMFPsFhdP1Ssg"
posthog.project_api_key = api_key
# posthog.project_api_key = "phc_uw1Luh19sPQ21stMORUwptefa7ETja6KVKXyRvNjOT9"
# posthog.project_api_key = "phc_pFHcQ4aIQalscAhuciS2odFxr0QhOJwKe7jXecQSiDP"
posthog.personal_api_key = "phx_vUhWLbQzd6Hbv9hTakMdXrNDNrI2nywURZGYyHGPXzO"

# Where you host PostHog, with no trailing /.
# You can remove this line if you're using posthog.com
# posthog.host = host
posthog.host = "https://app.posthog.com/"
# posthog.host = "http://localhost:8000"
# posthog.poll_interval = 10

print(posthog.get_all_flags("63e610c4-f63b-4c73-8dcb-9d9c31c8fa40", only_evaluate_locally=True))

# print(posthog.get_all_flags("xyz", only_evaluate_locally=True))
exit()

# print(posthog.feature_flag_definitions())
# print(posthog.get_all_flags("distinct_id"))
# {"token":"sTMFPsFhdP1Ssg","distinct_id":"SHCqo6_5LgQMRMzKMqJ84kVnYv84vo0jurPuM4xlsgU","groups":{"project":"fc445b88-e2c4-488e-bb52-aa80cd7918c9","organization":"4dc8564d-bd82-1065-2f40-97f7c50f67cf","customer":"cus_IK2DWsWVn2ZM16","instance":"https://app.posthog.com"},"person_properties":{"email":"neil@posthog.com"},"group_properties":{"project":{"id":2,"uuid":"fc445b88-e2c4-488e-bb52-aa80cd7918c9","name":"PostHog App + Website","ingested_event":true,"is_demo":false,"timezone":"US/Pacific","instance_tag":"none"},"organization":{"id":"4dc8564d-bd82-1065-2f40-97f7c50f67cf","name":"PostHog","slug":"posthog-kskn","created_at":"2020-09-24T15:05:01.254111Z","available_features":["zapier","slack_integration","microsoft_teams_integration","discord_integration","apps","app_metrics","boolean_flags","multivariate_flags","console_logs","recordings_playlists","recordings_performance","recordings_file_export","experimentation","group_analytics","dashboards","funnels","graphs_trends","paths","subscriptions","paths_advanced","dashboard_permissioning","dashboard_collaboration","ingestion_taxonomy","correlation_analysis","tagging","behavioral_cohort_filtering","tracked_users","data_retention","team_members","organizations_projects","api_access","google_login","project_based_permissioning","sso_enforcement","white_labeling","saml","role_based_access","community_support","dedicated_support","email_support","account_manager","training","configuration_support","terms_and_conditions","security_assessment","bespoke_pricing","invoice_payments","support_slas"],"taxonomy_set_events_count":1387,"taxonomy_set_properties_count":117,"instance_tag":"none"},"customer":{},"instance":{"site_url":"https://app.posthog.com"}}}
print(
    posthog.feature_enabled(
        "posthog-3000",
        "b514487b-f974-488e-b7ef-430f1c305f70",
        send_feature_flag_events=False,
        person_properties={
            "clientHash": "f407fcf8-43ed-4b58-98c4-1706c1cac865",
            "other": "treu",
            "$feature_enrollment/posthog-3000": "true",
        },
    )
)
# print(posthog.feature_flag_definitions())

posthog.capture("test id", "test event", disable_geoip=True)

exit()


posthog.capture("test id", "test event", disable_geoip=True)
posthog.capture("test id", "test event with geoip", disable_geoip=False)


posthog.capture("test id", "xyz")

flags = posthog.get_all_flags(
    "distinct_id",
    person_properties={
        "$initial_current_url": "http://localhost:8000/",
        "$geoip_city_name": "Sydney2",
    },
    only_evaluate_locally=True,
    disable_geoip=False,
)
print(flags)


exit()

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
# exit()
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
