# PostHog Python

[![PyPI](https://img.shields.io/pypi/v/posthog)](https://pypi.org/project/posthog/)


Please see the [Python integration docs](https://posthog.com/docs/integrations/python-integration) for details.

## Development

### Testing Locally

1. Run `python3 -m venv env` (creates virtual environment called "env")
    * or `uv venv env`
2. Run `source env/bin/activate` (activates the virtual environment)
3. Run `python3 -m pip install -e ".[test]"` (installs the package in develop mode, along with test dependencies)
    * or `uv pip install -e ".[test]"`
4. Run `make test`
  1. To run a specific test do `pytest -k test_no_api_key`

### Running Locally

Assuming you have a [local version of PostHog](https://posthog.com/docs/developing-locally) running, you can run `python3 example.py` to see the library in action.

### Running the Django Sentry Integration Locally

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

### Releasing Versions

Updated are released using GitHub Actions: after bumping `version.py` in `master` and adding to `CHANGELOG.md`, go to [our release workflow's page](https://github.com/PostHog/posthog-python/actions/workflows/release.yaml) and dispatch it manually, using workflow from `master`.

## Questions?

### [Join our Slack community.](https://join.slack.com/t/posthogusers/shared_invite/enQtOTY0MzU5NjAwMDY3LTc2MWQ0OTZlNjhkODk3ZDI3NDVjMDE1YjgxY2I4ZjI4MzJhZmVmNjJkN2NmMGJmMzc2N2U3Yjc3ZjI5NGFlZDQ)
