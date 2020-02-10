# PostHog Python

Official PostHog Python library to capture and send events to any PostHog instance (including PostHog.com).

This library uses an internal queue to make calls non-blocking and fast. It also batches requests and flushes asynchronously, making it perfect to use in any part of your web app or other server side application that needs performance.

## Installation 

```bash
pip install posthog
```

In your app, import the posthog library and set your api key **before** making any calls.

```python
import posthog

posthog.write_key = 'YOUR API KEY'
```

You can find your key in the /setup page in PostHog.

To debug, you can set debug mode.
```python
posthog.debug = True
```

## Making calls

### Capture

Capture allows you to capture anything a user does within your system, which you can later use in PostHog to find patterns in usage, work out which features to improve or where people are giving up.

A `capture` call requires
 - `distinct id` which uniquely identifies your user
 - `event name` to make sure 
   - We recommend using [verb] [noun], like `movie played` or `movie updated` to easily identify what your events mean later on.

Optionally you can submit
- `properties`, which can be a dict with any information you'd like to add

For example:
```python
posthog.capture('distinct id', 'movie played', {'movie_id': '123', 'category': 'romcom'})
```

### Identify
Identify lets you add metadata on your users so you can more easily identify who they are in PostHog, and even do things like segment users by these properties.

An `identify` call requires
- `distinct id` which uniquely identifies your user
- `properties` with a dict with any key: value pairs 

For example:
```python
posthog.capture('distinct id', {
    'email': 'dwayne@gmail.com',
    'name': 'Dwayne Johnson'
})
```

The most obvious place to make this call is whenever a user signs up, or when they update their information.

### Alias

To marry up whatever a user does before they sign up or log in with what they do after you need to make an alias call. This will allow you to answer questions like "Which marketing channels leads to users churning after a month?" or "What do users do on our website before signing up?"

In a purely back-end implementation, this means whenever an anonymous user does something, you'll want to send a session ID ([Django](https://stackoverflow.com/questions/526179/in-django-how-can-i-find-out-the-request-session-sessionid-and-use-it-as-a-vari), [Flask](https://stackoverflow.com/questions/15156132/flask-login-how-to-get-session-id)) with the capture call. Then, when that users signs up, you want to do an alias call with the session ID and the newly created user ID.

The same concept applies for when a user logs in.

An `alias` call requires
- `previous distinct id` the unique ID of the user before
- `distinct id` the current unique id

For example:
```python
posthog.alias('anonymous session id', 'distinct id')
```

## Django

For Django, you can do the initialisation of the key in the AppConfig, so that it's available everywhere.

in `yourapp/apps.py`
```python
from django.apps import AppConfig
import posthog

class YourAppConfig(AppConfig):
    def ready(self):
        posthog.write_key = 'your key'
```

Then, anywhere else in your app you can do
```python
import posthog

def homepage(request):
    # example capture
    posthog.capture(request.session.session_key, 'page view', ....)
```

# Development

## Naming confusion

As our open source project [PostHog](https://github.com/PostHog/posthog) shares the same module name, we create a special `posthog-analytics` package, mostly for internal use to avoid module collision. It is the exact same.

## How to release
1. Increase `VERSION` in `posthog/version.py`
2. run `make release` and `make release_analytics`
3. `git commit -am "Release X.Y.Z."` (where X.Y.Z is the new version)
4. `git tag -a X.Y.Z -m "Version X.Y.Z"` (where X.Y.Z is the new version).

## Thank you

This library is largely based on the `analytics-python` package.