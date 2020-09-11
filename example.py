# PostHog Python library example

# Import the library
import posthog

# You can find this key on the /setup page in PostHog
posthog.api_key = '<your key>'

# Where you host PostHog, with no trailing /.
# You can remove this line if you're using posthog.com
# posthog.host = 'http://127.0.0.1:8000' 

# Capture an event
posthog.capture('distinct_id', 'event', {'property1': 'value', 'property2': 'value'})

# # Alias a previous distinct id with a new one
# posthog.alias('distinct_id', 'new_distinct_id')

# # Add properties to the person
# posthog.identify('distinct_id', {'email': 'something@something.com'})