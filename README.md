# PostHog Python

Please see the main [PostHog docs](https://posthog.com/docs).

Specifically, the [Python integration](https://posthog.com/docs/integrations/python-integration) details.

## Questions?

### [Join our Slack community.](https://join.slack.com/t/posthogusers/shared_invite/enQtOTY0MzU5NjAwMDY3LTc2MWQ0OTZlNjhkODk3ZDI3NDVjMDE1YjgxY2I4ZjI4MzJhZmVmNjJkN2NmMGJmMzc2N2U3Yjc3ZjI5NGFlZDQ)

# Local Development

## Testing Locally

1. Run `python3 -m venv env` (creates virtual environment called "env")
2. Run `source env/bin/activate` (activates the virtual environment)
3. Run `python3 -m pip install -e ".[test]"` (installs the package in develop mode, along with test dependencies)
4. Run `make test`

## Running Locally

Assuming you have a [local version of PostHog](https://posthog.com/docs/developing-locally) running, you can run `python3 example.py` to see the library in action.

## Running the Django Sentry Integration Locally

There's a sample Django project included, called `sentry_django_example`, which explains how to use PostHog with Sentry.

There's 2 places of importance (Changes required are all marked with TODO in the sample project directory)

1. Settings.py
    1. Input your Sentry DSN
    2. Input your Sentry Org and ProjectID details into `PosthogIntegration()`
    3. Add `POSTHOG_DJANGO` to settings.py. This allows the `PosthogDistinctIdMiddleware` to get the distinct_ids

2. urls.py
    1. This includes the `sentry-debug/` endpoint, which generates an exception

To run things: `make django_example`. This installs the posthog-python library with the sentry-sdk add-on, and then runs the django app.
Also start the PostHog app locally.
Then navigate to `http://127.0.0.1:8080/sentry-debug/` and you should get an event in both Sentry and PostHog, with links to each other.
