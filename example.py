# PostHog Python library example

# Import the library
import posthog
import time

# You can find this key on the /setup page in PostHog
posthog.api_key = ''
posthog.personal_api_key = ''

# Where you host PostHog, with no trailing /.
# You can remove this line if you're using posthog.com
posthog.host = 'http://127.0.0.1:8000'

# Capture an event
posthog.capture('distinct_id', 'event', {'property1': 'value', 'property2': 'value'})

print(posthog.feature_enabled('beta-feature', 'distinct_id'))

print('sleeping')
time.sleep(45)

print(posthog.feature_enabled('beta-feature', 'distinct_id'))

# # Alias a previous distinct id with a new one
posthog.alias('distinct_id', 'new_distinct_id')

# # Add properties to the person
posthog.identify('distinct_id', {'email': 'something@something.com'})