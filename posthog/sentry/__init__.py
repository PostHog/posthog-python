import posthog
from posthog.request import DEFAULT_HOST

def update_posthog(event, _hint):
    if event.get('tags').get("posthog_distinct_id"):
        event['tags']["PostHog URL"] = f"{posthog.host or DEFAULT_HOST}/person/{event['tags']['posthog_distinct_id']}"
    return event
